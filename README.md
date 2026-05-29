# 博主建联管理系统 v2 📊
**飞书 Sheet → Supabase → 前端实时看板 + 企微/微信播报**
完全免费，零服务器。

---

## 系统架构

```
飞书电子表格
     │
     │ 每天 09:00 自动拉取
     ▼
GitHub Actions (免费)
  sync.py
     │
     ├─→ Supabase 数据库 (免费 500MB)
     │       │
     │       └─→ dashboard.html 实时读取展示
     │
     ├─→ 企业微信机器人
     └─→ 微信 Server酱
```

---

## 部署步骤（约 20 分钟）

### Step 1：飞书表格新增"录入日期"列

在 **H 列** 新增一列，表头写"录入日期"，格式：`2025-05-29`

当前列顺序（不要改动，或在 `sync.py` 的 `COL` 里同步修改）：

| A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|
| 数量 | 博主链接 | 回复情况 | 博主名 | 粉丝量(万) | 联系方式 | 跟进人 | 录入日期 |

**回复情况**下拉选项建议固定为：
- `没回`
- `抖音私信建联`
- `引导到私域了`

---

### Step 2：创建 Supabase 项目

1. 打开 [supabase.com](https://supabase.com) → 免费注册 → 新建项目
2. 进入 **SQL Editor** → 粘贴 `schema.sql` 全部内容 → 点击 Run
3. 进入 **Settings → API**，记录：
   - `Project URL`（格式：`https://xxxx.supabase.co`）
   - `anon public` Key（前端用，只读）
   - `service_role` Key（Python 脚本用，可写，**不要泄露**）

---

### Step 3：配置前端看板

打开 `dashboard.html`，修改第 21-22 行：

```javascript
const SUPABASE_URL  = "https://xxxx.supabase.co";  // ← 你的 Project URL
const SUPABASE_ANON = "eyJhbGci...";               // ← anon public key
```

将 `dashboard.html` 用浏览器直接打开即可使用。
也可以托管到 GitHub Pages（免费）让所有人访问。

---

### Step 4：飞书开放平台配置

1. 打开 [open.feishu.cn](https://open.feishu.cn) → 创建企业自建应用
2. 权限管理 → 开启 `sheets:spreadsheet:readonly`
3. 记录 **App ID** 和 **App Secret**
4. 从飞书表格 URL 提取 **spreadsheetToken**：
   ```
   https://xxx.feishu.cn/sheets/shtXXXXXXX
                                ^^^^^^^^^^^ 这段就是 SHEET_TOKEN
   ```

---

### Step 5：获取推送渠道

**企微机器人**：群右上角 → 添加机器人 → 复制 Webhook 地址

**Server酱（微信推送）**：[sct.ftqq.com](https://sct.ftqq.com) → 微信扫码 → 复制 SendKey

---

### Step 6：配置 GitHub 仓库密钥

代码推到 GitHub 后 → Settings → Secrets and variables → Actions → New repository secret

| 变量名 | 说明 |
|--------|------|
| `FEISHU_APP_ID` | 飞书 App ID |
| `FEISHU_APP_SECRET` | 飞书 App Secret |
| `SHEET_TOKEN` | 表格 Token |
| `SHEET_ID` | 页签名，默认 `Sheet1` |
| `SUPABASE_URL` | Supabase Project URL |
| `SUPABASE_SERVICE_KEY` | service_role key |
| `WECOM_WEBHOOK` | 企微机器人 Webhook |
| `SERVERCHAN_KEY` | Server酱 SendKey |

---

## 每天操作流程

1. 在飞书表格中录入博主信息，**H列填今天日期**（如 `2025-05-29`）
2. GitHub Actions 每天 **09:00 自动**拉取数据，写入 Supabase，推送播报
3. 随时打开 `dashboard.html` 查看实时数据

**手动触发**：GitHub → Actions → 飞书同步 & 日报推送 → Run workflow

---

## 考评规则

| 等级 | 分数 | 含义 |
|------|------|------|
| A ⭐ | ≥80 | 优秀 |
| B ✅ | ≥60 | 良好 |
| C ⚠️ | ≥40 | 待改进 |
| D ❌ | <40 | 需关注 |

评分 = **转化率40%** + **博主质量30%** + **活跃度30%**

- 转化率：引导私域成功 ÷ 已联系
- 质量：该跟进人名下博主均粉 ≤5万为满分
- 活跃度：个人联系量 ÷ 团队平均（上限2倍）
