#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
博主建联管理工具 v8
新增：
- 平台自动识别（抖音/视频号/小红书/知乎/微博）
- 博主类型读取（K列）
- 质量评分读取（I列，每跟进人区块第一行）
- 回访机制（两天无回复标待回访，四天标可放弃）
- 新考评公式：转化率40% + 上级质量评分30% + 活跃度30%
- 重复数据不计入工作量（H列标注"已重复"的行跳过）
- 重复未处理扣活跃度分
- 日报新增：状态汇总、平台分布、回访名单、AI博主类型总结
"""

import os, sys, requests, re
from datetime import datetime, timedelta, date
from collections import defaultdict

# ══════════════════════════════════════════
#  配置
# ══════════════════════════════════════════
FEISHU_APP_ID        = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET    = os.environ["FEISHU_APP_SECRET"]
FEISHU_USER_TOKEN    = os.environ["FEISHU_USER_TOKEN"]
FEISHU_REFRESH_TOKEN = os.environ["FEISHU_REFRESH_TOKEN"]
WIKI_TOKEN           = os.environ["WIKI_TOKEN"]

SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_KEY         = os.environ["SUPABASE_SERVICE_KEY"]
SERVERCHAN_KEY       = os.environ.get("SERVERCHAN_KEY", "")
WECOM_WEBHOOK        = os.environ.get("WECOM_WEBHOOK", "")
DEEPSEEK_API_KEY     = os.environ.get("DEEPSEEK_API_KEY", "")

GITHUB_TOKEN         = os.environ["GITHUB_TOKEN"]
GITHUB_REPO          = "gummy1803-ai/blogger-tracker"

# 列索引（0-based，A=0）
COL_SEQ       = 0   # A: 数量
COL_LINK      = 1   # B: 博主链接
COL_STATUS    = 2   # C: 回复情况
COL_NAME      = 3   # D: 博主名
COL_FOLLOWERS = 4   # E: 粉丝量
COL_ACCOUNT   = 5   # F: 博主号
COL_OWNER     = 6   # G: 跟进人
COL_DUP       = 7   # H: 查重结果
COL_QUALITY   = 8   # I: 质量评分（每跟进人区块第一行）
COL_NOTE      = 9   # J: 备注
COL_TYPE      = 10  # K: 博主类型

STATUS_SUCCESS = "引导到私域了"
STATUS_CONTACT = {"抖音私信建联", "小红书私信建联", "微信建联", "微信私信建联", "引导到私域了", "没引导到私域"}
STATUS_NO_REPLY = {"没回", "未回"}

W_SUCCESS  = 0.40
W_QUALITY  = 0.30
W_ACTIVITY = 0.30

REVISIT_DAYS  = 2   # 两天无回复 → 待回访
ABANDON_DAYS  = 4   # 四天无回复 → 可放弃


# ════════════════════════════════════════
#  GitHub Secrets 更新
# ════════════════════════════════════════

def get_repo_public_key():
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/public-key",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "X-GitHub-Api-Version": "2022-11-28"})
    return r.json()

def encrypt_secret(public_key_b64, secret_value):
    from base64 import b64encode
    from nacl import encoding, public
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder)
    box = public.SealedBox(pk)
    return b64encode(box.encrypt(secret_value.encode())).decode()

def update_github_secret(secret_name, secret_value):
    try:
        pk_data   = get_repo_public_key()
        key       = pk_data.get("key", "")
        key_id    = pk_data.get("key_id", "")
        if not key:
            print(f"  ⚠️ 公钥为空，跳过 {secret_name}")
            return
        encrypted = encrypt_secret(key, secret_value)
        r = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/{secret_name}",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "X-GitHub-Api-Version": "2022-11-28"},
            json={"encrypted_value": encrypted, "key_id": key_id})
        print(f"  {'✓' if r.status_code in (201,204) else '⚠️'} Secret {secret_name}: {r.status_code}")
    except Exception as e:
        print(f"  ⚠️ Secret更新异常: {e}")


# ════════════════════════════════════════
#  飞书 Token 刷新
# ════════════════════════════════════════

def get_app_token():
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
    return r.json().get("app_access_token", "")

def refresh_user_token():
    app_token = get_app_token()
    r = requests.post(
        "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token",
        headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
        json={"grant_type": "refresh_token", "refresh_token": FEISHU_REFRESH_TOKEN},
        timeout=10)
    d = r.json()
    if d.get("code") == 0:
        data = d["data"]
        new_user_token    = data["access_token"]
        new_refresh_token = data["refresh_token"]
        print(f"  ✓ Token刷新成功，有效期 {data.get('expires_in',0)//3600} 小时")
        update_github_secret("FEISHU_USER_TOKEN",    new_user_token)
        update_github_secret("FEISHU_REFRESH_TOKEN", new_refresh_token)
        return new_user_token
    else:
        print(f"  ⚠️ Token刷新失败({d.get('code')}): {d.get('message','')}, 使用原Token")
        return FEISHU_USER_TOKEN


# ════════════════════════════════════════
#  飞书数据读取
# ════════════════════════════════════════

def get_spreadsheet_token(user_token):
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token={WIKI_TOKEN}"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"获取wiki节点失败: {d.get('msg','')}")
    return d["data"]["node"]["obj_token"]

def get_all_sheets(user_token, spreadsheet_token):
    url = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"获取sheet列表失败: {d}")
    return d["data"]["sheets"]

def sheet_title_to_date(title):
    title = title.strip()
    m = re.match(r'^(\d{1,2})\.(\d{1,2})$', title)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        return str(date(date.today().year, month, day))
    except ValueError:
        return None

def read_sheet_data(user_token, spreadsheet_token, sheet_id):
    range_str = f"{sheet_id}!A3:K500"
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_str}"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"读取数据失败: {d}")
    return d.get("data", {}).get("valueRange", {}).get("values", []) or []


# ════════════════════════════════════════
#  平台识别
# ════════════════════════════════════════

def detect_platform(link, account):
    """根据链接或账号判断平台"""
    src = (link + " " + account).lower()
    if "douyin.com" in src or "v.douyin" in src or "dy" in src:
        return "抖音"
    if "channels.weixin" in src or "视频号" in src or "sph." in src:
        return "视频号"
    if "xiaohongshu.com" in src or "xhs" in src or "小红书" in src:
        return "小红书"
    if "zhihu.com" in src or "知乎" in src:
        return "知乎"
    if "weibo.com" in src or "weibo" in src or "微博" in src:
        return "微博"
    if "bilibili.com" in src or "b站" in src:
        return "B站"
    return "其他"


# ════════════════════════════════════════
#  数据解析
# ════════════════════════════════════════

def parse_followers(raw):
    if raw is None:
        return 0.0
    s = str(raw).strip().replace(",", "")
    m = re.match(r"([\d.]+)\s*[万wW]", s)
    if m:
        return float(m.group(1)) * 10000
    try:
        return float(s)
    except:
        return 0.0

def get_cell(row, idx, default=""):
    try:
        v = row[idx]
        return str(v).strip() if v is not None else default
    except IndexError:
        return default

def parse_rows(rows, record_date):
    """
    解析一个工作表的行数据
    同时提取质量评分（I列，每跟进人区块第一行有值）
    返回 (records, owner_quality_map)
    """
    records = []
    owner_quality_map = {}   # owner → quality_score
    current_owner = ""

    for i, row in enumerate(rows):
        if not row or all(not c for c in row):
            continue

        # 序号校验（A列必须是数字）
        try:
            int(float(get_cell(row, COL_SEQ)))
        except:
            continue

        name    = get_cell(row, COL_NAME)
        link    = get_cell(row, COL_LINK)
        if not name and not link:
            continue

        owner   = get_cell(row, COL_OWNER) or "未分配"
        status  = get_cell(row, COL_STATUS)
        account = get_cell(row, COL_ACCOUNT)
        dup     = get_cell(row, COL_DUP)
        note    = get_cell(row, COL_NOTE)
        btype   = get_cell(row, COL_TYPE)

        # 质量评分：跟进人变化时读取I列
        if owner != current_owner:
            current_owner = owner
            q_raw = get_cell(row, COL_QUALITY)
            if q_raw:
                try:
                    owner_quality_map[owner] = float(q_raw)
                except:
                    pass

        is_dup = "已重复" in dup

        records.append({
            "link":        link,
            "name":        name,
            "status":      status,
            "followers":   parse_followers(get_cell(row, COL_FOLLOWERS)),
            "account":     account,
            "owner":       owner,
            "record_date": record_date,
            "platform":    detect_platform(link, account),
            "blogger_type": btype,
            "note":        note,
            "is_dup":      is_dup,
        })

    return records, owner_quality_map


# ════════════════════════════════════════
#  回访逻辑
# ════════════════════════════════════════

def check_revisit(all_records, today):
    """
    扫描所有记录，找出需要回访和可放弃的博主
    返回 (待回访列表, 可放弃列表)
    """
    # 按 link/account 分组，找最后一次记录日期
    blogger_last = {}
    for r in all_records:
        key = r.get("link") or r.get("account") or r.get("name")
        if not key:
            continue
        if r["status"] in STATUS_NO_REPLY:
            d = r["record_date"]
            if key not in blogger_last or d > blogger_last[key]["date"]:
                blogger_last[key] = {
                    "date":   d,
                    "name":   r["name"],
                    "owner":  r["owner"],
                    "status": r["status"],
                }
        elif r["status"] == STATUS_SUCCESS:
            # 已成功，移出待回访
            blogger_last.pop(key, None)

    revisit = []
    abandon  = []
    for key, info in blogger_last.items():
        try:
            last_date = date.fromisoformat(info["date"])
        except:
            continue
        days_ago = (today - last_date).days
        if days_ago >= ABANDON_DAYS:
            abandon.append({**info, "days": days_ago, "key": key})
        elif days_ago >= REVISIT_DAYS:
            revisit.append({**info, "days": days_ago, "key": key})

    return revisit, abandon


# ════════════════════════════════════════
#  Supabase
# ════════════════════════════════════════

def supa_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }

def upsert_bloggers(records):
    if not records:
        return 0
    # 去掉内部字段 is_dup
    payload = [{k: v for k, v in r.items() if k != "is_dup"} for r in records]
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/bloggers",
        headers=supa_headers(), json=payload, timeout=15)
    if r.status_code not in (200, 201):
        print(f"  ⚠️ Supabase错误: {r.status_code} {r.text[:200]}")
        return 0
    return len(payload)

def save_daily_report(report_date, records, report_text):
    cont = sum(1 for r in records if r["status"] in STATUS_CONTACT)
    succ = sum(1 for r in records if r["status"] == STATUS_SUCCESS)
    payload = {
        "report_date":  str(report_date),
        "total":        len(records),
        "contacted":    cont,
        "success":      succ,
        "rate":         round(succ / cont, 4) if cont else 0,
        "w_total":      len(records),
        "w_contacted":  cont,
        "w_success":    succ,
        "w_rate":       round(succ / cont, 4) if cont else 0,
        "report_text":  report_text,
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/daily_reports",
        headers=supa_headers(), json=payload, timeout=10)
    print(f"  日报快照: {r.status_code}")


# ════════════════════════════════════════
#  统计 & 考评
# ════════════════════════════════════════

def calc_stats(records, owner_quality_map):
    """
    records: 昨日数据，is_dup=True 的不计入工作量
    owner_quality_map: 上级质量评分
    """
    bk = defaultdict(lambda: {
        "total": 0, "valid": 0, "contacted": 0, "success": 0,
        "fans": [], "dup_count": 0
    })
    for r in records:
        o = r["owner"]
        bk[o]["total"] += 1
        if r["is_dup"]:
            bk[o]["dup_count"] += 1
            continue   # 重复数据不计入工作量
        bk[o]["valid"] += 1
        if r["status"] in STATUS_CONTACT:
            bk[o]["contacted"] += 1
        if r["status"] == STATUS_SUCCESS:
            bk[o]["success"] += 1
        if r["followers"] > 0:
            bk[o]["fans"].append(r["followers"])

    res = {}
    for o, b in bk.items():
        af = sum(b["fans"]) / len(b["fans"]) if b["fans"] else 0
        c  = b["contacted"]
        dup_rate = b["dup_count"] / b["total"] if b["total"] else 0
        res[o] = {
            **b,
            "avg_fans":    round(af / 10000, 2),
            "rate":        round(b["success"] / c, 4) if c else 0,
            "dup_rate":    round(dup_rate, 4),
            "quality_score": owner_quality_map.get(o, None),
        }
    return res

def calc_kpi(stats):
    avgs     = [s["contacted"] for s in stats.values()]
    team_avg = sum(avgs) / len(avgs) if avgs else 1
    out      = {}
    for o, s in stats.items():
        # 活跃度
        act = min(s["contacted"] / max(team_avg, 1), 2.0) / 2.0
        # 重复率扣分（每超10%扣10%活跃度）
        dup_penalty = min(int(s["dup_rate"] * 10) * 0.10, 0.50)
        act = max(0, act - dup_penalty)

        # 上级质量评分（满分100，转换为0-1）
        q_score = s["quality_score"]
        if q_score is not None:
            qual = q_score / 100.0
        else:
            qual = 0.6   # 未打分默认给60分

        sc    = round((s["rate"] * W_SUCCESS + qual * W_QUALITY + act * W_ACTIVITY) * 100, 1)
        grade = "A" if sc >= 80 else "B" if sc >= 60 else "C" if sc >= 40 else "D"
        out[o] = {"score": sc, "grade": grade, "dup_penalty": round(dup_penalty * 100, 1)}
    return out


# ════════════════════════════════════════
#  AI 博主类型总结
# ════════════════════════════════════════

def ai_summarize_types(success_records):
    """用 DeepSeek 总结建联成功的博主类型规律"""
    if not DEEPSEEK_API_KEY:
        return ""
    if not success_records:
        return "昨日暂无建联成功记录。"

    types = [r.get("blogger_type", "") for r in success_records if r.get("blogger_type")]
    platforms = [r.get("platform", "") for r in success_records]
    fans = [r.get("followers", 0) / 10000 for r in success_records if r.get("followers", 0) > 0]

    prompt = f"""以下是昨日建联成功的博主数据，请用2-3句话总结规律，语言简洁：
