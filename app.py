import os, re, json
from collections import defaultdict
from datetime import date, timedelta
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB
CORS(app)

# ============================================================
# 配置区 —— 建议在 Railway / Vercel 环境变量里设置
# ============================================================
APP_ID            = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET        = os.environ.get("FEISHU_APP_SECRET", "")
DEFAULT_SHEET_URL = os.environ.get("DEFAULT_SHEET_URL", "")

# 博主质量标准：平均粉丝数上限（单位：万）
MAX_AVG_FOLLOWERS = float(os.environ.get("MAX_AVG_FOLLOWERS", "5"))  # 默认 5 万

# 建联状态配置
STATUS_SUCCESS = "引导到私域了"
STATUS_CONTACT = {"抖音私信建联", "引导到私域了", "没引导到私域"}

# 考评权重
W_SUCCESS = 0.40
W_QUALITY = 0.30
W_ACTIVITY = 0.30
# ============================================================


# ────────────────────────────────────────────────────────────
# 飞书基础 API
# ────────────────────────────────────────────────────────────

def get_token(app_id, app_secret):
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"Token 获取失败：{data.get('msg')}，请检查 App ID / App Secret")
    return data["tenant_access_token"]


def extract_token_from_url(url):
    m = re.search(r"/(?:sheets|spreadsheets)/([A-Za-z0-9]+)", url)
    if m:
        return m.group(1), "sheet"
    m = re.search(r"/wiki/([A-Za-z0-9]+)", url)
    if m:
        return m.group(1), "wiki"
    raise Exception("无法从 URL 识别表格，请确认链接正确")


def resolve_spreadsheet_token(raw, kind, token):
    if kind == "sheet":
        return raw
    resp = requests.get(
        "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
        headers={"Authorization": f"Bearer {token}"},
        params={"token": raw, "obj_type": "wiki"},
        timeout=10,
    )
    data = resp.json()
    obj_token = data.get("data", {}).get("node", {}).get("obj_token", "")
    return obj_token if obj_token else raw


def get_all_sheets(stoken, token):
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{stoken}/sheets/query",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"获取工作表失败：{data.get('msg')}，请确认应用已获得表格权限")
    return data["data"]["sheets"]


def col_letter_to_index(col_letter):
    col_letter = col_letter.upper()
    result = 0
    for ch in col_letter:
        result = result * 26 + (ord(ch) - ord('A') + 1)
    return result - 1


def col_index_to_letter(idx):
    result = ""
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        result = chr(rem + ord('A')) + result
    return result


def get_sheet_range(stoken, sheet_id, col_start, col_end, token, max_row=3000):
    range_str = f"{sheet_id}!{col_start}1:{col_end}{max_row}"
    resp = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{stoken}/values/{range_str}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    data = resp.json()
    if data.get("code") != 0:
        return []
    return data.get("data", {}).get("valueRange", {}).get("values", []) or []


def write_duplicate_marks(stoken, sheet_id, rows_to_mark, mark_col, token):
    if not rows_to_mark:
        return 0
    value_ranges = []
    for row in rows_to_mark:
        value_ranges.append({
            "range": f"{sheet_id}!{mark_col}{row}:{mark_col}{row}",
            "values": [["已重复"]]
        })
    written = 0
    for i in range(0, len(value_ranges), 20):
        batch = value_ranges[i:i+20]
        resp = requests.post(
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{stoken}/values_batch_update",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"valueRanges": batch},
            timeout=15,
        )
        if resp.json().get("code") == 0:
            written += len(batch)
    return written


# ────────────────────────────────────────────────────────────
# 读取所有工作表数据（通用）
# 返回 list of records，每条包含：name, owner, followers, status, sheet, row
# ────────────────────────────────────────────────────────────

def read_all_records(stoken, cfg, token, skip_sheets):
    """
    cfg: {
      name_col, owner_col, followers_col, status_col,
      has_header
    }
    所有 col 均为列字母，followers_col / owner_col / status_col 可为空字符串
    """
    all_sheets = get_all_sheets(stoken, token)

    cols = [cfg["name_col"]]
    for c in [cfg.get("owner_col",""), cfg.get("followers_col",""), cfg.get("status_col","")]:
        if c:
            cols.append(c)

    # 计算最小读取范围
    indices = [col_letter_to_index(c) for c in cols]
    left_idx  = min(indices)
    right_idx = max(indices)
    left_col  = col_index_to_letter(left_idx)
    right_col = col_index_to_letter(right_idx)

    name_rel    = col_letter_to_index(cfg["name_col"]) - left_idx
    owner_rel   = col_letter_to_index(cfg["owner_col"]) - left_idx if cfg.get("owner_col") else None
    follow_rel  = col_letter_to_index(cfg["followers_col"]) - left_idx if cfg.get("followers_col") else None
    status_rel  = col_letter_to_index(cfg["status_col"]) - left_idx if cfg.get("status_col") else None

    records = []
    sheet_stats = []

    for sheet in all_sheets:
        title = sheet["title"]
        sid   = sheet.get("sheetId") or sheet.get("sheet_id", "")

        if title in skip_sheets or title == "重复检测结果":
            sheet_stats.append({"name": title, "count": 0, "skipped": True})
            continue

        rows  = get_sheet_range(stoken, sid, left_col, right_col, token)
        start = 1 if cfg.get("has_header", True) else 0
        count = 0

        for i, row in enumerate(rows[start:], start=start + 1):
            if not row:
                continue

            raw_name = row[name_rel] if len(row) > name_rel else ""
            if not raw_name:
                continue
            name = str(raw_name).strip()
            if not name:
                continue

            def get_col(rel):
                if rel is None: return ""
                v = row[rel] if len(row) > rel else ""
                return str(v).strip() if v else ""

            owner     = get_col(owner_rel)   or "未知"
            status    = get_col(status_rel)  or ""
            followers_raw = get_col(follow_rel)

            # 解析粉丝数（支持"1.2万"、"12000"、"1.2w"等格式）
            followers = parse_followers(followers_raw)

            records.append({
                "name":      name,
                "key":       name.replace(" ", "").lower(),
                "sheet":     title,
                "sheet_id":  sid,
                "row":       i,
                "owner":     owner,
                "status":    status,
                "followers": followers,
            })
            count += 1

        sheet_stats.append({"name": title, "count": count, "skipped": False})

    return records, sheet_stats, all_sheets


def parse_followers(raw):
    if not raw:
        return 0.0
    s = str(raw).strip().replace(",", "").replace("，", "")
    # 万/w
    m = re.match(r"([\d.]+)\s*[万wW]", s)
    if m:
        return float(m.group(1)) * 10000
    try:
        return float(s)
    except:
        return 0.0


# ────────────────────────────────────────────────────────────
# 重复检测
# ────────────────────────────────────────────────────────────

def detect_duplicates(records):
    name_map = defaultdict(list)
    for r in records:
        name_map[r["key"]].append(r)

    dups = []
    for key, occs in name_map.items():
        if len(occs) > 1:
            owners = [o["owner"] for o in occs]
            unique_owners = list(dict.fromkeys(owners))
            dups.append({
                "name":        occs[0]["name"],
                "count":       len(occs),
                "owners":      unique_owners,
                "occurrences": occs,
            })
    dups.sort(key=lambda x: -x["count"])

    total  = len(records)
    unique = len(name_map)
    return {
        "total":    total,
        "unique":   unique,
        "dupCount": len(dups),
        "dups":     dups,
    }


# ────────────────────────────────────────────────────────────
# 统计 & 考评
# ────────────────────────────────────────────────────────────

def calc_stats(records):
    bk = defaultdict(lambda: {
        "total": 0, "contacted": 0, "success": 0,
        "fans": [], "quality_fail": 0
    })
    for r in records:
        o = r["owner"]
        bk[o]["total"] += 1
        if r["status"] in STATUS_CONTACT:
            bk[o]["contacted"] += 1
        if r["status"] == STATUS_SUCCESS:
            bk[o]["success"] += 1
        f = r["followers"]
        if f > 0:
            bk[o]["fans"].append(f)
            if f / 10000 > MAX_AVG_FOLLOWERS:
                bk[o]["quality_fail"] += 1

    result = {}
    for o, b in bk.items():
        af  = sum(b["fans"]) / len(b["fans"]) if b["fans"] else 0
        c   = b["contacted"]
        result[o] = {
            **b,
            "avg_fans":   round(af / 10000, 2),   # 存万为单位
            "quality_ok": (af / 10000) <= MAX_AVG_FOLLOWERS,
            "rate":       round(b["success"] / c, 4) if c else 0,
        }
    return result


