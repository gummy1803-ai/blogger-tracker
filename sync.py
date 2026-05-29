#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
博主建联管理工具 v2
流程：飞书 Sheet → Supabase → 企微 + 微信播报
"""

import os, sys, json, requests
from datetime import datetime, timedelta, date
from collections import defaultdict

# ══════════════════════════════════════════
#  环境变量配置
# ══════════════════════════════════════════
FEISHU_APP_ID     = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
SHEET_TOKEN       = os.environ["SHEET_TOKEN"]
SHEET_ID          = os.environ.get("SHEET_ID", "Sheet1")

SUPABASE_URL      = os.environ["SUPABASE_URL"]          # https://xxxx.supabase.co
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"]  # service_role key（可写）

WECOM_WEBHOOK     = os.environ.get("WECOM_WEBHOOK", "")
SERVERCHAN_KEY    = os.environ.get("SERVERCHAN_KEY", "")

# ══════════════════════════════════════════
#  列映射（对应实际飞书表格）
#  A=数量  B=博主链接  C=回复情况  D=博主名
#  E=粉丝量(万)  F=联系方式  G=跟进人  H=录入日期
# ══════════════════════════════════════════
COL = {
    "link":      1,   # B
    "status":    2,   # C
    "name":      3,   # D
    "followers": 4,   # E（单位：万人）
    "owner":     6,   # G
    "date":      7,   # H
}

MAX_AVG_FOLLOWERS = 5.0
STATUS_SUCCESS  = "引导到私域了"
STATUS_CONTACT  = {"抖音私信建联", "引导到私域了"}

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


def read_feishu(token: str) -> list:
    url = (f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets"
           f"/{SHEET_TOKEN}/values/{SHEET_ID}!A2:H2000")
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0: raise RuntimeError(f"读取表格失败: {d}")
    return d.get("data", {}).get("valueRange", {}).get("values", []) or []


# ════════════════════════════════════════
#  数据解析
# ════════════════════════════════════════

def parse_followers(raw) -> float:
    if raw is None: return 0.0
    try: return float(str(raw).strip().replace(",", ""))
    except: return 0.0


def parse_rows(rows: list) -> list:
    records = []
    for row in rows:
        if not row or all(not c for c in row): continue
        def get(i, d=""):
            try: v = row[i]; return str(v).strip() if v is not None else d
            except IndexError: return d

        name  = get(COL["name"])
        link  = get(COL["link"])
        owner = get(COL["owner"]) or "未分配"
        rdate = get(COL["date"])

        # 日期格式校验
        try: datetime.strptime(rdate, "%Y-%m-%d")
        except: rdate = None

        if not (name or link): continue

        records.append({
            "link":        link,
            "name":        name,
            "status":      get(COL["status"]),
            "followers":   parse_followers(row[COL["followers"]] if len(row) > COL["followers"] else 0),
            "owner":       owner,
            "record_date": rdate,
        })
    return records


# ════════════════════════════════════════
#  Supabase API
# ════════════════════════════════════════

def supa_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",  # upsert
    }


def upsert_bloggers(records: list) -> int:
    """批量 upsert 博主记录，返回写入条数"""
    if not records: return 0
    # 过滤掉没有日期的记录（无法 upsert）
    valid = [r for r in records if r["record_date"]]
    if not valid: return 0

    # 分批（Supabase 单次建议 ≤500 条）
    batch = 500
    written = 0
    for i in range(0, len(valid), batch):
        chunk = valid[i:i+batch]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/bloggers",
            headers=supa_headers(),
            json=chunk,
            timeout=15,
        )
        if r.status_code not in (200, 201):
            print(f"  ⚠️ Supabase upsert 错误: {r.status_code} {r.text[:200]}")
        else:
            written += len(chunk)
    return written


def save_daily_report(today: date, today_rows, week_rows, report_text: str):
    """将今日日报快照写入 daily_reports 表"""
    def counts(rows):
        cont = sum(1 for r in rows if r["status"] in STATUS_CONTACT)
        succ = sum(1 for r in rows if r["status"] == STATUS_SUCCESS)
        return len(rows), cont, succ

    t_total, t_cont, t_succ = counts(today_rows)
    w_total, w_cont, w_succ = counts(week_rows)

    payload = {
        "report_date": str(today),
        "total":       t_total, "contacted":   t_cont,  "success":   t_succ,
        "rate":        round(t_succ/t_cont, 4) if t_cont else 0,
        "w_total":     w_total, "w_contacted": w_cont,  "w_success": w_succ,
        "w_rate":      round(w_succ/w_cont, 4) if w_cont else 0,
        "report_text": report_text,
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/daily_reports",
        headers=supa_headers(),
        json=payload,
        timeout=10,
    )
    if r.status_code not in (200, 201):
        print(f"  ⚠️ 日报快照写入失败: {r.status_code} {r.text[:200]}")
    else:
        print("  ✓ 日报快照已写入 Supabase")


# ════════════════════════════════════════
#  统计 & 考评
# ════════════════════════════════════════

def filter_date(records, target: date):
    return [r for r in records if r["record_date"] == str(target)]

def filter_week(records, today: date):
    ws = str(today - timedelta(days=today.weekday()))
    return [r for r in records if r["record_date"] and ws <= r["record_date"] <= str(today)]

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


# ════════════════════════════════════════
#  报告生成
# ════════════════════════════════════════

def fmt_rate(n, d): return f"{n/d*100:.1f}%" if d else "—"
def fmt_f(f): return f"{f:.1f}万"
def sums(s): return sum(v["total"] for v in s.values()), sum(v["contacted"] for v in s.values()), sum(v["success"] for v in s.values())

def build_report(today_stats, week_stats, kpi, today: date) -> str:
    dl = today.strftime("%m月%d日")
    ws = (today-timedelta(days=today.weekday())).strftime("%m/%d")
    we = today.strftime("%m/%d")
    tt,tc,ts = sums(today_stats); wt,wc,ws2 = sums(week_stats)
    grade_emoji = {"A":"⭐","B":"✅","C":"⚠️","D":"❌"}

    lines = [
        f"📊 博主建联日报 · {dl}",
        "━"*24,
        "📅 今日总览",
        f"  录入博主：{tt} 人",
        f"  已联系：{tc} 人",
        f"  引导私域成功：{ts} 人",
        f"  今日转化率：{fmt_rate(ts,tc)}",
        "",
        f"📆 本周累计（{ws} ~ {we}）",
        f"  录入：{wt} 人  联系：{wc} 人  成功：{ws2} 人",
        f"  周转化率：{fmt_rate(ws2,wc)}",
        "",
        "👥 跟进人进度 & 考评",
        "━"*24,
    ]
    for o in sorted(today_stats, key=lambda x: today_stats[x]["contacted"], reverse=True):
        ds = today_stats[o]; ws3 = week_stats.get(o,{"contacted":0,"success":0}); k = kpi.get(o,{"score":0,"grade":"D"})
        q = "✅ 达标" if ds["quality_ok"] else f"⚠️ 超标（均{fmt_f(ds['avg_fans'])}）"
        lines += [
            f"👤 {o}    {grade_emoji.get(k['grade'],'')} {k['grade']}级  {k['score']}分",
            f"  今日：联系{ds['contacted']}人 · 成功{ds['success']}人 · {fmt_rate(ds['success'],ds['contacted'])}",
            f"  本周：联系{ws3['contacted']}人 · 成功{ws3['success']}人",
            f"  博主均粉：{fmt_f(ds['avg_fans'])}  {q}","",
        ]
    lines += ["━"*24,"📌 A≥80 B≥60 C≥40 D<40  |  转化40%+质量30%+活跃30%"]
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

    # 1. 从飞书读取
    token   = feishu_token()
    rows    = read_feishu(token)
    records = parse_rows(rows)
    print(f"  飞书读取: {len(records)} 条有效记录")

    # 2. 写入 Supabase
    written = upsert_bloggers(records)
    print(f"  Supabase upsert: {written} 条")

    if not records:
        print("  ⚠️ 无数据，跳过报告"); sys.exit(0)

    # 3. 统计 & 考评
    today_rows  = filter_date(records, today)
    week_rows   = filter_week(records, today)
    today_stats = calc_stats(today_rows)
    week_stats  = calc_stats(week_rows)
    kpi         = calc_kpi(today_stats)

    # 4. 生成 & 保存报告
    report = build_report(today_stats, week_stats, kpi, today)
    print("\n" + "="*40 + "\n" + report + "\n" + "="*40)
    save_daily_report(today, today_rows, week_rows, report)

    # 5. 推送
    push_wecom(report)
    push_serverchan(f"博主建联日报·{today.strftime('%m/%d')}", report)
    print("✓ 全部完成")


if __name__ == "__main__":
    main()
