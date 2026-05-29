import requests, json, os

APP_ID     = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
CODE       = os.environ["FEISHU_AUTH_CODE"]

# 第一步：获取 app_access_token
r1 = requests.post(
    "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
    json={"app_id": APP_ID, "app_secret": APP_SECRET}
)
app_token = r1.json().get("app_access_token", "")
print(f"app_access_token: {app_token[:20]}...")

# 第二步：用 code 换 user_access_token
r2 = requests.post(
    "https://open.feishu.cn/open-apis/authen/v1/access_token",
    headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
    json={"grant_type": "authorization_code", "code": CODE}
)
d = r2.json()
print(json.dumps(d, indent=2, ensure_ascii=False))

if d.get("code") == 0:
    data = d["data"]
    print(f"\n✅ 成功！")
    print(f"user_access_token: {data.get('access_token','')}")
    print(f"refresh_token: {data.get('refresh_token','')}")
    print(f"expires_in: {data.get('expires_in','')} 秒")
