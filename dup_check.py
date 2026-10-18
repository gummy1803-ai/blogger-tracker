#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨表查重脚本
- 每天 21:00-24:00 每15分钟跑一次
- 按博主链接和抖音号查重（不是博主名）
- 重复行在 H 列写入"已重复"
- 0:00 锁定当天工作表
- 查重报告推送微信
"""

import os, sys, requests, re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit
from collections import defaultdict

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
WIKI_TOKEN = os.environ.get("WIKI_TOKEN", "")
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
ALLOW_LOCK = os.environ.get("ALLOW_LOCK", "false").lower() == "true"
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def business_today():
    return datetime.now(BUSINESS_TZ).date()


def target_date():
    explicit = os.environ.get("TARGET_DATE", "")
    if explicit:
        return date.fromisoformat(explicit)
    return business_today() - timedelta(days=1 if IS_FINAL else 0)


# 列索引
COL_SEQ     = 0
COL_LINK    = 1
COL_STATUS  = 2
COL_NAME    = 3
COL_ACCOUNT = 5
COL_OWNER   = 6
COL_DUP     = 7   # H列

# 是否是最后一次跑（0:00锁表）
IS_FINAL = os.environ.get("IS_FINAL", "false").lower() == "true"


# ════════════════════════════════════════
#  飞书 Token
# ════════════════════════════════════════

def checked(response):
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu API failed: code={data.get('code')}")
    return data


def get_app_token():
    if not all((FEISHU_APP_ID, FEISHU_APP_SECRET, WIKI_TOKEN)):
        raise RuntimeError("Missing FEISHU_APP_ID, FEISHU_APP_SECRET or WIKI_TOKEN")
    d = checked(requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10))
    if not d.get("tenant_access_token"):
        raise RuntimeError("Missing tenant_access_token")
    return d["tenant_access_token"]


# ════════════════════════════════════════
#  飞书数据读写
# ════════════════════════════════════════

def get_spreadsheet_token(user_token):
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?token={WIKI_TOKEN}"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = checked(r)
    if d.get("code") != 0:
        raise RuntimeError(f"获取wiki节点失败: {d.get('msg','')}")
    return d["data"]["node"]["obj_token"]

def get_all_sheets(user_token, spreadsheet_token):
    url = f"https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = checked(r)
    if d.get("code") != 0:
        raise RuntimeError(f"获取sheet列表失败: {d}")
    return d["data"]["sheets"]

def sheet_title_to_date(title):
    title = title.strip()
    m = re.match(r'^(\d{1,2})\.(\d{1,2})$', title)
    if not m:
        return None
    try:
        return str(date(target_date().year, int(m.group(1)), int(m.group(2))))
    except:
        return None

def read_sheet_data(user_token, spreadsheet_token, sheet_id):
    range_str = f"{sheet_id}!A3:K500"
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_str}"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = checked(r)
    if d.get("code") != 0:
        raise RuntimeError("Sheet read failed; refusing partial deduplication")
    values = d["data"]["valueRange"].get("values", []) or []
    if len(values) >= 498:
        raise RuntimeError("Read window exhausted; refusing incomplete scan")
    return values

def get_cell(row, idx, default=""):
    if idx >= len(row) or row[idx] is None:
        return default
    value = row[idx]
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or default).strip()
    if isinstance(value, list):
        return next((get_cell([v], 0) for v in value if isinstance(v, dict) and v.get("link")), default)
    return str(value).strip()


def normalize_key(link, account):
    """Conservative identities: never collapse users across platforms or lowercase IDs."""
    keys = set()
    parts = urlsplit(link.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme in ("http", "https") and host:
        keys.add("url:" + urlunsplit(("https", host, parts.path.rstrip("/"), parts.query, "")))
    # Column F is documented as Douyin account; other platforms need explicit rules.
    if account and (not host or host == "douyin.com" or host.endswith(".douyin.com")):
        keys.add("douyin-account:" + account.strip())
    return keys


def write_dup_marks(user_token, spreadsheet_token, sheet_id, rows_to_mark):
    """在H列写入"已重复"（1-based行号）"""
    if not rows_to_mark:
        return 0
    if DRY_RUN:
        return 0
    value_ranges = []
    for row_num in rows_to_mark:
        value_ranges.append({
            "range":  f"{sheet_id}!H{row_num}:H{row_num}",
            "values": [["已重复"]]
        })
    written = 0
    for i in range(0, len(value_ranges), 20):
        batch = value_ranges[i:i+20]
        r = requests.post(
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_update",
            headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
            json={"valueRanges": batch},
            timeout=15)
        checked(r)
        written += len(batch)
    return written

def lock_sheet(user_token, spreadsheet_token, sheet_id, row_count):
    if DRY_RUN or not ALLOW_LOCK or not IS_FINAL:
        return False
    if not isinstance(row_count, int) or row_count <= 0:
        raise RuntimeError("Missing row count; refusing lock")
    r = requests.post(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/protected_dimension",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"addProtectedDimension": {
            "dimension": {"sheetId": sheet_id, "majorDimension": "ROWS",
                          "startIndex": 0, "endIndex": row_count},
            "editors": {"users": [], "groups": []}}}, timeout=10)
    checked(r)
    return True


# ════════════════════════════════════════
#  查重核心
# ════════════════════════════════════════

def run_dup_check(user_token, spreadsheet_token, all_sheets):
    """
    跨表查重：按链接和抖音号去重
    返回 (total_marked, dup_report_lines)
    """
    target = target_date().isoformat()
    # 第一遍：读所有数据，建立去重索引
    key_to_first = {}     # normalized_key → {sheet, row, name, owner, date}
    sheet_records = {}    # sheet_id → [(row_num, keys, name, owner, is_already_dup)]

    for sheet in all_sheets:
        title       = sheet.get("title", "").strip()
        record_date = sheet_title_to_date(title)
        if not record_date or record_date > target:
            continue

        sid  = sheet.get("sheetId") or sheet.get("sheet_id", "")
        if not sid:
            raise RuntimeError("Missing sheet id")
        rows = read_sheet_data(user_token, spreadsheet_token, sid)
        recs = []

        for i, row in enumerate(rows):
            if not row or all(not c for c in row):
                continue
            try:
                int(float(get_cell(row, COL_SEQ)))
            except:
                continue

            row_num = i + 3   # 数据从第3行开始
            link    = get_cell(row, COL_LINK)
            account = get_cell(row, COL_ACCOUNT)
            name    = get_cell(row, COL_NAME)
            owner   = get_cell(row, COL_OWNER) or "未分配"
            dup_val = get_cell(row, COL_DUP)
            if dup_val and "已重复" not in dup_val:
                raise RuntimeError("Nonempty custom H cell; refusing overwrite")
            already_dup = "已重复" in dup_val

            keys = normalize_key(link, account)
            recs.append((row_num, keys, name, owner, already_dup, record_date))

        sheet_records[sid] = (title, recs)

    # 第二遍：按日期顺序处理，先出现的为首次，后出现的标重复
    all_entries = []
    for sid, (title, recs) in sheet_records.items():
        for row_num, keys, name, owner, already_dup, record_date in recs:
            all_entries.append((record_date, title, sid, row_num, keys, name, owner, already_dup))
    all_entries.sort(key=lambda x: (x[0], x[1], x[3]))

    to_mark = defaultdict(list)    # sid → [row_num, ...]
    dup_details = []
    total_new_dups = 0

    for record_date, title, sid, row_num, keys, name, owner, already_dup in all_entries:
        is_dup = False
        matched_first = None
        for key in sorted(keys):
            if key and key in key_to_first:
                is_dup = True
                matched_first = key_to_first[key]
                break

        if is_dup:
            if not already_dup and record_date == target:
                to_mark[sid].append(row_num)
                total_new_dups += 1
                dup_details.append({
                    "name":       name,
                    "owner":      owner,
                    "sheet":      title,
                    "row":        row_num,
                    "first_sheet": matched_first["sheet"],
                    "first_owner": matched_first["owner"],
                    "first_date":  matched_first["date"],
                })
        # Keep aliases on duplicate rows, preserving the original first occurrence.
        for key in sorted(keys):
                if key and key not in key_to_first:
                    key_to_first[key] = matched_first or {
                        "sheet": title, "owner": owner,
                        "date":  record_date, "name": name,
                    }

    # 写回飞书
    total_marked = 0
    for sid, rows_to_mark in to_mark.items():
        n = write_dup_marks(user_token, spreadsheet_token, sid, rows_to_mark)
        total_marked += n

    return total_marked, dup_details


# ════════════════════════════════════════
#  推送
# ════════════════════════════════════════

def push_wecom(content):
    if not WECOM_WEBHOOK:
        return
    requests.post(WECOM_WEBHOOK,
                  json={"msgtype": "text", "text": {"content": content}},
                  timeout=10)

def push_serverchan(title, content):
    if not SERVERCHAN_KEY:
        return
    requests.post(f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send",
                  data={"title": title, "desp": content.replace("\n", "\n\n")},
                  timeout=10)

def build_dup_report(total_marked, dup_details, locked):
    today = target_date()
    tag   = "【最终查重·表格已锁定】" if locked else "【定时查重】"
    lines = [
        f"🔍 {tag} {today.month}月{today.day}日",
        "━" * 22,
        f"  本次发现：{len(dup_details)} 条；实际写入：{total_marked} 条",
        f"  模式：{'只读预演' if DRY_RUN else '写入'}",
    ]
    if dup_details:
        lines += ["", "重复明细："]
        for d in dup_details[:15]:
            lines.append(
                f"  ⚠️ {d['name']}（{d['owner']}·{d['sheet']}第{d['row']}行）"
                f"  ← 首次：{d['first_owner']}·{d['first_sheet']}"
            )
        if len(dup_details) > 15:
            lines.append(f"  ...共 {len(dup_details)} 条，请核对执行模式及实际写入数量")
    else:
        lines.append("  ✅ 未发现新增重复")

    if locked:
        lines += ["", "📌 表格已锁定，如需修改请联系管理员"]
    return "\n".join(lines)


# ════════════════════════════════════════
#  主入口
# ════════════════════════════════════════

def main():
    today = target_date()
    print(f"▶ 查重开始  {today}  IS_FINAL={IS_FINAL}")

    user_token        = get_app_token()
    spreadsheet_token = get_spreadsheet_token(user_token)
    all_sheets        = get_all_sheets(user_token, spreadsheet_token)

    total_marked, dup_details = run_dup_check(user_token, spreadsheet_token, all_sheets)
    print(f"  新标注重复: {total_marked} 条")

    locked = False
    if IS_FINAL and ALLOW_LOCK and not DRY_RUN:
        candidates = [sheet for sheet in all_sheets
                      if sheet_title_to_date(sheet.get("title", "")) == str(today)]
        if len(candidates) != 1:
            raise RuntimeError("Expected exactly one target sheet; refusing lock")
        sheet = candidates[0]
        sid = sheet.get("sheetId") or sheet.get("sheet_id", "")
        locked = lock_sheet(user_token, spreadsheet_token, sid,
                            sheet.get("grid_properties", {}).get("row_count"))

    report = build_dup_report(total_marked, dup_details, locked)
    print("\n" + report)

    if DRY_RUN:
        return
    push_wecom(report)
    push_serverchan(f"查重报告·{today.month}/{today.day}", report)
    print("✓ 完成")

if __name__ == "__main__":
    main()

