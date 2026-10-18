# Actions 排查与分阶段恢复（2026-10-18）

## 已确认与未确认

- main 当前基线：`0f6a2657ca013473b9aa32f9981ea938f486f63b`，最后提交 2026-06-29。
- 最后一次已核对运行：33170996420，2026-08-28，任务成功。但日志显示 Supabase HTTP 500 / code 21000，同批次冲突键重复，实际写入 0；日报快照 201，Server酱 HTTP 200。HTTP 成功不能单独证明业务推送送达。
- 根目录 dup_check.yml 不会被 Actions 调度；本修复移到 .github/workflows/。
- 6月29日至8月28日为60天，与公共仓库无活动自动暂停规则相符。**尚未取得工作流 state，不能断言 disabled_inactivity。**
- 当前 GitHub 连接拒绝 actions/workflows 和 actions/permissions 读取接口，也不提供 enable/dispatch 操作。Secrets 接口不可读；未读取或展示任何凭据值，未调用生产飞书写入/锁表。

## 修复范围

- 查重采用 tenant_access_token，每次动态申请，移除用户令牌刷新和 GitHub Secret 写入依赖。必须在飞书授予应用 Wiki/电子表格读取权限；正式写入另需相应编辑权限及文档协作者访问。
- 默认 DRY_RUN=true，无 H 列写入、锁表或推送；定时运行也默认预演。只有仓库变量 DUP_WRITE_ENABLED=true 才允许 H 列写入。
- 仅写指定目标日期，历史表用于比对，不自动重标历史。跨平台身份隔离、富文本链接提取、重复别名传递；无法解析的短链不联网展开，可能漏判，需人工检查。
- 按北京时间计算日期；午夜最终运行默认处理前一天。延迟超过一天或补跑必须显式输入 target_date。
- 全部读取完成才写；读取错误、达到 A3:K500 读取上限、写入错误都中止。当前仍限制498行，超限需先扩展分页读取。
- 锁表需要 IS_FINAL、非预演和 ALLOW_LOCK 三条件，缺少行数或目标表不唯一则中止；检查 HTTP 和飞书业务码，失败不会宣称已锁定。**工作流固定 ALLOW_LOCK=false，保护范围 API 请求结构、重复保护处理及真实权限尚未在隔离飞书副本验证，暂不可开启生产锁表。**
- 日报日期参数生效、参数通过环境变量传入，统一北京时间；按数据库真正的 (link, record_date) 键去重，避免空链接采用不同 name 导致同批冲突；写入/推送失败不再假成功。空链接同日只保留第一条，这是现有数据库唯一键的限制，若需要保留所有无链接博主，应另行迁移键设计。
- 工作流只需 contents:read，不需要 GH_PAT 或 secrets:write。日报和查重分别串行执行。

## 管理员执行的核验与恢复步骤

以下使用具有仓库管理/Actions权限的 GitHub CLI 登录会话；不要把令牌粘贴到日志或提交。

```bash
gh api repos/gummy1803-ai/blogger-tracker/actions/workflows/sync.yml --jq '{id,state,path}'
gh api repos/gummy1803-ai/blogger-tracker/actions/permissions
gh api repos/gummy1803-ai/blogger-tracker/actions/permissions/workflow
gh secret list -R gummy1803-ai/blogger-tracker
```

记录实际 state（active / disabled_inactivity / disabled_manually），核查仓库 Settings → Actions 允许 checkout@v4、setup-python@v5，以及账户/仓库未被禁用。不要为此开放默认写权限。

1. 合并修复分支前，确认 `DUP_WRITE_ENABLED` 未设置或为 false；运行模拟测试 CI。
2. 检查 Secret 名称：日报必需 FEISHU_APP_ID、FEISHU_APP_SECRET、WIKI_TOKEN、SUPABASE_URL、SUPABASE_SERVICE_KEY；查重前三项。推送至少配置 WECOM_WEBHOOK 或 SERVERCHAN_KEY。GH_PAT、用户/刷新令牌不再供这两个任务使用；不要直接删除，先排查其他脚本依赖。
3. 在飞书隔离副本配置应用访问，先运行查重预演，核对目标日期、行号、重复明细。模拟单元测试不证明真实飞书权限或保护范围 API 正确。
4. 启用工作流并以只读模式手动运行查重：

```bash
gh workflow enable dup_check.yml -R gummy1803-ai/blogger-tracker
gh workflow run dup_check.yml -R gummy1803-ai/blogger-tracker -f target_date=2026-10-18 -f is_final=false
gh run list -R gummy1803-ai/blogger-tracker --workflow dup_check.yml --limit 5
```

5. 日报认证和数据库配置核查完成后，手动恢复一天，再启用定时。日报会写 Supabase 并发送通知，不要反复补跑同一天以免重复通知。

```bash
gh workflow enable sync.yml -R gummy1803-ai/blogger-tracker
gh workflow run sync.yml -R gummy1803-ai/blogger-tracker -f date=2026-10-17
```

6. 生产预演明细与备份核对无误后，设置 `DUP_WRITE_ENABLED=true` 恢复每天北京时间21:00–23:45每15分钟及次日00:00查重标记。锁表仍关闭。若需锁表，先在副本验证官方保护范围接口的请求/响应、管理员可编辑性、已有保护幂等处理、午夜日期和失败后恢复，再单独更改工作流。
7. 检查后续 event=schedule 的运行及业务结果。不要把手动运行成功视为定时恢复证明。长期无代码活动的公共仓库仍可能再次停用，需要外部监测或迁移到适当的持续调度服务。

## 回退

立即把 DUP_WRITE_ENABLED 设为 false，必要时 `gh workflow disable dup_check.yml -R gummy1803-ai/blogger-tracker`；不会撤销已写标记。恢复标记前按备份逐条确认，不批量清空H列。此修复没有生产写入或锁表操作。