def calc_kpi(stats):
    avgs = [s["contacted"] for s in stats.values()]
    team_avg = sum(avgs) / len(avgs) if avgs else 1
    out = {}
    for o, s in stats.items():
        act  = min(s["contacted"] / max(team_avg, 1), 2.0) / 2.0
        qual = 1.0 if s["quality_ok"] else max(
            0, 1 - (s["avg_fans"] - MAX_AVG_FOLLOWERS) / MAX_AVG_FOLLOWERS
        )
        score = round((s["rate"] * W_SUCCESS + qual * W_QUALITY + act * W_ACTIVITY) * 100, 1)
        grade = "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"
        out[o] = {"score": score, "grade": grade}
    return out


def quality_summary(records):
    """全局博主质量概览"""
    total = len(records)
    with_fans = [r for r in records if r["followers"] > 0]
    if not with_fans:
        return {"avg": 0, "ok_rate": 0, "fail_count": 0}
    avg_fans = sum(r["followers"] for r in with_fans) / len(with_fans) / 10000
    fail = sum(1 for r in with_fans if r["followers"] / 10000 > MAX_AVG_FOLLOWERS)
    return {
        "avg":       round(avg_fans, 2),
        "ok_rate":   round((len(with_fans) - fail) / len(with_fans) * 100, 1) if with_fans else 0,
        "fail_count": fail,
        "threshold":  MAX_AVG_FOLLOWERS,
    }


