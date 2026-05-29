#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
博主建联管理工具 v2
流程：飞书知识库电子表格（每天一个Sheet）→ Supabase → 企微 + 微信播报
"""

import os, sys, requests
from datetime import datetime, timedelta, date
from collections import defaultdict

# ══════════════════════════════════════════
#  环境变量配置
# ══════════════════════════════════════════
FEISHU_APP_ID     = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
WIKI_TOKEN        = os.environ["SHEET_TOKEN"]   # 知识库节点ID：GguhwBZkMiJQsokq1ZYcPebEnfb

SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"]

WECOM_WEBHOOK     = os.environ.get("WECOM_WEBHOOK", "")
SERVERCHAN_KEY    = os.environ.get("SERVERCHAN_KEY", "")

# ══════════════════════════════════════════
#  列映射（A=0 B=1 C=2 D=3 E=4 F=5 G=6）
#  A:数量 B:博主链接 C:回复情况 D:博主名 E:粉丝量(万) F:联系方式 G:跟进人
# ══════════════════════════════════════════
COL = {
    "link":      1,   # B
    "status":    2,   # C
    "name":      3,   # D
    "followers": 4,   # E（万人）
    "owner":     6,   # G
}

MAX_AVG_FOLLOWERS = 5.0
STATUS_SUCCESS  = "引导到私域了"
STATUS_CONTACT  = {"抖音私信建联", "引导到私域了", "没引导到私域"}
W_SUCCESS = 0.40; W_QUALITY = 0.30; W_ACTIVITY = 0.30


# ════════════════════════════════════════
#  飞书 API
# ════════════════════════════════════════

def feishu_token() -> str:
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0: raise RuntimeError(f"飞书授权失败: {d}")
    return d["tenant_access_token"]


def get_spreadsheet_token(token: str, wiki_token: str) -> str:
    """从知识库节点ID获取电子表格Token"""
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token={wiki_token}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0: raise RuntimeError(f"获取wiki节点失败: {d}")
    obj_token = d["data"]["node"]["obj_token"]
    print(f"  电子表格Token: {obj_token}")
    return obj_token


def get_sheet_list(token: str, spreadsheet_token: str) -> list:
    """获取所有Sheet页签列表"""
    url = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0: raise RuntimeError(f"获取Sheet列表失败: {d}")
    return d["data"]["sheets"]


def find_today_sheet(sheets: list, today: date) -> dict | None:
    """找到今天对应的Sheet（格式：M.D，如5.29）"""
    # 生成多种可能的名称格式
    candidates = [
        f"{today.month}.{today.day}",           # 5.29
        f"{today.month:02d}.{today.day:02d}",   # 05.29
        f"{today.month}/{today.day}",            # 5/29
        today.strftime("%m.%d"),                 # 05.29
    ]
    for sheet in sheets:
        title = sheet.get("title", "").strip()
        if title in candidates:
            print(f"  找到今日Sheet: {title} (id={sheet['sheet_id']})")
            return sheet
    print(f"  ⚠️ 未找到今日Sheet，现有页签: {[s['title'] for s in sheets]}")
    return None


def read_sheet_data(token: str, spreadsheet_token: str, sheet_id: str) -> list:
    """读取指定Sheet的数据"""
    range_str = f"{sheet_id}!A3:G500"  # 从第3行开始（跳过标题行和表头）
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_str}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0: raise RuntimeError(f"读取Sheet数据失败: {d}")
    return d.get("data", {}).get("valueRange", {}).get("values", []) or []


# ════════════════════════════════════════
#  数据解析
# ════════════════════════════════════════

def parse_followers(raw) -> float:
    if raw is None: return 0.0
    try: return float(str(raw).strip().replace(",", ""))
    except: return 0.0


def parse_rows(rows: list, record_date: str) -> list:
    records = []
    for row in rows:
        if not row or all(not c for c in row): continue
        def get(i, d=""):
            try: v = row[i]; return str(v).strip() if v is not None else d
            except IndexError: return d

        name  = get(COL["name"])
        link  = get(COL["link"])
        if not (name or link): continue

        # 跳过非博主数据行（如"加微信数量"统计行）
        seq = get(0)
        try: int(seq)
        except: continue

        records.append({
            "link":        link,
            "name":        name,
            "status":      get(COL["status"]),
            "followers":   parse_followers(row[COL["followers"]] if len(row) > COL["followers"] else 0),
            "owner":       get(COL["owner"]) or "未分配",
            "record_date": record_date,
        })
    return records


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

def upsert_bloggers(records: list) -> int:
    if not records: return 0
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/bloggers",
        headers=supa_headers(), json=records, timeout=15)
    if r.status_code not in (200, 201):
        print(f"  ⚠️ Supabase写入错误: {r.status_code} {r.text[:200]}")
        return 0
    return len(records)

def save_daily_report(today: date, records: list, report_text: str):
    cont = sum(1 for r in records if r["status"] in STATUS_CONTACT)
    succ = sum(1 for r in records if r["status"] == STATUS_SUCCESS)
    payload = {
        "report_date": str(today),
        "total": len(records), "contacted": cont, "success": succ,
        "rate": round(succ/cont, 4) if cont else 0,
        "w_total": len(records), "w_contacted": cont, "w_success": succ,
        "w_rate": round(succ/cont, 4) if cont else 0,
        "report_text": report_text,
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/daily_reports",
                      headers=supa_headers(), json=payload, timeout=10)
    print(f"  日报快照: {r.status_code}")


# ════════════════════════════════════════
#  统计 & 考评
# ════════════════════════════════════════

def calc_stats(rows):
    bk = defaultdict(lambda: {"total":0,"contacted":0,"success":0,"fans":[]})
    for r in rows:
        o = r["owner"]
        bk[o]["total"] += 1
        if r["status"] in STATUS_CONTACT: bk[o]["contacted"] += 1
        if r["status"] == STATUS_SUCCESS:  bk[o]["success"]  += 1
        if r["followers"] > 0: bk[o]["fans"].append(r["followers"])
    res = {}
    for o, b in bk.items():
        af = sum(b["fans"])/len(b["fans"]) if b["fans"] else 0
        c  = b["contacted"]
        res[o] = {**b, "avg_fans": af, "quality_ok": af<=MAX_AVG_FOLLOWERS,
                  "rate": b["success"]/c if c else 0}
    return res

def calc_kpi(stats):
    avgs = [s["contacted"] for s in stats.values()]
    team = sum(avgs)/len(avgs) if avgs else 1
    out  = {}
    for o, s in stats.items():
        act  = min(s["contacted"]/max(team,1), 2.0)/2.0
        qual = 1.0 if s["quality_ok"] else max(0, 1-(s["avg_fans"]-MAX_AVG_FOLLOWERS)/MAX_AVG_FOLLOWERS)
        sc   = round((s["rate"]*W_SUCCESS + qual*W_QUALITY + act*W_ACTIVITY)*100, 1)
        out[o] = {"score": sc, "grade": "A"if sc>=80 else"B"if sc>=60 else"C"if sc>=40 else"D"}
    return out

def fmt_rate(n, d): return f"{n/d*100:.1f}%" if d else "—"
def fmt_f(f): return f"{f:.1f}万"
def sums(s): return (sum(v["total"] for v in s.values()),
                     sum(v["contacted"] for v in s.values()),
                     sum(v["success"] for v in s.values()))

def build_report(stats, kpi, today: date, total_records: int) -> str:
    dl = today.strftime("%m月%d日")
    tt, tc, ts = sums(stats)
    grade_emoji = {"A":"⭐","B":"✅","C":"⚠️","D":"❌"}
    lines = [
        f"📊 博主建联日报 · {dl}",
        "━"*24,
        f"  今日录入博主：{total_records} 人",
        f"  已联系：{tc} 人",
        f"  引导私域成功：{ts} 人",
        f"  今日转化率：{fmt_rate(ts,tc)}",
        "",
        "👥 跟进人进度 & 考评",
        "━"*24,
    ]
    for o in sorted(stats, key=lambda x: stats[x]["contacted"], reverse=True):
        s = stats[o]; k = kpi.get(o, {"score":0,"grade":"D"})
        q = "✅ 达标" if s["quality_ok"] else f"⚠️ 超标（均{fmt_f(s['avg_fans'])}）"
        lines += [
            f"👤 {o}  {grade_emoji.get(k['grade'],'')} {k['grade']}级 {k['score']}分",
            f"  联系{s['contacted']}人 · 成功{s['success']}人 · {fmt_rate(s['success'],s['contacted'])}",
            f"  均粉：{fmt_f(s['avg_fans'])}  {q}", "",
        ]
    lines += ["━"*24, "A≥80 B≥60 C≥40 D<40 | 转化40%+质量30%+活跃30%"]
    return "\n".join(lines)


# ════════════════════════════════════════
#  推送
# ════════════════════════════════════════

def push_wecom(content):
    if not WECOM_WEBHOOK: return
    r = requests.post(WECOM_WEBHOOK, json={"msgtype":"text","text":{"content":content}}, timeout=10)
    print(f"  [企微] {r.status_code}")

def push_serverchan(title, content):
    if not SERVERCHAN_KEY: return
    r = requests.post(f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
                      data={"title":title,"desp":content.replace("\n","\n\n")}, timeout=10)
    print(f"  [Server酱] {r.status_code}")


# ════════════════════════════════════════
#  主入口
# ════════════════════════════════════════

def main():
    today = date.today()
    print(f"▶ 开始同步  {today}")

    # 1. 飞书认证
    token = feishu_token()
    print("  ✓ 飞书认证成功")

    # 2. 知识库节点 → 电子表格Token
    spreadsheet_token = get_spreadsheet_token(token, WIKI_TOKEN)

    # 3. 获取所有Sheet，找今天的
    sheets = get_sheet_list(token, spreadsheet_token)
    today_sheet = find_today_sheet(sheets, today)

    if not today_sheet:
        print(f"  ⚠️ 今日({today.month}.{today.day})无对应Sheet，退出")
        sys.exit(0)

    # 4. 读取今日数据
    rows = read_sheet_data(token, spreadsheet_token, today_sheet["sheet_id"])
    records = parse_rows(rows, str(today))
    print(f"  今日有效记录: {len(records)} 条")

    if not records:
        print("  ⚠️ 今日无数据"); sys.exit(0)

    # 5. 写入 Supabase
    written = upsert_bloggers(records)
    print(f"  Supabase upsert: {written} 条")

    # 6. 统计 & 考评 & 报告
    stats  = calc_stats(records)
    kpi    = calc_kpi(stats)
    report = build_report(stats, kpi, today, len(records))
    print("\n" + "="*40 + "\n" + report + "\n" + "="*40)

    # 7. 保存日报 & 推送
    save_daily_report(today, records, report)
    push_wecom(report)
    push_serverchan(f"博主建联日报·{today.strftime('%m/%d')}", report)
    print("✓ 完成")


if __name__ == "__main__":
    main()
