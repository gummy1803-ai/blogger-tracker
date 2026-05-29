#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
博主建联管理工具 · 每日播报
数据源：飞书电子表格
输出：企业微信 + 微信（Server酱）
"""

import os
import sys
import requests
from datetime import datetime, timedelta, date
from collections import defaultdict

# ══════════════════════════════════════════
#  配置区（从环境变量读取）
# ══════════════════════════════════════════
FEISHU_APP_ID     = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
SHEET_TOKEN       = os.environ["SHEET_TOKEN"]
SHEET_ID          = os.environ.get("SHEET_ID", "Sheet1")
WECOM_WEBHOOK     = os.environ.get("WECOM_WEBHOOK", "")
SERVERCHAN_KEY    = os.environ.get("SERVERCHAN_KEY", "")

# ══════════════════════════════════════════
#  列映射（与实际表格一致）
#  A=0  B=1  C=2  D=3  E=4  F=5  G=6  H=7
# ══════════════════════════════════════════
COL = {
    "seq":       0,   # A: 数量（序号，忽略）
    "link":      1,   # B: 博主链接
    "status":    2,   # C: 回复情况
    "name":      3,   # D: 博主名
    "followers": 4,   # E: 粉丝量（单位：万人）
    "contact":   5,   # F: 联系方式/ID（忽略）
    "owner":     6,   # G: 跟进人
    "date":      7,   # H: 录入日期（格式 2025-05-29）⚠️ 需在表格新增此列
}

# ══════════════════════════════════════════
#  业务规则
# ══════════════════════════════════════════
MAX_AVG_FOLLOWERS = 5.0        # 平均粉丝上限（万人，对应 5万）
STATUS_SUCCESS    = "引导到私域了"
STATUS_CONTACTED  = {"抖音私信建联", "引导到私域了"}
# 注意：表格里显示的是"没回"，与"未回"对应，两种都兼容
STATUS_NO_REPLY   = {"没回", "未回"}

# 考评权重
W_SUCCESS  = 0.40   # 建联转化率
W_QUALITY  = 0.30   # 博主质量（均粉达标率）
W_ACTIVITY = 0.30   # 活跃度（相对团队均值）


# ══════════════════════════════════════════
#  飞书 API
# ══════════════════════════════════════════

def get_feishu_token() -> str:
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书授权失败：{data}")
    return data["tenant_access_token"]


def read_sheet_rows(access_token: str) -> list:
    """读取 A2:H2000（跳过第1行表头）"""
    range_str = f"{SHEET_ID}!A2:H2000"
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{SHEET_TOKEN}/values/{range_str}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"读取表格失败：{data}")
    return data.get("data", {}).get("valueRange", {}).get("values", []) or []


# ══════════════════════════════════════════
#  数据解析
# ══════════════════════════════════════════

def parse_followers(raw) -> float:
    """
    解析粉丝数，返回万人单位的浮点数。
    表格 E 列单位本身就是万人，直接读即可。
    示例：1 → 1.0万, 0.2 → 0.2万, 8.6 → 8.6万
    """
    if raw is None:
        return 0.0
    s = str(raw).strip().replace(",", "")
    try:
        # 如果已经是数字（万为单位），直接返回
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def parse_row(row: list) -> dict:
    def get(idx, default=""):
        try:
            v = row[idx]
            return str(v).strip() if v is not None else default
        except IndexError:
            return default

    return {
        "link":      get(COL["link"]),
        "status":    get(COL["status"]),
        "name":      get(COL["name"]),
        "followers": parse_followers(row[COL["followers"]] if len(row) > COL["followers"] else 0),
        "owner":     get(COL["owner"]) or "未分配",
        "date":      get(COL["date"]),
    }


def load_records(rows: list) -> list:
    records = []
    for row in rows:
        if not row or all((not c) for c in row):
            continue
        r = parse_row(row)
        if r["name"] or r["link"]:
            records.append(r)
    return records


# ══════════════════════════════════════════
#  时间筛选
# ══════════════════════════════════════════

def records_of_date(records: list, target: date) -> list:
    return [r for r in records if r["date"] == target.strftime("%Y-%m-%d")]


def records_of_week(records: list, today: date) -> list:
    week_start = today - timedelta(days=today.weekday())
    result = []
    for r in records:
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            if week_start <= d <= today:
                result.append(r)
        except (ValueError, TypeError):
            pass
    return result


# ══════════════════════════════════════════
#  统计分析
# ══════════════════════════════════════════

def calc_stats(records: list) -> dict:
    bucket = defaultdict(lambda: {
        "total": 0, "contacted": 0, "success": 0, "followers": []
    })
    for r in records:
        owner = r["owner"]
        bucket[owner]["total"] += 1
        if r["status"] in STATUS_CONTACTED:
            bucket[owner]["contacted"] += 1
        if r["status"] == STATUS_SUCCESS:
            bucket[owner]["success"] += 1
        if r["followers"] > 0:
            bucket[owner]["followers"].append(r["followers"])

    result = {}
    for owner, b in bucket.items():
        avg_f = sum(b["followers"]) / len(b["followers"]) if b["followers"] else 0.0
        contacted = b["contacted"]
        result[owner] = {
            "total":         b["total"],
            "contacted":     contacted,
            "success":       b["success"],
            "success_rate":  b["success"] / contacted if contacted else 0.0,
            "avg_followers": avg_f,           # 万人
            "quality_ok":    avg_f <= MAX_AVG_FOLLOWERS,
        }
    return result


def calc_kpi(today_stats: dict) -> dict:
    totals   = [s["contacted"] for s in today_stats.values()]
    team_avg = sum(totals) / len(totals) if totals else 1

    scores = {}
    for owner, s in today_stats.items():
        activity = min(s["contacted"] / max(team_avg, 1), 2.0) / 2.0

        if s["quality_ok"]:
            quality = 1.0
        else:
            over_ratio = (s["avg_followers"] - MAX_AVG_FOLLOWERS) / MAX_AVG_FOLLOWERS
            quality = max(0.0, 1.0 - over_ratio)

        raw   = (s["success_rate"] * W_SUCCESS + quality * W_QUALITY + activity * W_ACTIVITY) * 100
        score = round(raw, 1)
        grade = "A ⭐" if score >= 80 else "B ✅" if score >= 60 else "C ⚠️" if score >= 40 else "D ❌"
        scores[owner] = {"score": score, "grade": grade}
    return scores


# ══════════════════════════════════════════
#  报告生成
# ══════════════════════════════════════════

def fmt_rate(n, d):
    return f"{n/d*100:.1f}%" if d else "—"

def fmt_fans(f: float) -> str:
    """格式化粉丝数（万人）"""
    return f"{f:.1f}万"

def sum_stats(stats: dict):
    return (
        sum(s["total"]     for s in stats.values()),
        sum(s["contacted"] for s in stats.values()),
        sum(s["success"]   for s in stats.values()),
    )


def build_report(today_stats, week_stats, kpi, today: date) -> str:
    date_label = today.strftime("%m月%d日")
    week_start = (today - timedelta(days=today.weekday())).strftime("%m/%d")
    week_end   = today.strftime("%m/%d")

    t_total, t_cont, t_succ = sum_stats(today_stats)
    w_total, w_cont, w_succ = sum_stats(week_stats)

    lines = [
        f"📊 博主建联日报 · {date_label}",
        "━" * 24,
        "📅 今日总览",
        f"  录入博主：{t_total} 人",
        f"  已联系：{t_cont} 人",
        f"  引导私域成功：{t_succ} 人",
        f"  今日转化率：{fmt_rate(t_succ, t_cont)}",
        "",
        f"📆 本周累计（{week_start} ~ {week_end}）",
        f"  录入：{w_total} 人  联系：{w_cont} 人  成功：{w_succ} 人",
        f"  周转化率：{fmt_rate(w_succ, w_cont)}",
        "",
        "👥 跟进人进度 & 考评",
        "━" * 24,
    ]

    sorted_owners = sorted(
        today_stats.keys(),
        key=lambda o: today_stats[o]["contacted"],
        reverse=True,
    )

    for owner in sorted_owners:
        ds = today_stats[owner]
        ws = week_stats.get(owner, {"contacted": 0, "success": 0})
        k  = kpi.get(owner, {"score": 0, "grade": "—"})
        q_label = "✅ 达标" if ds["quality_ok"] else f"⚠️ 超标（均{fmt_fans(ds['avg_followers'])}）"

        lines += [
            f"👤 {owner}    {k['grade']}  {k['score']} 分",
            f"  今日：联系 {ds['contacted']} 人 · 成功 {ds['success']} 人 · 转化率 {fmt_rate(ds['success'], ds['contacted'])}",
            f"  本周：联系 {ws['contacted']} 人 · 成功 {ws['success']} 人",
            f"  博主均粉：{fmt_fans(ds['avg_followers'])}  {q_label}",
            "",
        ]

    lines += [
        "━" * 24,
        "📌 考评说明（满分100）",
        "  A≥80  B≥60  C≥40  D<40",
        "  转化率40% + 质量30% + 活跃度30%",
        "  质量 = 跟进博主均粉 ≤ 5万为满分",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════
#  推送
# ══════════════════════════════════════════

def push_wecom(content: str):
    if not WECOM_WEBHOOK:
        print("[企微] 未配置 Webhook，跳过")
        return
    resp = requests.post(
        WECOM_WEBHOOK,
        json={"msgtype": "text", "text": {"content": content}},
        timeout=10,
    )
    print(f"[企微] 状态: {resp.status_code}")


def push_serverchan(title: str, content: str):
    if not SERVERCHAN_KEY:
        print("[Server酱] 未配置 Key，跳过")
        return
    resp = requests.post(
        f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
        data={"title": title, "desp": content.replace("\n", "\n\n")},
        timeout=10,
    )
    print(f"[Server酱] 状态: {resp.status_code}")


# ══════════════════════════════════════════
#  主入口
# ══════════════════════════════════════════

def main():
    today = date.today()
    print(f"▶ 生成日报  日期: {today}")

    token   = get_feishu_token()
    rows    = read_sheet_rows(token)
    records = load_records(rows)
    print(f"  有效记录: {len(records)} 条")

    if not records:
        print("  ⚠️ 无数据，退出")
        sys.exit(0)

    today_records = records_of_date(records, today)
    week_records  = records_of_week(records, today)
    print(f"  今日: {len(today_records)} 条  本周: {len(week_records)} 条")

    today_stats = calc_stats(today_records)
    week_stats  = calc_stats(week_records)
    kpi         = calc_kpi(today_stats)

    report = build_report(today_stats, week_stats, kpi, today)
    print("\n" + "=" * 40)
    print(report)
    print("=" * 40 + "\n")

    push_wecom(report)
    push_serverchan(f"博主建联日报 · {today.strftime('%m/%d')}", report)
    print("✓ 完成")


if __name__ == "__main__":
    main()