# ────────────────────────────────────────────────────────────
# Flask 路由
# ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        default_url=DEFAULT_SHEET_URL,
        has_credentials=bool(APP_ID and APP_SECRET),
        max_followers=MAX_AVG_FOLLOWERS,
    )


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/check", methods=["POST"])
def check():
    """重复检测（+ 可选统计）"""
    body       = request.json or {}
    sheet_url  = (body.get("url") or "").strip()
    skip_raw   = [s.strip() for s in (body.get("skip") or "").split(",") if s.strip()]
    app_id     = APP_ID     or (body.get("appId") or "").strip()
    app_secret = APP_SECRET or (body.get("appSecret") or "").strip()

    cfg = {
        "name_col":      (body.get("nameCol")      or "D").strip().upper(),
        "owner_col":     (body.get("ownerCol")     or "").strip().upper(),
        "followers_col": (body.get("followersCol") or "").strip().upper(),
        "status_col":    (body.get("statusCol")    or "").strip().upper(),
        "has_header":    body.get("hasHeader", True),
    }

    if not sheet_url:
        return jsonify({"error": "请填写表格 URL"}), 400
    if not app_id or not app_secret:
        return jsonify({"error": "未配置飞书应用凭证"}), 400

    try:
        token  = get_token(app_id, app_secret)
        raw, kind = extract_token_from_url(sheet_url)
        stoken = resolve_spreadsheet_token(raw, kind, token)

        records, sheet_stats, _ = read_all_records(stoken, cfg, token, skip_raw)

        dup_result   = detect_duplicates(records)
        qual_summary = quality_summary(records) if cfg["followers_col"] else None

        return jsonify({
            **dup_result,
            "sheets":        sheet_stats,
            "qualitySummary": qual_summary,
            "_stoken":       stoken,
            "_token":        token,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/stats", methods=["POST"])
def stats():
    """统计报告：每人建联数、转化率、考评"""
    body       = request.json or {}
    sheet_url  = (body.get("url") or "").strip()
    skip_raw   = [s.strip() for s in (body.get("skip") or "").split(",") if s.strip()]
    app_id     = APP_ID     or (body.get("appId") or "").strip()
    app_secret = APP_SECRET or (body.get("appSecret") or "").strip()
    period     = body.get("period", "all")   # "today" | "week" | "all"

    cfg = {
        "name_col":      (body.get("nameCol")      or "D").strip().upper(),
        "owner_col":     (body.get("ownerCol")     or "F").strip().upper(),
        "followers_col": (body.get("followersCol") or "").strip().upper(),
        "status_col":    (body.get("statusCol")    or "").strip().upper(),
        "date_col":      (body.get("dateCol")      or "").strip().upper(),
        "has_header":    body.get("hasHeader", True),
    }

    if not sheet_url:
        return jsonify({"error": "请填写表格 URL"}), 400
    if not app_id or not app_secret:
        return jsonify({"error": "未配置飞书应用凭证"}), 400

    try:
        token  = get_token(app_id, app_secret)
        raw, kind = extract_token_from_url(sheet_url)
        stoken = resolve_spreadsheet_token(raw, kind, token)

        # 读取时把 date_col 也纳入范围
        cfg_read = {**cfg}
        if cfg["date_col"]:
            cfg_read["date_col"] = cfg["date_col"]

        records, sheet_stats, _ = read_all_records(stoken, cfg_read, token, skip_raw)

        # 如果有日期列，先按日期过滤
        if cfg["date_col"] and period != "all":
            today = date.today()
            filtered = []
            for r in records:
                d_raw = r.get("date_str", "")
                try:
                    d = date.fromisoformat(str(d_raw).strip())
                    if period == "today" and d == today:
                        filtered.append(r)
                    elif period == "week":
                        week_start = today - timedelta(days=today.weekday())
                        if week_start <= d <= today:
                            filtered.append(r)
                except:
                    pass
            records = filtered

        # 统计前先查重（标出重复，供前端展示）
        dup_result = detect_duplicates(records)
        # 去重后再统计（每个博主只算一次，取首次出现）
        seen_keys = set()
        deduped   = []
        for r in records:
            if r["key"] not in seen_keys:
                seen_keys.add(r["key"])
                deduped.append(r)

        stat_data  = calc_stats(deduped)
        kpi_data   = calc_kpi(stat_data)
        qual_data  = quality_summary(deduped) if cfg["followers_col"] else None

        # 汇总
        total_contact = sum(s["contacted"] for s in stat_data.values())
        total_success = sum(s["success"]   for s in stat_data.values())
        total_blogger = len(deduped)

        # 每人详情
        persons = []
        for owner in sorted(stat_data, key=lambda x: stat_data[x]["contacted"], reverse=True):
            s = stat_data[owner]
            k = kpi_data.get(owner, {"score": 0, "grade": "D"})
            persons.append({
                "owner":     owner,
                "total":     s["total"],
                "contacted": s["contacted"],
                "success":   s["success"],
                "rate":      round(s["rate"] * 100, 1),
                "avg_fans":  s["avg_fans"],
                "quality_ok": s["quality_ok"],
                "score":     k["score"],
                "grade":     k["grade"],
            })

        return jsonify({
            "period":       period,
            "totalBlogger": total_blogger,
            "totalContact": total_contact,
            "totalSuccess": total_success,
            "overallRate":  round(total_success / total_contact * 100, 1) if total_contact else 0,
            "dupCount":     dup_result["dupCount"],
            "persons":      persons,
            "quality":      qual_data,
            "sheets":       sheet_stats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/mark", methods=["POST"])
def mark():
    """把重复行写回飞书，标注"已重复"（首次出现保留）"""
    body     = request.json or {}
    stoken   = (body.get("stoken") or "").strip()
    token    = (body.get("token")  or "").strip()
    mark_col = (body.get("markCol") or "H").strip().upper()
    dups     = body.get("dups") or []

    if not stoken or not token:
        return jsonify({"error": "缺少表格凭证，请重新检测"}), 400

    sheet_rows = defaultdict(list)
    sheet_id_map = {}
    for dup in dups:
        for occ in (dup.get("occurrences") or [])[1:]:
            sid = occ["sheet_id"]
            sheet_rows[sid].append(occ["row"])
            sheet_id_map[sid] = occ["sheet"]

    total_marked = 0
    details = []
    for sid, rows in sheet_rows.items():
        n = write_duplicate_marks(stoken, sid, rows, mark_col, token)
        total_marked += n
        details.append({"sheet": sheet_id_map[sid], "marked": n})

    return jsonify({"marked": total_marked, "details": details})


DEEPSEEK_KEY = os.environ.get("DEEPSEEK_KEY", "sk-bc0712cef1064ba995cc4c170bfd0166")
QWEN_KEY     = os.environ.get("QWEN_KEY",     "sk-eaf0550046c94b37920b095d75b730f7")
TAVILY_KEY   = os.environ.get("TAVILY_KEY",   "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")


@app.route("/api/search", methods=["POST"])
def api_search():
    """DuckDuckGo 搜索博主信息（免费无需 Key）"""
    body  = request.json or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query 不能为空"}), 400
    try:
        # DuckDuckGo Instant Answer API
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        data = resp.json()
        parts = []
        if data.get("AbstractText"):
            parts.append(data["AbstractText"])
        if data.get("Answer"):
            parts.append(data["Answer"])
        for r in data.get("RelatedTopics", [])[:5]:
            if isinstance(r, dict) and r.get("Text"):
                parts.append(r["Text"])
        text = " ".join(parts).strip()

        # 如果 DDG 返回内容太少，补充用 Google 搜索摘要
        if len(text) < 100:
            g_resp = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"q": query, "num": 3, "key": "AIzaSyC-placeholder"},
                timeout=8
            )
            # 如果 Google 也不行，直接返回博主名作为基础信息
            text = text or f"搜索关键词：{query}"

        return jsonify({"text": text[:2000]})
    except Exception as e:
        return jsonify({"text": f"搜索关键词：{query}，搜索服务暂时不可用"})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """DeepSeek 分析博主类型或生成报告（服务端转发）"""
    body    = request.json or {}
    prompt  = (body.get("prompt") or "").strip()
    max_tok = int(body.get("max_tokens", 1000))
    if not prompt:
        return jsonify({"error": "prompt 不能为空"}), 400
    try:
        resp = requests.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {QWEN_KEY}", "Content-Type": "application/json"},
            json={"model": "qwen-turbo", "max_tokens": max_tok,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=55,
        )
        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not text:
            err_msg = str(data.get("error", {}) or data)
            return jsonify({"error": "分析失败：" + err_msg}), 500
        return jsonify({"text": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/vision", methods=["POST"])
def api_vision():
    """通义千问 VL 识别博主截图（支持自定义prompt）"""
    body   = request.json or {}
    images = body.get("images", [])   # base64 列表
    custom_prompt = body.get("prompt", "").strip()
    if not images:
        return jsonify({"error": "images 不能为空"}), 400

    default_prompt = """请识别图片中的博主主页信息，每张图片对应一个博主。
严格按以下 JSON 数组格式返回，不要任何其他文字：
[
  {
    "name": "博主名称",
    "link": "主页链接（如有，否则空字符串）",
    "platform": "平台（小红书/抖音/B站/微博等）",
    "followers": "粉丝数（纯数字+单位，如 19.4万）",
    "blogger_type": "博主类型（如 AI科技/生活方式/知识分享/手工DIY等）",
    "status": "",
    "error": ""
  }
]
如果某张图片无法识别或信息不足，在对应条目的 error 字段说明原因，其他字段留空。
注意：followers 只填粉丝数，不要填获赞数或关注数。"""

    # 构造多张图片的消息
    content_parts = []
    for img_b64 in images:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
        })
    content_parts.append({
        "type": "text",
        "text": custom_prompt if custom_prompt else default_prompt
    })

    try:
        resp = requests.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {QWEN_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "qwen-vl-plus",
                "messages": [{"role": "user", "content": content_parts}],
                "max_tokens": 3000
            },
            timeout=55
        )
        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not text:
            err_msg = str(data.get("error", {}))
            return jsonify({"error": "识别失败：" + err_msg}), 500
        # 尝试解析 JSON（数组或对象均可）
        match = re.search(r'(\[[\s\S]*\]|\{[\s\S]*\})', text)
        if match:
            try:
                result = json.loads(match.group(0))
                # 统一返回数组
                if isinstance(result, dict):
                    return jsonify({"result": [result]})
                return jsonify({"result": result})
            except Exception:
                pass
        return jsonify({"raw": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


SUPABASE_URL  = "https://wtfmhfpjgoliyzsozfav.supabase.co"
SUPABASE_ANON = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind0Zm1oZnBqZ29saXl6c296ZmF2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODAwMjE2NjYsImV4cCI6MjA5NTU5NzY2Nn0.FxMCYutcjxm4YdwW3rGj0rALQYEeagS6D0vEINxb8Vc"


@app.route("/api/data", methods=["GET"])
def api_data():
    """中转 Supabase 数据请求"""
    table  = request.args.get("table", "bloggers")
    select = request.args.get("select", "*")
    limit  = request.args.get("limit", "1000")
    offset = request.args.get("offset", "0")
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select={select}&limit={limit}&offset={offset}&order=id.asc"
        resp = requests.get(url, headers={
            "apikey": SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}"
        }, timeout=15)
        return resp.text, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/reports", methods=["GET"])
def api_reports():
    """中转 Supabase reports 表"""
    try:
        url = f"{SUPABASE_URL}/rest/v1/reports?select=*&order=id.desc"
        resp = requests.get(url, headers={
            "apikey": SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}"
        }, timeout=15)
        return resp.text, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/reports/save", methods=["POST"])
def api_reports_save():
    """保存分析报告到 Supabase"""
    body = request.json or {}
    period_key   = (body.get("period_key") or "").strip()
    period_label = (body.get("period_label") or "").strip()
    report_text  = (body.get("report_text") or "").strip()
    blogger_count = int(body.get("blogger_count", 0))
    if not period_key or not report_text:
        return jsonify({"error": "period_key 和 report_text 不能为空"}), 400
    try:
        # 先尝试更新，如果不存在则插入
        headers = {
            "apikey": SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}",
            "Content-Type": "application/json",
        }
        payload = {"period_key": period_key, "period_label": period_label,
                   "report_text": report_text, "blogger_count": blogger_count}

        # 检查是否已存在
        check = requests.get(
            f"{SUPABASE_URL}/rest/v1/analysis_reports?period_key=eq.{period_key}&select=id",
            headers=headers, timeout=10
        )
        existing = check.json()

        if existing:
            # 更新
            resp = requests.patch(
                f"{SUPABASE_URL}/rest/v1/analysis_reports?period_key=eq.{period_key}",
                headers={**headers, "Prefer": "return=minimal"},
                json=payload, timeout=10
            )
        else:
            # 插入
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/analysis_reports",
                headers={**headers, "Prefer": "return=minimal"},
                json=payload, timeout=10
            )

        if resp.status_code in (200, 201, 204):
            return jsonify({"ok": True})
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reports/list", methods=["GET"])
def api_reports_list():
    """获取所有分析报告列表"""
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/analysis_reports?select=id,period_key,period_label,blogger_count,created_at&order=created_at.desc",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"},
            timeout=10
        )
        return resp.text, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reports/get", methods=["GET"])
def api_reports_get():
    """获取指定 period_key 的报告"""
    period_key = request.args.get("period_key", "")
    if not period_key:
        return jsonify({"error": "period_key 不能为空"}), 400
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/analysis_reports?period_key=eq.{period_key}&select=*",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"},
            timeout=10
        )
        data = resp.json()
        if data:
            return jsonify(data[0])
        return jsonify({"error": "not_found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rank/save", methods=["POST"])
def api_rank_save():
    body = request.json or {}
    report_key   = (body.get("report_key") or "").strip()
    report_data  = body.get("report_data", {})
    blogger_count = int(body.get("blogger_count", 0))
    if not report_key:
        return jsonify({"error": "report_key 不能为空"}), 400
    try:
        headers = {
            "apikey": SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}",
            "Content-Type": "application/json",
        }
        check = requests.get(
            f"{SUPABASE_URL}/rest/v1/rank_reports?report_key=eq.{report_key}&select=id",
            headers=headers, timeout=10
        )
        payload = {"report_key": report_key, "report_data": report_data if isinstance(report_data, dict) else json.loads(report_data), "blogger_count": blogger_count}
        if check.json():
            resp = requests.patch(
                f"{SUPABASE_URL}/rest/v1/rank_reports?report_key=eq.{report_key}",
                headers={**headers, "Prefer": "return=minimal"},
                json=payload, timeout=10
            )
        else:
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/rank_reports",
                headers={**headers, "Prefer": "return=minimal"},
                json=payload, timeout=10
            )
        if resp.status_code in (200, 201, 204):
            return jsonify({"ok": True})
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rank/list", methods=["GET"])
def api_rank_list():
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/rank_reports?select=id,report_key,blogger_count,created_at&order=created_at.desc",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"},
            timeout=10
        )
        return resp.text, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rank/get", methods=["GET"])
