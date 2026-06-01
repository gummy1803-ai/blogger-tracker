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
from datetime import date
from collections import defaultdict

FEISHU_APP_ID        = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET    = os.environ["FEISHU_APP_SECRET"]
FEISHU_USER_TOKEN    = os.environ["FEISHU_USER_TOKEN"]
FEISHU_REFRESH_TOKEN = os.environ["FEISHU_REFRESH_TOKEN"]
WIKI_TOKEN           = os.environ["WIKI_TOKEN"]
SERVERCHAN_KEY       = os.environ.get("SERVERCHAN_KEY", "")
WECOM_WEBHOOK        = os.environ.get("WECOM_WEBHOOK", "")
GITHUB_TOKEN         = os.environ["GITHUB_TOKEN"]
GITHUB_REPO          = "gummy1803-ai/blogger-tracker"

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

def get_app_token():
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
    return r.json().get("app_access_token", "")

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
            return
        encrypted = encrypt_secret(key, secret_value)
        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/{secret_name}",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "X-GitHub-Api-Version": "2022-11-28"},
            json={"encrypted_value": encrypted, "key_id": key_id})
    except Exception as e:
        print(f"  ⚠️ Secret更新异常: {e}")

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
        update_github_secret("FEISHU_USER_TOKEN",    new_user_token)
        update_github_secret("FEISHU_REFRESH_TOKEN", new_refresh_token)
        return new_user_token
    return FEISHU_USER_TOKEN


# ════════════════════════════════════════
#  飞书数据读写
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
    try:
        return str(date(date.today().year, int(m.group(1)), int(m.group(2))))
    except:
        return None

def read_sheet_data(user_token, spreadsheet_token, sheet_id):
    range_str = f"{sheet_id}!A3:K500"
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_str}"
    r = requests.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=10)
    d = r.json()
    if d.get("code") != 0:
        return []
    return d.get("data", {}).get("valueRange", {}).get("values", []) or []

def get_cell(row, idx, default=""):
    try:
        v = row[idx]
        return str(v).strip() if v is not None else default
    except IndexError:
        return default

def normalize_key(link, account):
    """提取可用于去重的唯一标识"""
    keys = set()
    # 从链接提取用户ID
    for pattern in [r'user/([A-Za-z0-9_\-]+)', r'/([A-Za-z0-9_\-]{8,})']:
        m = re.search(pattern, link)
        if m:
            keys.add(m.group(1).lower())
    # 抖音号
    if account:
        keys.add(account.strip().lower())
    # 链接本身清理后
    if link:
        clean = re.sub(r'[?#].*', '', link).rstrip('/').lower()
        keys.add(clean)
    return keys

def write_dup_marks(user_token, spreadsheet_token, sheet_id, rows_to_mark):
    """在H列写入"已重复"（1-based行号）"""
    if not rows_to_mark:
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
        if r.json().get("code") == 0:
            written += len(batch)
    return written

def lock_sheet(user_token, spreadsheet_token, sheet_id):
    """锁定工作表（设置保护范围，普通成员只读）"""
    try:
        r = requests.post(
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/protected_dimension",
            headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
            json={
                "addProtectedDimension": {
                    "dimension": {
                        "sheetId":       sheet_id,
                        "majorDimension": "ROWS",
                        "startIndex":    0,
                        "endIndex":      1000,
                    },
                    "editors": {"users": [], "groups": []},
                }
            },
            timeout=10)
        print(f"  锁表: {r.status_code}")
    except Exception as e:
        print(f"  ⚠️ 锁表失败: {e}")


# ════════════════════════════════════════
#  查重核心
# ════════════════════════════════════════

def run_dup_check(user_token, spreadsheet_token, all_sheets):
    """
    跨表查重：按链接和抖音号去重
    返回 (total_marked, dup_report_lines)
    """
    # 第一遍：读所有数据，建立去重索引
    key_to_first = {}     # normalized_key → {sheet, row, name, owner, date}
    sheet_records = {}    # sheet_id → [(row_num, keys, name, owner, is_already_dup)]

    for sheet in all_sheets:
        title       = sheet.get("title", "").strip()
        record_date = sheet_title_to_date(title)
        if not record_date:
            continue

        sid  = sheet.get("sheetId") or sheet.get("sheet_id", "")
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
        for key in keys:
            if key and key in key_to_first:
                is_dup = True
                matched_first = key_to_first[key]
                break

        if is_dup:
            if not already_dup:
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
        else:
            # 首次出现，加入索引
            for key in keys:
                if key:
                    key_to_first[key] = {
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

def build_dup_report(total_marked, dup_details, is_final):
    today = date.today()
    tag   = "【最终查重·表格已锁定】" if is_final else "【定时查重】"
    lines = [
        f"🔍 {tag} {today.month}月{today.day}日",
        "━" * 22,
        f"  本次新发现重复：{total_marked} 条",
    ]
    if dup_details:
        lines += ["", "重复明细："]
        for d in dup_details[:15]:
            lines.append(
                f"  ⚠️ {d['name']}（{d['owner']}·{d['sheet']}第{d['row']}行）"
                f"  ← 首次：{d['first_owner']}·{d['first_sheet']}"
            )
        if len(dup_details) > 15:
            lines.append(f"  ...共 {len(dup_details)} 条，已全部写回H列")
    else:
        lines.append("  ✅ 未发现新增重复")

    if is_final:
        lines += ["", "📌 表格已锁定，如需修改请联系管理员"]
    return "\n".join(lines)


# ════════════════════════════════════════
#  主入口
# ════════════════════════════════════════

def main():
    today = date.today()
    print(f"▶ 查重开始  {today}  IS_FINAL={IS_FINAL}")

    user_token        = refresh_user_token()
    spreadsheet_token = get_spreadsheet_token(user_token)
    all_sheets        = get_all_sheets(user_token, spreadsheet_token)

    total_marked, dup_details = run_dup_check(user_token, spreadsheet_token, all_sheets)
    print(f"  新标注重复: {total_marked} 条")

    # 0:00 最后一次：锁定今日工作表
    if IS_FINAL:
        today_str = str(today)
        for sheet in all_sheets:
            title = sheet.get("title", "").strip()
            if sheet_title_to_date(title) == today_str:
                sid = sheet.get("sheetId") or sheet.get("sheet_id", "")
                lock_sheet(user_token, spreadsheet_token, sid)
                print(f"  已锁定: {title}")
                break

    report = build_dup_report(total_marked, dup_details, IS_FINAL)
    print("\n" + report)

    push_wecom(report)
    push_serverchan(f"查重报告·{today.month}/{today.day}", report)
    print("✓ 完成")

if __name__ == "__main__":
    main()
