# PostgreSQL 存储

## 数据边界

项目业务数据和持久任务状态统一使用服务器 PostgreSQL。服务端根 `DATABASE_URL` 必填。

聊天原件、有效用户、登录会话、微信身份、配置和任务回执持续保存；用户确认清空会话时，删除该批聊天、媒体及相关任务数据，范围见 [清空聊天记录](chat-history-deletion.md)。媒体定位信息独立保存，按需下载；平台资源失效保留记录和失败原因。启动不会自行清理历史，也不会重放历史任务。

## 初始化与权限

`backend/wechat_manager/sql/058_baseline.sql` 是初始化基线，`059_chat_history_deletions.sql` 增补逐用户清空进度，当前结构为59。显式执行 `python -m wechat_manager migrate-db` 创建新库结构和主密钥；不预置账号，用户通过验证码首次登录创建。旧库51–53先补齐等待记录，再从54–57显式升级至58，最后升级至59；迁移前保护原始备份，服务启动不执行迁移。等待及清空记录沿用逐用户RLS和复合外键，重启不重新消费旧回复。

生产使用 `wechat_manager_owner` 管理对象，migration 身份执行结构变更，app 身份运行 Web/Worker。业务表强制 RLS；用户范围来自验证后的会话、逐用户回调，缺少范围默认拒绝。

新库与升级库的表、列、索引、约束、视图、触发器、RLS 和运行授权须等价。结构变化在服务切换期间显式完成，提交后保留最新数据，只允许同结构恢复或前向修复。

## 持久状态

- `message_contents` 保存原文和必要 XML；`message_records`、媒体定位及事件关联引用同一内容。
- `history_dedup` 保存去重标识，`history_id_watermarks` 分配单调 ID，均不能因名称带 history 而清空。
- `tasks.handled_message_ids` 是回复对应的原文关联；历史与新任务使用同一读取契约。模型决策保留其真实来源，不为历史任务补造决策。
- `account_automation_state` 保存账号自动化初始化与人工设置状态。
- 发送租约、发送水位、防重、未知结果和 `migration_inbox` 发布回调缓冲独立保留。

## 备份与连接

使用 `deploy/server.py backup` 生成 PostgreSQL custom-format 备份，实际恢复检查成功后才轮转。备份包含配置、主密钥、媒体及网关证书状态，放在 `/var/backups/qingda` 并走统一保留入口。

管理工具通过已授权的 SSH 隧道连接服务器本机 PostgreSQL，使用对应管理角色。不得开放数据库公网端口。