def api_rank_get():
    report_key = request.args.get("report_key", "")
    if not report_key:
        return jsonify({"error": "report_key 不能为空"}), 400
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/rank_reports?report_key=eq.{report_key}&select=*",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"},
            timeout=10
        )
        data = resp.json()
        if data:
            row = data[0]
            if isinstance(row.get("report_data"), str):
                row["report_data"] = json.loads(row["report_data"])
            return jsonify(row)
        return jsonify({"error": "not_found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/checkins", methods=["GET"])
def api_checkins_get():
    """获取签到记录"""
    from datetime import datetime
    date  = request.args.get("date", "")
    month = request.args.get("month", "")  # YYYY-MM
    try:
        url = f"{SUPABASE_URL}/rest/v1/checkins?order=checkin_date.desc&limit=1000"
        if date:
            url += f"&checkin_date=eq.{date}"
        elif month:
            url += f"&checkin_date=gte.{month}-01&checkin_date=lte.{month}-31"
        elif request.args.get("before", ""):
            before = request.args.get("before")
            url += f"&checkin_date=lt.{before}"
        else:
            # 默认取当月全部
            ym = datetime.now().strftime("%Y-%m")
            url += f"&checkin_date=gte.{ym}-01"
        resp = requests.get(url, headers={
            "apikey": SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}"
        }, timeout=15)
        return resp.text, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/checkins", methods=["POST"])
def api_checkins_save():
    """保存签到记录"""
    body = request.json or {}
    member_name  = body.get("member_name", "")
    checkin_date = body.get("checkin_date", "")
    if not member_name or not checkin_date:
        return jsonify({"error": "member_name 和 checkin_date 不能为空"}), 400
    try:
        headers = {
            "apikey": SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}",
            "Content-Type": "application/json",
        }
        payload = {
            "member_name": member_name,
            "checkin_date": checkin_date,
            "chat_images": body.get("chat_images", []),
            "blogger_images": body.get("blogger_images", []),
            "review_images": body.get("review_images", []),
            "submitted": body.get("submitted", False),
            "admin_override": body.get("admin_override", None),
        }
        # 先查询是否已存在该记录
        check = requests.get(
            f"{SUPABASE_URL}/rest/v1/checkins?member_name=eq.{requests.utils.quote(member_name)}&checkin_date=eq.{checkin_date}&select=id",
            headers=headers, timeout=10
        )
        existing = []
        try:
            existing = check.json() if check.status_code == 200 else []
        except Exception:
            existing = []

        if existing and isinstance(existing, list) and existing[0].get("id"):
            # 已存在则 PATCH 更新
            record_id = existing[0]["id"]
            resp = requests.patch(
                f"{SUPABASE_URL}/rest/v1/checkins?id=eq.{record_id}",
                headers={**headers, "Prefer": "return=representation"},
                json=payload, timeout=10
            )
        else:
            # 不存在则 POST 新增
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/checkins",
                headers={**headers, "Prefer": "return=representation"},
                json=payload, timeout=10
            )
        if resp.status_code in (200, 201):
            data = resp.json()
            return jsonify(data[0] if isinstance(data, list) and data else (data if not isinstance(data, list) else {}))
        print(f"[checkins_save] Supabase error {resp.status_code}: {resp.text[:500]}")
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        print(f"[checkins_save] exception: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/tactics", methods=["GET"])
def api_tactics_get():
    """获取话术库，字段映射为前端格式"""
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/tactics?select=*&order=created_at.desc&limit=200",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"},
            timeout=10
        )
        data = resp.json()
        # 把数据库字段映射为前端期望的格式
        mapped = []
        for t in data:
            mapped.append({
                "id":       t.get("id"),
                "type":     t.get("category", "其他"),
                "script":   t.get("content", ""),
                "member":   t.get("member", ""),
                "note":     t.get("note", ""),
                "platform": t.get("source", ""),
                "date":     t.get("created_at", "")[:10] if t.get("created_at") else "",
            })
        return json.dumps(mapped, ensure_ascii=False), 200, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/tactics", methods=["POST"])
def api_tactics_save():
    """保存话术到数据库"""
    body = request.json
    # 支持直接数组或 {tactics: [...]} 两种格式
    if isinstance(body, list):
        tactics_list = body
    elif isinstance(body, dict):
        tactics_list = body.get("tactics", [])
        if not tactics_list:
            # 尝试把 body 本身当单条
            tactics_list = [body]
    else:
        return jsonify({"error": "无效数据格式"}), 400

    if not tactics_list:
        return jsonify({"error": "tactics 不能为空"}), 400

    # 字段名映射：前端用 type/script，数据库用 category/content
    mapped = []
    for t in tactics_list:
        if isinstance(t, dict):
            mapped.append({
                "category": t.get("category") or t.get("type") or "其他",
                "content":  t.get("content")  or t.get("script") or "",
                "member":   t.get("member",  ""),
                "note":     t.get("note",    ""),
                "source":   t.get("source")  or t.get("platform") or "",
            })
    if not mapped:
        return jsonify({"error": "没有有效的话术数据"}), 400

    try:
        headers = {
            "apikey": SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/tactics",
            headers=headers,
            json=mapped,
            timeout=10
        )
        if resp.status_code in (200, 201, 204):
            return jsonify({"ok": True, "count": len(mapped)})
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tactics/<int:tactic_id>", methods=["DELETE"])
def api_tactics_delete(tactic_id):
    """删除话术"""
    try:
        resp = requests.delete(
            f"{SUPABASE_URL}/rest/v1/tactics?id=eq.{tactic_id}",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"},
            timeout=10
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/members", methods=["GET"])
def api_members_get():
    """获取成员状态列表"""
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/member_status?select=*&order=member_name",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"},
            timeout=10
        )
        return resp.text, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/members", methods=["POST"])
def api_members_update():
    """更新成员状态（离职/恢复）"""
    body = request.json or {}
    member_name = (body.get("member_name") or "").strip()
    status = body.get("status", "active")  # active / inactive
    note = body.get("note", "")
    if not member_name:
        return jsonify({"error": "member_name 不能为空"}), 400
    try:
        headers = {
            "apikey": SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}",
            "Content-Type": "application/json",
        }
        check = requests.get(
            f"{SUPABASE_URL}/rest/v1/member_status?member_name=eq.{member_name}&select=id",
            headers=headers, timeout=10
        )
        payload = {"member_name": member_name, "status": status, "note": note,
                   "updated_at": "now()"}
        if check.json():
            resp = requests.patch(
                f"{SUPABASE_URL}/rest/v1/member_status?member_name=eq.{member_name}",
                headers={**headers, "Prefer": "return=minimal"},
                json=payload, timeout=10
            )
        else:
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/member_status",
                headers={**headers, "Prefer": "return=minimal"},
                json=payload, timeout=10
            )
        if resp.status_code in (200, 201, 204):
            return jsonify({"ok": True})
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/kb", methods=["POST"])
def api_kb_add():
    """新增知识库条目"""
    body = request.json or {}
    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/knowledge_base",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            json=body, timeout=10
        )
        if resp.status_code in (200, 201, 204):
            return jsonify({"ok": True})
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/kb/<int:kb_id>", methods=["PATCH"])
def api_kb_update(kb_id):
    """更新知识库条目"""
    body = request.json or {}
    try:
        resp = requests.patch(
            f"{SUPABASE_URL}/rest/v1/knowledge_base?id=eq.{kb_id}",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"},
            json=body, timeout=10
        )
        if resp.status_code in (200, 201, 204):
            return jsonify({"ok": True})
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/kb/<int:kb_id>", methods=["DELETE"])
def api_kb_delete(kb_id):
    """删除知识库条目"""
    try:
        resp = requests.delete(
            f"{SUPABASE_URL}/rest/v1/knowledge_base?id=eq.{kb_id}",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"},
            timeout=10
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# 周绩效报告（自动/手动生成）—— calcMemberPerf 的 Python 实现
# ============================================================
PERF_CHECKIN_MEMBERS = [
    {"name": "杜承睿"}, {"name": "崔靖"}, {"name": "卢泳琦"}, {"name": "李可"},
    {"name": "许家铭"}, {"name": "汉强生"}, {"name": "刘焱"}, {"name": "寇诺琪"}, {"name": "谢家顺"},
]
PERF_STATUS_SUCCESS = ["引导到私域了", "二次回访成功"]


def _perf_headers():
    return {"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"}


def _perf_week_dates(monday):
    """给定周一日期(date对象)，返回周一到周五的 'YYYY-MM-DD' 列表"""
    return [(monday + timedelta(days=i)).isoformat() for i in range(5)]


def _perf_fetch_all_rows():
    """获取 bloggers 表全部记录（建联数据）"""
    rows = []
    offset = 0
    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bloggers?select=owner,record_date,status,name,blogger_type,followers,is_dup"
            f"&order=record_date.asc&limit=1000&offset={offset}",
            headers=_perf_headers(), timeout=20
        )
        batch = resp.json() if resp.status_code == 200 else []
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return rows


