# 配置、部署与恢复

提交和推送不构成部署授权；以下步骤仅在明确授权的目标环境执行。公开仓库不保存生产配置，路径和服务名称沿用既有约定，域名及证书须按各自部署配置。

## 部署环境约定

源码 `/opt/wechat-manager`；PostgreSQL `wechat_manager` 仅监听回环；主密钥 `/etc/wechat-manager/master.key`；业务附件位于根 `DATA_DIR`。唯一配置为项目根 `.env`，字段以 `.env.example` 和 `Config` 为准。

微信 Token 和 DeepSeek 密钥按用户加密保存，回调密钥按用户生成。语音固定使用阿里百炼北京 `qwen-audio-3.1-asr-flash`，API Key 与 DeepSeek 分开在前端按用户配置、由同一 Vault 加密保存；不配置共享语音环境密钥，旧腾讯环境变量由发布入口清除。凭据不进入日志、源码和发布包；无配置时仅语音失败。和风天气 Host 和 Key 由各用户在系统设置中成组保存，Key 由 Vault 加密；不迁移旧共用凭据，无配置时仅天气工具返回尚未配置。发布入口在受保护配置备份后清除旧 QWEATHER_API_HOST、QWEATHER_API_KEY。服务端环境不再配置共享微信 Token、管理员迁移手机号、百炼或天气密钥。邮箱验证码使用 SMTP TLS，手机验证码使用阿里云号码认证；供应商调用前持久预占额度，失败或不确定结果不自动重发。

## 构建与发布

1. 核对工作区、目标远程、生产提交、发布清单、配置及在途任务；只提交本次改动。
2. 必要时在服务器隔离库实际恢复备份，验证结构、权限和数据转换。项目回归入口 `scripts/check.py` 仅接受服务器 `TEST_DATABASE_URL`。
3. 提交后构建前端，使用现有发布入口生成带源码、依赖与前端产物哈希的发布包。
4. 用 `scripts/build_release.py --baseline <已核验线上快照> --nginx-sha256 <网关哈希>` 生成带源码、依赖和产物清单的发布包。
5. 推送并核对目标远程提交。上传受限项目中转目录，复用 `deploy/update.py <发布包> <SHA256>`。已构建的依赖环境可用 `--prepared` 复用。
6. 发布器核对漂移、暂存回调、停止业务进程、备份数据库、显式升级、切换源码与依赖、恢复 Web、导入缓冲并启动 Worker。停站标记存在时 Worker 保持停止。
7. 核对 API、Worker、前端与运行版本，完成本次生产业务验收。只接受与当前提交相符的验收记录。

公网源沿用 `PUBLIC_URL` 和原生 Nginx 当前配置。证书通过 `wechat-manager-certificate-renew.timer` 调用原生 Certbot 续期；不使用容器网关。域名切换见[域名与 HTTPS](domain-https.md)。

## 故障恢复

发布失败时先核对数据库实际版本。结构未变化可恢复已核验的同结构源码和依赖；结构提交后保留最新数据库与回调缓冲，使用同结构修复版本恢复服务。禁止通过删除版本号或恢复旧库丢弃新数据。

业务启动不会自动迁移。未知发送结果不会自动重试，桌面已开始但未确认的操作只核对原聊天；不能把租约到期当作重新执行许可。

## 日备与回收

```sh
/opt/wechat-manager/.venv/bin/python /opt/wechat-manager/deploy/server.py backup
/opt/wechat-manager/.venv/bin/python /opt/wechat-manager/deploy/storage_retention.py
```

第二条默认预览。生产验收通过后才接受对应发布并按保留规则回收。未验收、失败和显式保护材料继续保留；详见[存储保留](storage-retention.md)。
