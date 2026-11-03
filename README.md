# 青答 Qingda · 微信 AI 助手

[![静态检查与构建](https://github.com/dawn0608/qingda/actions/workflows/check.yml/badge.svg)](https://github.com/dawn0608/qingda/actions/workflows/check.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

青答是可自行部署的微信 AI 助手与管理台，集成账号管理、智能回复、聊天记录和系统设置。通过 WechatApi 接入微信，以邮箱或手机号验证码登录，首次验证后自动注册；不同用户的账号、密钥、聊天和任务相互隔离。

## 功能

- **微信管理**：查看账号资料、状态、好友和群聊目录，通过用户自己的 Token 配置接入及专属回调；微信登录与退出在接入平台完成。
- **智能回复**：按账号设置提示词、公开资料和回复范围，草稿自动保存，结束编辑后整份生效；支持文本、内置文字表情、普通静态图片及语音。
- **聊天记录**：按会话浏览、搜索已保存消息，查看已有图片缓存；点击助手回复查看[实际注入上下文与完整生成过程](docs/conversation-context.md#回复上下文查看)，并可在二次确认后清空会话记录及关联媒体。
- **逐用户配置**：分别管理微信接入、DeepSeek、阿里云百炼及和风天气配置，供应商密钥在服务端加密保存。
- **后台任务**：同一会话互斥，不同会话可并行准备答案；同一微信账号串行发送，发送完成后至少间隔 1 秒。不确定发送结果不自动重试，不重放历史任务。

当前版本固定使用 DeepSeek `deepseek-flash` 处理文本和图片、阿里百炼北京地域 `qwen-audio-3.1-asr-flash` 转写语音。视频、文件、动态图片及全部 `MsgType=47` 贴图不参与 AI 回复。关系证据不足时暂停回复，好友及入群事件不主动问候。模型和协议细节见[媒体说明](docs/media.md)及[回复策略](docs/reply-strategy.md)。

## 环境与外部服务

- Python 3.10（见 `.python-version`）、[uv](https://docs.astral.sh/uv/)、Node.js 22.19 以上、npm。
- PostgreSQL 17 及其命令行工具；语音解码需要 FFmpeg。
- 可用的 WechatApi Token；AI 功能需要自己的 DeepSeek、阿里百炼 API Key，天气功能需要和风天气 API Host 和 Key。
- 验证码登录需要配置 SMTP 邮件服务或阿里云号码认证服务，字段见根 `.env.example`。项目不提供默认管理员或共享供应商凭据。

后端使用 FastAPI、psycopg 与 PostgreSQL，前端使用 React、TypeScript 和 Vite。既有 Linux 运维入口使用 systemd、原生 Nginx 及 Certbot。

## 配置与启动

```sh
git clone https://github.com/dawn0608/qingda.git
cd qingda
cp .env.example .env
uv sync --locked
npm --prefix frontend ci
```

Windows PowerShell 将复制命令替换为 `Copy-Item .env.example .env`。唯一运行配置是项目根 `.env`，不要提交真实配置、数据库、媒体或密钥文件。

先准备独立 PostgreSQL 数据库及结构管理、迁移和业务角色，参考[初始化与权限](docs/postgresql-storage.md#初始化与权限)。编辑 `.env` 的 `DATABASE_URL`、验证码供应商等配置；结构升级使用迁移身份，Web/Worker 使用无超级用户和 `BYPASSRLS` 权限的业务身份。新库初始化与已有数据库升级是显式操作，服务启动不自动修改结构。

```sh
# 在 .env 中暂时配置迁移身份的 DATABASE_URL 后执行：
uv run python -m qingda migrate-db

# 将 .env 的 DATABASE_URL 改回业务身份后构建并启动：
npm --prefix frontend run build
uv run python -m qingda serve
```

另开一个位于项目根目录的终端启动 Worker：

```sh
uv run python -m qingda worker
```

默认管理台地址为 <http://127.0.0.1:17950/wechat/>。在系统设置中配置微信 Token 及各供应商密钥。接收真实微信回调需要接入平台能够访问的 HTTPS 根地址、相应反向代理和正确的 `PUBLIC_URL`；回环地址用于本机开发，不能接收公网回调。

## 检查与验收

本地和 GitHub CI 可执行以下静态检查、现有前端测试及构建，无需生产配置或数据库：

```sh
uv run ruff check backend deploy scripts
uv run ruff format --check backend deploy scripts
node --test frontend/tests/*.test.mjs frontend/src/*.test.mjs
npm --prefix frontend run build
```

GitHub CI 的绿色状态仅表示上述检查通过。完整后端回归仍使用专用 Linux/PostgreSQL 隔离环境和 `scripts/check.py`，部分夹具依赖 root/peer、Nginx、OpenSSL 等服务器条件；它不在默认 GitHub CI 中运行。程序测试、真实模型效果、生产功能和手机实收分别记录，详见[验收说明](docs/verification.md)。

## 部署与维护

线上运维统一使用 `/opt/qingda`、`qingda-*` 服务及 `qingda` 系统账号；Python 模块、软件包、数据库和角色也统一为 `qingda`，不提供旧名运行兼容。部署前按自己的环境配置路径、域名、证书及供应商服务，示例地址不代表公共演示站点。

本项目已确认任务默认连续完成必要检查、提交推送、CI 检查、既有生产环境部署及真实数据验收；同一范围无需重复确认部署。此约定由任务执行者落实，不表示 GitHub 已配置自动发布任务。部署后分别核对 API、Worker 和前端版本与哈希，业务验收默认只读，需要测试微信消息时由用户触发。配置及操作见[部署与恢复](docs/operations.md)、[域名与 HTTPS](docs/domain-https.md)和[备份保留](docs/storage-retention.md)。

## 文档

- [产品与界面](docs/prd.md)、[架构与状态](docs/design.md)
- [验证码认证](docs/authentication.md)、[PostgreSQL 存储](docs/postgresql-storage.md)
- [助手设定与号主公开资料](docs/assistant-settings.md)
- [回复策略](docs/reply-strategy.md)、[媒体理解](docs/media.md)、[联网工具](docs/auto-reply-tools.md)
- [开发规范](AGENTS.md)、[验收说明](docs/verification.md)

## 开源来源与许可

本项目自有代码采用 [MIT 许可证](LICENSE)，版权署名为 `2026 dawn0608`。随仓库提供的第三方字体、微信表情、分词资源及来源资料保留各自的权利和条款，详见[第三方声明](THIRD_PARTY_NOTICES.md)；项目 MIT 许可不替代这些资源的原始许可。

本开源版本于 2026-11-01 从 [Gitee wechat-manager](https://gitee.com/dawn0608/wechat-manager) 的 `master` 分支提交 `83eb0971b94c6ce59f20c830957c04fc4c4c2d2e` 整理而来，使用独立的 `main` 历史。公开快照已替换环境专属地址，并脱敏部分历史验收资料；原 Gitee 仓库及其提交历史保留，不自动双向同步。源项目名称仅作为历史出处保留；当前模块名为 `qingda`。