def _perf_fetch_checkins(dates):
    """获取指定日期范围的签到记录，按 {date: {member: {chat,blogger,review}}} 组织"""
    if not dates:
        return {}
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/checkins?checkin_date=in.({','.join(dates)})&select=member_name,checkin_date,chat_images,blogger_images,review_images",
        headers=_perf_headers(), timeout=20
    )
    rows = resp.json() if resp.status_code == 200 else []
    result = {}
    for r in (rows if isinstance(rows, list) else []):
        d = r.get("checkin_date")
        m = r.get("member_name")
        if not d or not m:
            continue
        result.setdefault(d, {})[m] = {
            "chat": r.get("chat_images") or [],
            "blogger": r.get("blogger_images") or [],
            "review": r.get("review_images") or [],
        }
    return result


def _perf_fetch_tactics(dates):
    """获取话术库记录，按成员+日期统计本周条数"""
    if not dates:
        return []
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/tactics?select=member,date",
        headers=_perf_headers(), timeout=20
    )
    return resp.json() if resp.status_code == 200 else []


def _perf_fetch_compat_scores(dates):
    """获取博主适配度分析记录"""
    if not dates:
        return []
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/compat_scores?score_date=in.({','.join(dates)})&select=member_name,score_date,compat_score",
        headers=_perf_headers(), timeout=20
    )
    return resp.json() if resp.status_code == 200 else []


def _perf_normalize_tactic_date(raw):
    """tactics.date 字段格式可能是 YYYY/M/D 或 YYYY-MM-DD，统一转成 YYYY-MM-DD"""
    if not raw:
        return None
    try:
        parts = re.split(r"[/-]", str(raw).strip())
        if len(parts) != 3:
            return None
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
        return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:
        return None


def calc_member_perf_py(member_name, dates, today_str, all_rows, checkins_by_date, tactics_rows, compat_rows):
    """
    Python 版 calcMemberPerf，逐项对照 index_new.html 中的同名函数。
    dates: 本周一到周五的日期字符串列表
    today_str: 'YYYY-MM-DD'，用于判断"未来日期不计分"
    """
    # ── 维度1：日常建联 (40分) ──
    daily_contact = {d: 0 for d in dates}
    daily_success = {d: 0 for d in dates}

    owner_rows = []
    for r in all_rows:
        owner = (r.get("owner") or "").strip()
        if owner != member_name:
            continue
        if r.get("is_dup"):
            continue
        d = r.get("record_date")
        if d not in dates:
            continue
        owner_rows.append(r)
        if r.get("status"):
            daily_contact[d] += 1
            if r.get("status") in PERF_STATUS_SUCCESS:
                daily_success[d] += 1

    contact_detail = dict(daily_contact)

    work_days = sum(1 for d in dates if d <= today_str)
    reach_days = 0
    for d in dates:
        if d > today_str:
            continue
        invited = daily_contact.get(d, 0)
        success = daily_success.get(d, 0)
        if invited >= 5 and success >= 3:
            reach_days += 1
    s_contact_qty = round(reach_days / work_days * 15) if work_days > 0 else 0

    # 表格信息完整性 10分
    complete_rows = [r for r in owner_rows if r.get("name") and r.get("status")]
    s_table = round(len(complete_rows) / len(owner_rows) * 10) if owner_rows else 0

    # 博主类型达标 10分
    type_ok = [r for r in owner_rows if r.get("blogger_type") and r.get("blogger_type") != "未知"]
    s_type = round(len(type_ok) / len(owner_rows) * 10) if owner_rows else 0

    # 粉丝数记录 5分
    fans_ok = [r for r in owner_rows if (r.get("followers") or 0) > 0]
    s_fans = round(len(fans_ok) / len(owner_rows) * 5) if owner_rows else 0

    dim1 = s_contact_qty + s_table + s_type + s_fans  # /40

    # ── 维度2：截图上传 (30分) ──
    s_review = s_chat = s_blogger_img = 0
    for d in dates:
        if d > today_str:
            continue
        rec = checkins_by_date.get(d, {}).get(member_name, {})
        if rec.get("review"):
            s_review += 2
        if rec.get("chat"):
            s_chat += 2
        if rec.get("blogger"):
            s_blogger_img += 2
    s_review = min(10, s_review)
    s_chat = min(10, s_chat)
    s_blogger_img = min(10, s_blogger_img)
    dim2 = s_review + s_chat + s_blogger_img  # /30

    # ── 维度3：工作质量 (20分) ──
    total_contact = len(owner_rows)
    total_success = len([r for r in owner_rows if r.get("status") in PERF_STATUS_SUCCESS])
    fans_vals = [r.get("followers") or 0 for r in owner_rows if (r.get("followers") or 0) > 0]
    avg_fans = (sum(fans_vals) / len(fans_vals) / 10000) if fans_vals else 0
    success_rate = (total_success / total_contact) if total_contact > 0 else 0
    dim3 = round(min(20, success_rate * 20 * 2))

    # ── 附加分 ──
    week_tactics_count = 0
    for r in tactics_rows:
        if (r.get("member") or "") != member_name:
            continue
        rd = _perf_normalize_tactic_date(r.get("date"))
        if rd is None:
            week_tactics_count += 1  # 无日期的不过滤（保底，与前端一致）
        elif rd in dates:
            week_tactics_count += 1
    bonus_script = min(20, week_tactics_count * 2)

    bonus_adapt = 0
    for d in dates:
        if d > today_str:
            continue
        has_success = any(
            (r.get("owner") or "").strip() == member_name and r.get("record_date") == d
            and r.get("status") in PERF_STATUS_SUCCESS and not r.get("is_dup")
            for r in all_rows
        )
        if has_success:
            continue
        day_scores = [r for r in compat_rows if r.get("member_name") == member_name and r.get("score_date") == d]
        if not day_scores:
            continue
        avg = sum((r.get("compat_score") or 0) for r in day_scores) / len(day_scores)
        if avg >= 70:
            bonus_adapt += 20
    bonus_adapt = min(100, bonus_adapt)

    bonus_total = min(10, bonus_adapt + bonus_script)

    total = min(100, dim1 + dim2 + dim3 + bonus_total)

    return {
        "total": round(total),
        "dim1": dim1, "dim2": dim2, "dim3": dim3, "bonusTotal": bonus_total,
        "s_contact_qty": s_contact_qty, "s_table": s_table, "s_type": s_type, "s_fans": s_fans,
        "s_review": s_review, "s_chat": s_chat, "s_blogger_img": s_blogger_img,
        "reachDays": reach_days, "workDays": work_days,
        "totalContact": total_contact, "totalSuccess": total_success,
        "contactDetail": contact_detail,
        "bonus_adapt": bonus_adapt, "bonus_script": bonus_script,
        "avgFans": round(avg_fans, 1),
        "dailySuccess": daily_success,
    }


def _qwen_complete(prompt, max_tokens=1200):
    """调用 qwen-turbo 生成文本，失败时返回空字符串"""
    try:
        resp = requests.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {QWEN_KEY}", "Content-Type": "application/json"},
            json={"model": "qwen-turbo", "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=55,
        )
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as e:
        print(f"[_qwen_complete] error: {e}")
        return ""


