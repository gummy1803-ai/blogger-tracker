#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
博主建联管理工具 v4
用用户身份Token读取飞书知识库表格
"""

import os, sys, json, requests
from datetime import datetime, timedelta, date
from collections import defaultdict

# ══════════════════════════════════════════
#  配置
# ══════════════════════════════════════════
FEISHU_APP_ID       = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET   = os.environ["FEISHU_APP_SECRET"]
FEISHU_USER_TOKEN   = os.environ["FEISHU_USER_TOKEN"]
FEISHU_REFRESH_TOKEN= os.environ["FEISHU_REFRESH_TOKEN"]
WIKI_TOKEN          = os.environ["SHEET_TOKEN"]   # GguhwBZkMiJQsokq1ZYcPebEnfb

SUPABASE_URL        = os.environ["SUPABASE_URL"]
SUPABASE_KEY        = os.environ["SUPABASE_SERVICE_KEY"]
WECOM_WEBHOOK       = os.environ.get("WECOM_WEBHOOK", "")
SERVERCHAN_KEY      = os.environ.get("SERVERCHAN_KEY", "")

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
#  刷新 User Token
# ════════════════════════════════════════

def refresh_user_token() -> str:
    """用 refresh_token 获取新的 user_access_token"""
    # 先拿 app_access_token
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
    app_token = r.json().get("app_access_token", "")

    # 用 refresh_token 换新 user_access_token
    r2 = requests.post(
        "https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token",
        headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
        json={"grant_type": "refresh_token", "refresh_token": FEISHU_REFRESH_TOKEN},
        timeout=10)
    d = r2.json()
    if d.get("code") == 0:
        token = d["data"]["access_token"]
        print(f"  ✓ Token刷新成功")
        return token
    else:
        print(f"  Token刷新失败: {d}, 使用原Token")
        return FEISHU_USER_TOKEN


# ════════════════════════════════════════
#  读取飞书数据
# ════════════════════════════════════════

def get_spreadsheet_token(user_token: str) -> str:
    """从wiki节点获取spreadsheet token"""
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token={WIKI_TOKEN}"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = r.json()
    print(f"  Wiki节点: code={d.get('code')}, msg={d.get('msg','')}")
    if d.get("code") != 0:
        raise RuntimeError(f"获取wiki节点失败: {d}")
    obj_token = d["data"]["node"]["obj_token"]
    print(f"  spreadsheet_token: {obj_token}")
    return obj_token


def get_today_sheet(user_token: str, spreadsheet_token: str, today: date) -> dict:
    """获取今日对应的sheet"""
    url = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"获取sheet列表失败: {d}")

    sheets = d["data"]["sheets"]
    candidates = [f"{today.month}.{today.day}", f"{today.month:02d}.{today.day:02d}"]
    for s in sheets:
        if s.get("title","").strip() in candidates:
            print(f"  找到今日sheet: {s['title']}")
            return s
    print(f"  可用sheets: {[s['title'] for s in sheets]}")
    raise RuntimeError(f"未找到今日sheet: {today.month}.{today.day}")


def read_sheet_data(user_token: str, spreadsheet_token: str, sheet_id: str) -> list:
    """读取sheet数据"""
    range_str = f"{sheet_id}!A3:G300"
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_str}"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"读取数据失败: {d}")
    return d.get("data",{}).get("valueRange",{}).get("values",[]) or []


# ════════════════════════════════════════
#  数据解析
# ════════════════════════════════════════

def parse_followers(raw) -> float:
    if raw is None: return 0.0
    try: return float(str(raw).strip().replace(",",""))
    except: return 0.0

def parse_rows(rows: list, record_date: str) -> list:
    records = []
    for row in rows:
        if not row or all(not c for c in row): continue
        def get(i, d=""):
            try: v=row[i]; return str(v).strip() if v is not None else d
            except IndexError: return d
        name = get(COL["name"]); link = get(COL["link"])
        if not (name or link): continue
        try: int(float(get(0)))
        except: continue
        records.append({
            "link":        link,
            "name":        name,
            "status":      get(COL["status"]),
            "followers":   parse_followers(row[COL["followers"]] if len(row)>COL["followers"] else 0),
            "owner":       get(COL["owner"]) or "未分配",
            "record_date": record_date,
        })
    return records


# ════════════════════════════════════════
#  Supabase
# ════════════════════════════════════════

def supa_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}

def upsert_bloggers(records):
    if not records: return 0
    r = requests.post(f"{SUPABASE_URL}/rest/v1/bloggers",
                      headers=supa_headers(), json=records, timeout=15)
    if r.status_code not in (200,201):
        print(f"  ⚠️ Supabase错误: {r.status_code} {r.text[:200]}")
        return 0
    return len(records)

def save_daily_report(today, records, report_text):
    cont = sum(1 for r in records if r["status"] in STATUS_CONTACT)
    succ = sum(1 for r in records if r["status"] == STATUS_SUCCESS)
    payload = {"report_date": str(today), "total": len(records),
               "contacted": cont, "success": succ,
               "rate": round(succ/cont,4) if cont else 0,
               "w_total": len(records), "w_contacted": cont, "w_success": succ,
               "w_rate": round(succ/cont,4) if cont else 0,
               "report_text": report_text}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/daily_reports",
                      headers=supa_headers(), json=payload, timeout=10)
    print(f"  日报快照: {r.status_code}")


# ════════════════════════════════════════
#  统计 & 考评 & 报告
# ════════════════════════════════════════

def calc_stats(rows):
    bk = defaultdict(lambda: {"total":0,"contacted":0,"success":0,"fans":[]})
    for r in rows:
        o=r["owner"]; bk[o]["total"]+=1
        if r["status"] in STATUS_CONTACT: bk[o]["contacted"]+=1
        if r["status"]==STATUS_SUCCESS: bk[o]["success"]+=1
        if r["followers"]>0: bk[o]["fans"].append(r["followers"])
    res={}
    for o,b in bk.items():
        af=sum(b["fans"])/len(b["fans"]) if b["fans"] else 0; c=b["contacted"]
        res[o]={**b,"avg_fans":af,"quality_ok":af<=MAX_AVG_FOLLOWERS,"rate":b["success"]/c if c else 0}
    return res

def calc_kpi(stats):
    avgs=[s["contacted"] for s in stats.values()]; team=sum(avgs)/len(avgs) if avgs else 1
    out={}
    for o,s in stats.items():
        act=min(s["contacted"]/max(team,1),2.0)/2.0
        qual=1.0 if s["quality_ok"] else max(0,1-(s["avg_fans"]-MAX_AVG_FOLLOWERS)/MAX_AVG_FOLLOWERS)
        sc=round((s["rate"]*W_SUCCESS+qual*W_QUALITY+act*W_ACTIVITY)*100,1)
        out[o]={"score":sc,"grade":"A"if sc>=80 else"B"if sc>=60 else"C"if sc>=40 else"D"}
    return out

def fmt_rate(n,d): return f"{n/d*100:.1f}%" if d else "—"
def fmt_f(f): return f"{f:.1f}万"
def sums(s): return (sum(v["total"] for v in s.values()),
                     sum(v["contacted"] for v in s.values()),
                     sum(v["success"] for v in s.values()))

def build_report(stats, kpi, today, total):
    dl=today.strftime("%m月%d日"); tt,tc,ts=sums(stats)
    ge={"A":"⭐","B":"✅","C":"⚠️","D":"❌"}
    lines=[f"📊 博主建联日报 · {dl}","━"*24,
           f"  录入博主：{total} 人",f"  已联系：{tc} 人",
           f"  引导私域成功：{ts} 人",f"  转化率：{fmt_rate(ts,tc)}","",
           "👥 跟进人进度 & 考评","━"*24]
    for o in sorted(stats,key=lambda x:stats[x]["contacted"],reverse=True):
        s=stats[o]; k=kpi.get(o,{"score":0,"grade":"D"})
        q="✅ 达标" if s["quality_ok"] else f"⚠️ 超标（均{fmt_f(s['avg_fans'])}）"
        lines+=[f"👤 {o}  {ge.get(k['grade'],'')} {k['grade']}级 {k['score']}分",
                f"  联系{s['contacted']}人 · 成功{s['success']}人 · {fmt_rate(s['success'],s['contacted'])}",
                f"  均粉：{fmt_f(s['avg_fans'])}  {q}",""]
    lines+=["━"*24,"A≥80 B≥60 C≥40 D<40 | 转化40%+质量30%+活跃30%"]
    return "\n".join(lines)


# ════════════════════════════════════════
#  推送
# ════════════════════════════════════════

def push_wecom(content):
    if not WECOM_WEBHOOK: return
    r=requests.post(WECOM_WEBHOOK,json={"msgtype":"text","text":{"content":content}},timeout=10)
    print(f"  [企微] {r.status_code}")

def push_serverchan(title,content):
    if not SERVERCHAN_KEY: return
    r=requests.post(f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
                    data={"title":title,"desp":content.replace("\n","\n\n")},timeout=10)
    print(f"  [Server酱] {r.status_code}")


# ════════════════════════════════════════
#  主入口
# ════════════════════════════════════════

def main():
    today = date.today()
    print(f"▶ 开始同步  {today}")

    # 1. 刷新用户Token
    user_token = refresh_user_token()

    # 2. 获取spreadsheet token
    spreadsheet_token = get_spreadsheet_token(user_token)

    # 3. 找今日sheet
    today_sheet = get_today_sheet(user_token, spreadsheet_token, today)

    # 4. 读取数据
    rows = read_sheet_data(user_token, spreadsheet_token, today_sheet["sheet_id"])
    records = parse_rows(rows, str(today))
    print(f"  有效记录: {len(records)} 条")

    if not records:
        print("  ⚠️ 今日无数据")
        sys.exit(0)

    # 5. 写入Supabase
    written = upsert_bloggers(records)
    print(f"  Supabase: {written} 条")

    # 6. 报告
    stats  = calc_stats(records)
    kpi    = calc_kpi(stats)
    report = build_report(stats, kpi, today, len(records))
    print("\n" + "="*40 + "\n" + report + "\n" + "="*40)

    save_daily_report(today, records, report)
    push_wecom(report)
    push_serverchan(f"博主建联日报·{today.strftime('%m/%d')}", report)
    print("✓ 完成")


if __name__ == "__main__":
    main()
