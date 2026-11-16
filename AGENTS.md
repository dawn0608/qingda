# 青答 Qingda 项目开发规范

继承全局规范。

青答是通用微信 AI 助手与管理台。启动、配置和操作命令见 [README](README.md)，专题文档见文末索引。对外仓库为 GitHub `dawn0608/qingda`，不自动同步 Gitee。

## 工程与数据

- 沿用单仓库模块化单体。后端用 Python、uv、FastAPI/Pydantic 和 psycopg，前端用 React、TypeScript strict、Vite、npm 和既有 CSS／组件。模型流程优先复用具名业务函数、持久任务和 Worker。
- PostgreSQL 是唯一业务数据库，沿用 SQL 迁移、RLS 和现有任务、缓存及协调机制，不自动引入 ORM、替代数据库或队列。根 `DATABASE_URL` 必填。本地配置用根 `.env`，公开示例为根 `.env.example`，忽略规则集中在根 `.gitignore`。CI、部署及测试按环境注入，测试配置与生产隔离。
- 既有运行环境为 Linux、systemd、原生 Nginx 和 Certbot。模块、包、数据库、角色和运维标识统一为 `qingda`，不提供旧名别名或运行兼容，网址和微信协议沿用各自契约。依赖使用可信源，保持 TLS 开启。
- 业务读取、回调、Worker、媒体和检索都基于服务端验证的用户范围。PostgreSQL RLS 缺省拒绝，同时核对账号、微信身份、会话及成员，扩容与恢复遵守相同范围。微信接入、模型及天气凭据按用户在服务端加密保存，不以共享凭据或旧密钥兜底。Token 本人回显和模型密钥脱敏按专题契约执行。
- 结构升级通过已授权的迁移入口显式执行，启动时不迁移、清理或重放历史任务。聊天原件、业务历史和防重记录持续保存，用户确认清空会话时按清空专题执行。跨进程任务、容量与预算以共享持久记录为准，领取、续期、写入及释放核验所有权。发送中或结果未知的消息不自动重放。

## 开发与验证

- 业务提示词在根 `prompts/` 分类维护 Markdown 模板，纳入版本管理并随包发布。
- 模型、提示词、工具、检索或业务规则变更时，使用现有 `evals/` 回归相关业务案例，并按授权范围进行真实调用验收。
- 常规 CI 检查 Ruff、前端 Node 测试、类型及构建。完整后端回归仅在专用 Linux／PostgreSQL 隔离环境设置 `TEST_DATABASE_URL` 后执行 `uv run python scripts/check.py`。
- 模型开发测试沿用全局预授权，清单凭据仅注入隔离测试进程，其余真实调用按项目授权执行。生产业务验收默认只读，收发微信由用户触发。故障注入、破坏性测试与恢复演练在服务器隔离环境执行。

## 生产发布与备份

- 属于既有明确生产部署授权范围的任务，CI 通过后继续发布和验收，用户限定交付到 PR 时止于 PR。变更公网入口或停站状态须另有授权。
- 发布以移动现有文件完成过渡，运行核验后回收。Git 回滚通过指定提交重新构建，不覆盖最新业务库。
- 清理依据[存储保留](docs/storage-retention.md)定位项目路径及历史原件保护要求，备份与保留策略以全局规范为准。唯一历史业务原件、明确要求留存的评测材料和必要恢复依赖按用途保留。

## 按任务查阅

按任务查阅当前契约；历史记录仅作证据，不构成执行授权。

| 涉及任务 | 先读文档 |
| --- | --- |
| 产品、架构、数据与迁移 | [产品](docs/prd.md)、[架构](docs/design.md)、[存储](docs/postgresql-storage.md) |
| 认证、账户与微信接入 | [认证](docs/authentication.md)、[账户设置](docs/account-settings.md)、[接入](docs/account-management.md) |
| 页面、草稿与号主资料 | [前端](docs/frontend-design.md)、[助手设定](docs/assistant-settings.md) |
| 好友、群聊与聊天记录 | [目录同步](docs/contact-discovery.md)、[前端](docs/frontend-design.md)、[清空记录](docs/chat-history-deletion.md) |
| 回复、唤醒、人工暂停与调度 | [回复策略](docs/reply-strategy.md)、[唤醒](docs/group-wakeup.md)、[恢复](docs/auto-reply-resume.md)、[队列](docs/reply-batches.md) |
| 模型、业务文件／媒体、上下文与工具 | [文件与媒体](docs/media.md)、[上下文](docs/conversation-context.md)、[工具](docs/auto-reply-tools.md)、[表达](docs/wechat-reply-style.md) |
| 测试与效果评测 | [验收](docs/verification.md) |
| 发布、域名与备份清理 | [运维](docs/operations.md)、[域名](docs/domain-https.md)、[存储保留](docs/storage-retention.md) |