WEEKLY_REPORT_RULES = """你是一名严谨的数据分析师，撰写本部分时必须严格遵守：
1. 禁止使用"总体良好"、"稳步提升"、"有待加强"、"持续优化"、"前景可观"、"建议关注"、"存在不足"等空话、客套话。
2. 所有结论必须绑定具体数值、比例、差值；没有数据支撑的内容不写。
3. 语言务实精炼，分点或分句陈述，不堆砌形容词。
4. 如果某天数据为0或异常，必须明确指出具体哪天、什么指标、数值是多少。"""


def _build_member_ai_text(member_name, perf, dates, day_breakdown, rank, total_members, is_bottom):
    """调用AI生成单个成员的综合评价/高分维度/问题维度/每日记录解读"""
    daily_lines = []
    for d, db in zip(dates, day_breakdown):
        if db["future"]:
            continue
        parts = [f"建联{db['cnt']}位"]
        if db["chatN"] or db["blogN"] or db["reviewN"]:
            parts.append(f"截图(列表{db['chatN']}/成功{db['blogN']}/复盘{db['reviewN']})")
        else:
            parts.append("无截图")
        daily_lines.append(f"{d}：{', '.join(parts)}")
    daily_text = "\n".join(daily_lines) if daily_lines else "本周无可统计的工作日"

    prompt = f"""{WEEKLY_REPORT_RULES}

成员：{member_name}
本周总分：{perf['total']}/100，排名第{rank}/{total_members}
维度得分：日常建联 {perf['dim1']}/40（建联数量达标{perf['s_contact_qty']}/15、表格完整{perf['s_table']}/10、博主类型{perf['s_type']}/10、粉丝记录{perf['s_fans']}/5）、截图上传 {perf['dim2']}/30（复盘{perf['s_review']}/10、建联成功{perf['s_chat']}/10、建联列表{perf['s_blogger_img']}/10）、工作质量 {perf['dim3']}/20、附加分 {perf['bonusTotal']}/10
建联数据：本周建联总数{perf['totalContact']}，成功{perf['totalSuccess']}，成功率{(perf['totalSuccess']/perf['totalContact']*100) if perf['totalContact'] else 0:.1f}%，达标天数{perf['reachDays']}/{perf['workDays']}
每日明细：
{daily_text}
是否处于末位20%：{'是' if is_bottom else '否'}

请输出以下三部分内容（每部分前用【】标注小标题，内容用分点列出，每点一句话且必须含数字）：
【高分维度】列出本周得分较高、有具体数据支撑的维度（1-2条，必须引用上方数字；若所有维度都不突出，写"本周无维度得分明显领先"）
【问题维度】指出本周得分较低或异常的维度（1-3条，必须引用具体数字和日期，比如某天建联0位、某类截图全周未传等）
【每日记录解读】用1-2句话概括本周5天的工作节奏特征（必须引用至少2天的具体数字，指出波动或异常点；如数据规律不明显，直接写"本周数据未显示明显规律"）
"""
    text = _qwen_complete(prompt, max_tokens=600)
    return text.strip()


def _build_bottom_advice(bottom_members_summary):
    """为末位成员生成统一的末位建议段落"""
    if not bottom_members_summary:
        return ""
    lines = "\n".join(bottom_members_summary)
    prompt = f"""{WEEKLY_REPORT_RULES}

本周排名后20%的成员及其关键数据：
{lines}

请为这些末位成员撰写一段"末位管理建议"，要求：
- 逐人给出建议，每人1-2条，必须引用其具体数据（总分、建联数、截图情况等）
- 建议必须可执行（例如：要求某成员下周一到周五每天邀请数达到5位以上并提交建联列表截图）
- 不写"请加强建联"、"建议关注"等空泛措辞
"""
    return _qwen_complete(prompt, max_tokens=700).strip()


def _build_overview_text(perf_list, dates, week_label):
    """生成报告开头的整体数据概况"""
    totals = [m["total"] for m in perf_list]
    contacts = [m["totalContact"] for m in perf_list]
    successes = [m["totalSuccess"] for m in perf_list]
    n = len(perf_list)
    prompt = f"""{WEEKLY_REPORT_RULES}

本周（{week_label}）运营组共{n}人参与评估。
各成员总分：{', '.join(f"{m['name']}{m['total']}" for m in perf_list)}
各成员本周建联总数：{', '.join(f"{m['name']}{m['totalContact']}位" for m in perf_list)}
各成员本周建联成功数：{', '.join(f"{m['name']}{m['totalSuccess']}位" for m in perf_list)}
总分最高{max(totals)}，最低{min(totals)}，平均{sum(totals)/n:.1f}
建联总数最高{max(contacts)}，最低{min(contacts)}，平均{sum(contacts)/n:.1f}

请输出一段"数据概况与现状复盘"，要求：
- 直接列出最大值/最小值/平均值及对应成员姓名
- 标注明显偏离整体的异常项（具体到姓名和数值）
- 不写总结性套话，只陈述数据事实
"""
    return _qwen_complete(prompt, max_tokens=500).strip()


def _perf_report_load(period_key):
    headers = _perf_headers()
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/weekly_perf_reports?period_key=eq.{period_key}&select=*",
        headers=headers, timeout=10
    )
    data = resp.json() if resp.status_code == 200 else []
    if not data:
        return None
    row = data[0]
    if isinstance(row.get("report_data"), str):
        row["report_data"] = json.loads(row["report_data"])
    return row


def _perf_report_save(period_key, period_label, report_data, member_count, exists):
    headers = _perf_headers()
    payload = {
        "period_key": period_key,
        "period_label": period_label,
        "report_data": report_data,
        "member_count": member_count,
    }
    if exists:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/weekly_perf_reports?period_key=eq.{period_key}",
            headers={**headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
            json=payload, timeout=15
        )
    else:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/weekly_perf_reports",
            headers={**headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
            json=payload, timeout=15
        )