平台分布：{dict_count(platforms)}
博主类型：{types if types else '未填写'}
粉丝均值：{round(sum(fans)/len(fans),1) if fans else 0}万
总计：{len(success_records)}人"""

    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
            },
            timeout=15,
        )
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  ⚠️ AI总结失败: {e}")
        return ""

def dict_count(lst):
    d = {}
    for x in lst:
        d[x] = d.get(x, 0) + 1
    return d


# ════════════════════════════════════════
#  日报生成
# ════════════════════════════════════════

def fmt_rate(n, d):
    return f"{n/d*100:.1f}%" if d else "—"

def fmt_f(f):
    return f"{f:.1f}万"

def sums(s):
    return (
        sum(v["valid"] for v in s.values()),
        sum(v["contacted"] for v in s.values()),
        sum(v["success"] for v in s.values()),
    )

def build_report(stats, kpi, records, report_date, revisit, abandon, ai_summary):
    dl  = report_date.strftime("%m月%d日")
    tt, tc, ts = sums(stats)
    total_raw  = sum(v["total"] for v in stats.values())
    total_dup  = sum(v["dup_count"] for v in stats.values())
    ge = {"A": "⭐", "B": "✅", "C": "⚠️", "D": "❌"}

    # 状态汇总
    status_count = defaultdict(int)
    platform_count = defaultdict(int)
    for r in records:
        if not r["is_dup"]:
            status_count[r["status"] or "未填写"] += 1
            platform_count[r["platform"]] += 1

    # 有效回复率（有回复的/总联系）
    replied = sum(1 for r in records if not r["is_dup"] and r["status"] not in STATUS_NO_REPLY and r["status"])
    valid_reply_rate = fmt_rate(replied, tt) if tt else "—"

    lines = [
        f"📊 博主建联日报 · {dl}",
        "━" * 24,
        f"  录入博主：{total_raw} 人",
    ]
    if total_dup:
        lines.append(f"  ⚠️ 重复数据：{total_dup} 条（已排除）")
    lines += [
        f"  有效博主：{tt} 人",
        f"  已联系：{tc} 人",
        f"  引导私域成功：{ts} 人",
        f"  转化率：{fmt_rate(ts, tc)}",
        f"  有效回复率：{valid_reply_rate}",
        "",
        "📱 平台分布",
    ]
    for p, cnt in sorted(platform_count.items(), key=lambda x: -x[1]):
        lines.append(f"  {p}：{cnt} 人")

    lines += ["", "📋 博主状态汇总"]
    for s, cnt in sorted(status_count.items(), key=lambda x: -x[1]):
        lines.append(f"  {s}：{cnt} 人")

    if ai_summary:
        lines += ["", "🤖 建联成功博主画像", f"  {ai_summary}"]

    lines += ["", "👥 跟进人进度 & 考评", "━" * 24]

    for o in sorted(stats, key=lambda x: stats[x]["contacted"], reverse=True):
        s = stats[o]
        k = kpi.get(o, {"score": 0, "grade": "D", "dup_penalty": 0})
        q_str = f"{s['quality_score']}分" if s["quality_score"] is not None else "待打分"
        dup_str = f"⚠️ 重复{s['dup_count']}条 扣活跃{k['dup_penalty']}%" if s["dup_count"] else ""

        lines += [
            f"{ge.get(k['grade'],'')} {o}  [{k['grade']}级 {k['score']}分]",
            f"  有效{s['valid']}人 · 联系{s['contacted']}人 · 成功{s['success']}人 · {fmt_rate(s['success'], s['contacted'])}",
            f"  均粉：{fmt_f(s['avg_fans'])}  上级评分：{q_str}",
        ]
        if dup_str:
            lines.append(f"  {dup_str}")
        lines.append("")

    # 回访提醒
    if revisit:
        lines += ["⏰ 待二次回访（两天无回复）"]
        for r in revisit[:10]:
            lines.append(f"  {r['owner']} · {r['name']} · 已{r['days']}天无回复")
        lines.append("")

    if abandon:
        lines += ["🚫 建议放弃（四天无回复）"]
        for r in abandon[:10]:
            lines.append(f"  {r['owner']} · {r['name']} · 已{r['days']}天无回复")
        lines.append("")

    lines += [
        "━" * 24,
        "A≥80 B≥60 C≥40 D<40",
        "转化40% + 上级评分30% + 活跃30%",
        "重复率每超10%扣10%活跃度",
    ]
    return "\n".join(lines)


# ════════════════════════════════════════
#  推送
# ════════════════════════════════════════

def push_wecom(content):
    if not WECOM_WEBHOOK:
        return
    r = requests.post(
        WECOM_WEBHOOK,
        json={"msgtype": "text", "text": {"content": content}},
        timeout=10)
    print(f"  [企微] {r.status_code}")

def push_serverchan(title, content):
    if not SERVERCHAN_KEY:
        return
    r = requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
        data={"title": title, "desp": content.replace("\n", "\n\n")},
        timeout=10)
    print(f"  [Server酱] {r.status_code}")


# ════════════════════════════════════════
#  主入口
# ════════════════════════════════════════

def main():
    today     = date.today()
    yesterday = today - timedelta(days=1)
    print(f"▶ 开始同步  今天:{today}  日报日期:{yesterday}")

    user_token        = refresh_user_token()
    spreadsheet_token = get_spreadsheet_token(user_token)
    all_sheets        = get_all_sheets(user_token, spreadsheet_token)
    print(f"  共找到 {len(all_sheets)} 个 sheet")

    all_records       = []
    yesterday_records = []
    all_quality_map   = {}

    for sheet in all_sheets:
        title       = sheet.get("title", "").strip()
        record_date = sheet_title_to_date(title)
        if not record_date:
            print(f"  跳过: {title}")
            continue

        rows = read_sheet_data(user_token, spreadsheet_token, sheet["sheet_id"])
        records, quality_map = parse_rows(rows, record_date)

        if not records:
            print(f"  {title} → 无有效数据")
            continue

        print(f"  {title} → {record_date}: {len(records)} 条")
        all_records.extend(records)
        all_quality_map.update(quality_map)

        if record_date == str(yesterday):
            yesterday_records = records

    # 全量写入 Supabase
    written = upsert_bloggers(all_records)
    print(f"  Supabase 总写入: {written} 条")

    if not yesterday_records:
        print(f"  ⚠️ 昨日（{yesterday}）无数据，跳过日报推送")
        sys.exit(0)

    # 回访检查（基于全量历史数据）
    revisit, abandon = check_revisit(all_records, today)
    print(f"  待回访: {len(revisit)} 人  可放弃: {len(abandon)} 人")

    # 统计 & 考评
    stats = calc_stats(yesterday_records, all_quality_map)
    kpi   = calc_kpi(stats)

    # AI 总结
    success_records = [r for r in yesterday_records if r["status"] == STATUS_SUCCESS]
    ai_summary      = ai_summarize_types(success_records)

    # 生成日报
    report = build_report(stats, kpi, yesterday_records, yesterday, revisit, abandon, ai_summary)
    print("\n" + "=" * 40 + "\n" + report + "\n" + "=" * 40)

    save_daily_report(yesterday, yesterday_records, report)
    push_wecom(report)
    push_serverchan(f"博主建联日报·{yesterday.strftime('%m/%d')}", report)
    print("✓ 完成")

if __name__ == "__main__":
    main()
