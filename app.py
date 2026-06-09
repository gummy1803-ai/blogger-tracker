import os, re
from collections import defaultdict
from datetime import date, timedelta
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
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
    """通义千问 VL 识别博主截图"""
    body   = request.json or {}
    images = body.get("images", [])   # base64 列表
    if not images:
        return jsonify({"error": "images 不能为空"}), 400

    # 构造多张图片的消息
    content_parts = []
    for img_b64 in images:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
        })
    content_parts.append({
        "type": "text",
        "text": """请识别图片中的博主主页信息，每张图片对应一个博主。
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
                "max_tokens": 2000
            },
            timeout=55
        )
        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not text:
            err_msg = str(data.get("error", {}))
            return jsonify({"error": "识别失败：" + err_msg}), 500
        # 尝试解析 JSON
        import re
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            try:
                result = json.loads(match.group(0))
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
    date = request.args.get("date", "")
    try:
        url = f"{SUPABASE_URL}/rest/v1/checkins?order=checkin_date.desc"
        if date:
            url += f"&checkin_date=eq.{date}"
        else:
            # 默认取最近7天
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            url += f"&checkin_date=gte.{cutoff}"
        resp = requests.get(url, headers={
            "apikey": SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}"
        }, timeout=10)
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
        check = requests.get(
            f"{SUPABASE_URL}/rest/v1/checkins?member_name=eq.{member_name}&checkin_date=eq.{checkin_date}&select=id",
            headers=headers, timeout=10
        )
        existing = check.json()
        payload = {
            "member_name": member_name,
            "checkin_date": checkin_date,
            "chat_images": body.get("chat_images", []),
            "blogger_images": body.get("blogger_images", []),
            "review_images": body.get("review_images", []),
            "submitted": body.get("submitted", False),
        }
        if existing:
            resp = requests.patch(
                f"{SUPABASE_URL}/rest/v1/checkins?member_name=eq.{member_name}&checkin_date=eq.{checkin_date}",
                headers={**headers, "Prefer": "return=representation"},
                json=payload, timeout=10
            )
        else:
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/checkins",
                headers={**headers, "Prefer": "return=representation"},
                json=payload, timeout=10
            )
        if resp.status_code in (200, 201):
            return jsonify(resp.json()[0] if isinstance(resp.json(), list) else resp.json())
        return jsonify({"error": resp.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tactics", methods=["GET"])
def api_tactics_get():
    """获取话术库"""
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/tactics?select=*&order=created_at.desc&limit=200",
            headers={"apikey": SUPABASE_ANON, "Authorization": f"Bearer {SUPABASE_ANON}"},
            timeout=10
        )
        return resp.text, resp.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        return json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"}


@app.route("/api/tactics", methods=["POST"])
def api_tactics_save():
    """保存话术到数据库"""
    body = request.json or {}
    tactics_list = body.get("tactics", [])
    if not tactics_list:
        return jsonify({"error": "tactics 不能为空"}), 400
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
            json=tactics_list,
            timeout=10
        )
        if resp.status_code in (200, 201, 204):
            return jsonify({"ok": True, "count": len(tactics_list)})
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