@app.route("/api/perf-report/init", methods=["POST"])
def api_perf_report_init():
    """
    第1步：计算所有在职成员的分数（不调用AI），创建/重置报告草稿（status=generating）。
    返回 period_key 和待生成AI文本的成员姓名顺序列表，供前端逐个调用 gen-member。
    body: { monday: 'YYYY-MM-DD' }（可选），force: bool
    """
    try:
        body = request.json or {}
        force = bool(body.get("force", False))

        today = date.today()
        if body.get("monday"):
            monday = date.fromisoformat(body["monday"])
        else:
            monday = today - timedelta(days=today.weekday())

        dates = _perf_week_dates(monday)
        friday = date.fromisoformat(dates[-1])
        period_key = f"week-{monday.isoformat()}"
        period_label = f"{monday.year}年第{monday.isocalendar()[1]}周（{monday.month}月{monday.day}日—{friday.month}月{friday.day}日）"

        existing = _perf_report_load(period_key)
        if existing and not force:
            status = (existing.get("report_data") or {}).get("status")
            if status == "done":
                return jsonify({"ok": True, "skipped": True, "period_key": period_key, "message": "本周报告已存在"})
            # status == 'generating'：允许前端续传/重新走一遍步骤

        today_str = today.isoformat()

        all_rows = _perf_fetch_all_rows()
        checkins_by_date = _perf_fetch_checkins(dates)
        tactics_rows = _perf_fetch_tactics(dates)
        compat_rows = _perf_fetch_compat_scores(dates)

        headers = _perf_headers()
        active_members = list(PERF_CHECKIN_MEMBERS)
        try:
            ms_resp = requests.get(
                f"{SUPABASE_URL}/rest/v1/member_status?select=member_name,status",
                headers=headers, timeout=10
            )
            ms_data = ms_resp.json() if ms_resp.status_code == 200 else []
            inactive = {r["member_name"] for r in ms_data if r.get("status") == "inactive"}
            active_members = [m for m in PERF_CHECKIN_MEMBERS if m["name"] not in inactive]
        except Exception as e:
            print(f"[perf-report] member_status fetch failed: {e}")

        perf_list = []
        for m in active_members:
            perf = calc_member_perf_py(m["name"], dates, today_str, all_rows, checkins_by_date, tactics_rows, compat_rows)
            perf_list.append({"name": m["name"], **perf})
        perf_list.sort(key=lambda x: x["total"], reverse=True)

        total_n = len(perf_list)
        bottom_n = max(1, -(-total_n * 20 // 100))  # ceil(total*0.2)

        members_data = []
        for idx, m in enumerate(perf_list):
            day_breakdown = []
            for d in dates:
                future = d > today_str
                cnt = m["contactDetail"].get(d, 0)
                rec = checkins_by_date.get(d, {}).get(m["name"], {})
                day_breakdown.append({
                    "date": d,
                    "future": future,
                    "cnt": cnt,
                    "chatN": len(rec.get("chat") or []),
                    "blogN": len(rec.get("blogger") or []),
                    "reviewN": len(rec.get("review") or []),
                })
            members_data.append({
                "name": m["name"], "rank": idx + 1, "total": m["total"],
                "dim1": m["dim1"], "dim2": m["dim2"], "dim3": m["dim3"], "bonusTotal": m["bonusTotal"],
                "s_contact_qty": m["s_contact_qty"], "s_table": m["s_table"], "s_type": m["s_type"], "s_fans": m["s_fans"],
                "s_review": m["s_review"], "s_chat": m["s_chat"], "s_blogger_img": m["s_blogger_img"],
                "reachDays": m["reachDays"], "workDays": m["workDays"],
                "totalContact": m["totalContact"], "totalSuccess": m["totalSuccess"],
                "isBottom": idx >= total_n - bottom_n,
                "aiText": None,
                "dayBreakdown": day_breakdown,
            })

        report_data = {
            "period_key": period_key,
            "period_label": period_label,
            "monday": monday.isoformat(),
            "friday": friday.isoformat(),
            "dates": dates,
            "generated_at": today_str,
            "status": "generating",
            "overview_text": None,
            "bottom_advice_text": None,
            "bottom_n": bottom_n,
            "members": members_data,
        }

        _perf_report_save(period_key, period_label, report_data, total_n, exists=bool(existing))

        return jsonify({
            "ok": True,
            "period_key": period_key,
            "period_label": period_label,
            "member_names": [m["name"] for m in members_data],
            "total_steps": total_n + 2,  # N 个成员 + overview + bottom advice
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/perf-report/gen-member", methods=["POST"])
def api_perf_report_gen_member():
    """第2步（逐个调用）：为指定成员生成AI文本（高分维度/问题维度/每日记录解读）"""
    try:
        body = request.json or {}
        period_key = (body.get("period_key") or "").strip()
        member_name = (body.get("member_name") or "").strip()
        if not period_key or not member_name:
            return jsonify({"error": "period_key 和 member_name 不能为空"}), 400

        row = _perf_report_load(period_key)
        if not row:
            return jsonify({"error": "报告不存在，请先调用 init"}), 404
        rd = row["report_data"]

        m = next((x for x in rd["members"] if x["name"] == member_name), None)
        if not m:
            return jsonify({"error": f"成员 {member_name} 不在本周报告中"}), 404

        total_n = len(rd["members"])
        ai_text = _build_member_ai_text(
            m["name"], m, rd["dates"], m["dayBreakdown"], m["rank"], total_n, m["isBottom"]
        )
        m["aiText"] = ai_text

        _perf_report_save(period_key, rd["period_label"], rd, row.get("member_count", total_n), exists=True)
        return jsonify({"ok": True, "member_name": member_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/perf-report/gen-overview", methods=["POST"])
def api_perf_report_gen_overview():
    """第3步：生成"数据概况"AI文本"""
    try:
        body = request.json or {}
        period_key = (body.get("period_key") or "").strip()
        if not period_key:
            return jsonify({"error": "period_key 不能为空"}), 400

        row = _perf_report_load(period_key)
        if not row:
            return jsonify({"error": "报告不存在，请先调用 init"}), 404
        rd = row["report_data"]

        overview_text = _build_overview_text(rd["members"], rd["dates"], rd["period_label"])
        rd["overview_text"] = overview_text

        _perf_report_save(period_key, rd["period_label"], rd, row.get("member_count", len(rd["members"])), exists=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/perf-report/gen-bottom", methods=["POST"])
def api_perf_report_gen_bottom():
    """第4步：生成"末位管理建议"AI文本，并将报告标记为完成"""
    try:
        body = request.json or {}
        period_key = (body.get("period_key") or "").strip()
        if not period_key:
            return jsonify({"error": "period_key 不能为空"}), 400

        row = _perf_report_load(period_key)
        if not row:
            return jsonify({"error": "报告不存在，请先调用 init"}), 404
        rd = row["report_data"]

        bottom_members = [m for m in rd["members"] if m["isBottom"]]
        bottom_summary_lines = [
            f"{m['name']}：总分{m['total']}/100，建联总数{m['totalContact']}位，成功{m['totalSuccess']}位，"
            f"达标天数{m['reachDays']}/{m['workDays']}，截图维度{m['dim2']}/30"
            for m in bottom_members
        ]
        bottom_advice_text = _build_bottom_advice(bottom_summary_lines)
        rd["bottom_advice_text"] = bottom_advice_text
        rd["status"] = "done"

        _perf_report_save(period_key, rd["period_label"], rd, row.get("member_count", len(rd["members"])), exists=True)
        return jsonify({"ok": True, "period_key": period_key, "period_label": rd["period_label"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/perf-report/list", methods=["GET"])
def api_perf_report_list():
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/weekly_perf_reports?select=id,period_key,period_label,member_count,created_at&order=created_at.desc",
            headers=_perf_headers(), timeout=10
        )
        return resp.text, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/perf-report/get", methods=["GET"])
def api_perf_report_get():
    period_key = request.args.get("period_key", "")
    if not period_key:
        return jsonify({"error": "period_key 不能为空"}), 400
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/weekly_perf_reports?period_key=eq.{period_key}&select=*",
            headers=_perf_headers(), timeout=10
        )
        data = resp.json()
        if data:
            row = data[0]
            if isinstance(row.get("report_data"), str):
                row["report_data"] = json.loads(row["report_data"])
            return jsonify(row)
        return jsonify({"error": "not_found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/perf-report/auto-check", methods=["GET"])
def api_perf_report_auto_check():
    """
    供前端在用户打开网站时调用：判断本周是否需要生成报告。
    本身不生成（生成由前端逐步调用 init/gen-member/gen-overview/gen-bottom 完成，
    以便展示真实进度条）；周一~周四不需要生成。
    """
    try:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        period_key = f"week-{monday.isoformat()}"

        if today.weekday() < 4:
            return jsonify({"ok": True, "needGenerate": False, "reason": "本周未过半"})

        row = _perf_report_load(period_key)
        if row and (row.get("report_data") or {}).get("status") == "done":
            return jsonify({"ok": True, "needGenerate": False, "reason": "本周报告已存在", "period_key": period_key})

        return jsonify({
            "ok": True, "needGenerate": True,
            "period_key": period_key, "monday": monday.isoformat(),
            "reason": "本周报告未生成或未完成"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# 飞书数据手动同步
# ============================================================
GH_PAT       = os.environ.get("GH_PAT", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "gummy1803-ai/blogger-tracker")
SYNC_WORKFLOW = os.environ.get("SYNC_WORKFLOW_FILE", "sync.yml")


@app.route("/api/sync-feishu", methods=["POST"])
def api_sync_feishu():
    """
    手动触发飞书 -> Supabase 同步（调用 GitHub Actions workflow_dispatch）。
    需要在 Vercel 环境变量中配置 GH_PAT（具有 repo + workflow 权限的 GitHub PAT）。
    """
    if not GH_PAT:
        return jsonify({"error": "未配置 GH_PAT，无法触发同步"}), 500

    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{SYNC_WORKFLOW}/dispatches"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {GH_PAT}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main"},
            timeout=15,
        )
        if resp.status_code == 204:
            return jsonify({"ok": True, "message": "同步任务已触发，预计1-2分钟内完成"})
        return jsonify({"error": f"GitHub返回 {resp.status_code}: {resp.text[:300]}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync-feishu/status", methods=["GET"])
def api_sync_feishu_status():
    """查询最近一次同步任务的运行状态"""
    if not GH_PAT:
        return jsonify({"error": "未配置 GH_PAT"}), 500
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{SYNC_WORKFLOW}/runs?per_page=1"
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {GH_PAT}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        d = resp.json()
        runs = d.get("workflow_runs", [])
        if not runs:
            return jsonify({"status": "none"})
        r = runs[0]
        return jsonify({
            "status": r.get("status"),       # queued / in_progress / completed
            "conclusion": r.get("conclusion"),  # success / failure / null
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "html_url": r.get("html_url"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)


# ============================================================
# 博主分类功能
# ============================================================

BLOGGER_TYPES = [
    "AI科技", "生活方式", "知识分享", "职场成长", "母婴亲子",
    "美妆护肤", "健康养生", "财经商业", "教育学习", "游戏娱乐",
    "美食餐饮", "旅行户外", "手工DIY", "宠物", "其他"
]

def _classify_headers():
    return {
        "apikey": SUPABASE_ANON,
        "Authorization": f"Bearer {SUPABASE_ANON}",
        "Content-Type": "application/json",
    }

def _ai_classify_batch(bloggers):
    """
    对一批博主调用 qwen 判断类型。
    bloggers: [{"id": ..., "name": ..., "link": ..., "account": ...}, ...]
    返回: {id: "类型"}
    """
    if not bloggers:
        return {}
    lines = "\n".join(
        f"{i+1}. 博主名={b.get('name') or '—'}  账号={b.get('account') or '—'}  链接={b.get('link') or '—'}"
        for i, b in enumerate(bloggers)
    )
    type_list = "、".join(BLOGGER_TYPES)
    prompt = f"""以下是一批博主的基本信息，请根据博主名、账号名和主页链接判断每位博主的内容类型。

可选类型：{type_list}

博主列表：
{lines}

请严格按如下格式返回，每行一条，序号对应上方列表，不要输出其他内容：
1. AI科技
2. 生活方式
...

如果无法判断，填"其他"。"""

    try:
        resp = requests.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={"Authorization": f"Bearer {QWEN_KEY}", "Content-Type": "application/json"},
            json={
                "model": "qwen-turbo",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30,
        )
        text = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        result = {}
        for line in text.strip().splitlines():
            line = line.strip()
            m = __import__("re").match(r"^(\d+)\.\s*(.+)$", line)
            if m:
                idx = int(m.group(1)) - 1
                type_val = m.group(2).strip()
                if 0 <= idx < len(bloggers):
                    # 规范化：只接受预定义类型，否则归为"其他"
                    matched = next((t for t in BLOGGER_TYPES if t in type_val), "其他")
                    result[bloggers[idx]["id"]] = matched
        return result
    except Exception as e:
        print(f"[classify] AI error: {e}")
        return {}


@app.route("/api/blogger-classify/list", methods=["GET"])
def api_blogger_classify_list():
    """
    获取所有建联成功博主的类型分布概况。
    返回: { summary: [{type, count, pct, samples}], unclassified: N, total: N }
    """
    try:
        headers = _classify_headers()
        # 只取建联成功的记录
        success_statuses = ["引导到私域了"]  # sync.py 只把"引导到私域了"写为成功状态
        status_filter = ",".join(f"status.eq.{requests.utils.quote(s, safe="")}" for s in success_statuses)
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bloggers"
            f"?or=({status_filter})"
            f"&is_dup=is.false"
            f"&select=id,name,link,account,blogger_type,followers,owner"
            f"&order=id.asc&limit=2000",
            headers=headers, timeout=20
        )
        rows = resp.json() if resp.status_code == 200 else []
        if not isinstance(rows, list):
            rows = []

        total = len(rows)
        unclassified = sum(1 for r in rows if not r.get("blogger_type") or r["blogger_type"] in ("", "未知"))

        # 按类型聚合
        type_map = {}
        for r in rows:
            t = r.get("blogger_type") or "未分类"
            if t in ("", "未知"):
                t = "未分类"
            type_map.setdefault(t, []).append(r)

        summary = []
        for t, members in sorted(type_map.items(), key=lambda x: -len(x[1])):
            samples = [m.get("name") or m.get("account") or "—" for m in members[:3]]
            summary.append({
                "type": t,
                "count": len(members),
                "pct": round(len(members) / total * 100, 1) if total else 0,
                "samples": samples,
            })

        return jsonify({"summary": summary, "unclassified": unclassified, "total": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/blogger-classify/detail", methods=["GET"])
def api_blogger_classify_detail():
    """
    获取某个类型下的所有建联成功博主。
    ?type=AI科技
    """
    try:
        type_val = request.args.get("type", "").strip()
        if not type_val:
            return jsonify({"error": "type 不能为空"}), 400

        headers = _classify_headers()
        success_statuses = ["引导到私域了"]  # sync.py 只把"引导到私域了"写为成功状态
        status_filter = ",".join(f"status.eq.{requests.utils.quote(s, safe="")}" for s in success_statuses)

        # 把 status 过滤 + 博主类型过滤 合并进同一个 and= 参数避免 or= 冲突
        type_filter = (
            "blogger_type.is.null,blogger_type.eq.,blogger_type.eq.未知"
            if type_val == "未分类"
            else f"blogger_type.eq.{requests.utils.quote(type_val)}"
        )
        # 用 and=(status_filter_wrapped) + type_filter 合并为一个 and 条件
        # Supabase: ?and=(or(status.eq.X,status.eq.Y),is_dup.is.false,<type_filter>)
        if type_val == "未分类":
            combined = (
                f"or({status_filter}),"
                f"is_dup.is.false,"
                f"or(blogger_type.is.null,blogger_type.eq.,blogger_type.eq.未知)"
            )
        else:
            combined = (
                f"or({status_filter}),"
                f"is_dup.is.false,"
                f"blogger_type.eq.{requests.utils.quote(type_val)}"
            )

        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bloggers"
            f"?and=({combined})"
            f"&select=id,name,link,account,blogger_type,followers,owner,record_date"
            f"&order=record_date.desc&limit=500",
            headers=headers, timeout=15
        )
        rows = resp.json() if resp.status_code == 200 else []
        return jsonify(rows if isinstance(rows, list) else [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/blogger-classify/run", methods=["POST"])
def api_blogger_classify_run():
    """
    对未分类的建联成功博主批量调用AI判断类型，每批10个，写回 bloggers 表。
    body: { limit: 50 }  — 本次最多处理几个（防超时）
    返回: { processed: N, updated: N }
    """
    try:
        body = request.json or {}
        limit = min(int(body.get("limit", 50)), 100)

        headers = _classify_headers()
        success_statuses = ["引导到私域了"]  # sync.py 只把"引导到私域了"写为成功状态
        status_filter = ",".join(f"status.eq.{requests.utils.quote(s, safe='')}" for s in success_statuses)

        # 拉未分类的记录
        run_combined = (
            f"or({status_filter}),"
            f"is_dup.is.false,"
            f"or(blogger_type.is.null,blogger_type.eq.,blogger_type.eq.未知)"
        )
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/bloggers"
            f"?and=({run_combined})"
            f"&select=id,name,link,account"
            f"&order=id.asc&limit={limit}",
            headers=headers, timeout=15
        )
        rows = resp.json() if resp.status_code == 200 else []
        if not isinstance(rows, list) or not rows:
            return jsonify({"processed": 0, "updated": 0, "message": "没有需要分类的博主"})

        # 分批10个调用AI
        batch_size = 10
        all_results = {}
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i+batch_size]
            result = _ai_classify_batch(batch)
            all_results.update(result)

        # 写回数据库
        updated = 0
        for row_id, type_val in all_results.items():
            r = requests.patch(
                f"{SUPABASE_URL}/rest/v1/bloggers?id=eq.{row_id}",
                headers={**headers, "Prefer": "return=minimal"},
                json={"blogger_type": type_val},
                timeout=10
            )
            if r.status_code in (200, 204):
                updated += 1

        return jsonify({
            "processed": len(rows),
            "updated": updated,
            "message": f"已处理 {len(rows)} 位博主，成功更新 {updated} 位"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/blogger-classify/update-type", methods=["POST"])
def api_blogger_classify_update_type():
    """手动修正单个博主类型。body: { id, type }"""
    try:
        body = request.json or {}
        row_id = body.get("id")
        type_val = (body.get("type") or "").strip()
        if not row_id or not type_val:
            return jsonify({"error": "id 和 type 不能为空"}), 400
        headers = _classify_headers()
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/bloggers?id=eq.{row_id}",
            headers={**headers, "Prefer": "return=minimal"},
            json={"blogger_type": type_val},
            timeout=10
        )
        return jsonify({"ok": r.status_code in (200, 204)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
