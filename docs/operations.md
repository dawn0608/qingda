# 配置、部署与恢复

已确认任务在既有生产环境连续完成检查、提交推送、部署和验收，同一范围不重复确认；切换公网入口、扩展目标环境或删除历史数据不属于常规交付。公开仓库不保存生产配置，路径和服务名称沿用既有约定，域名及证书须按各自部署配置。

## 部署环境约定

源码 `/opt/qingda`；PostgreSQL `wechat_manager` 仅监听回环；主密钥 `/etc/qingda/master.key`；业务附件位于根 `DATA_DIR`。唯一配置为项目根 `.env`，字段以 `.env.example` 和 `Config` 为准。

微信 Token 和 DeepSeek 密钥按用户加密保存，回调密钥按用户生成。语音固定使用阿里百炼北京 `qwen-audio-3.1-asr-flash`，API Key 与 DeepSeek 分开在前端按用户配置、由同一 Vault 加密保存；不配置共享语音环境密钥，旧腾讯环境变量由发布入口清除。凭据不进入日志、源码和发布包；无配置时仅语音失败。和风天气 Host 和 Key 由各用户在系统设置中成组保存，Key 由 Vault 加密；不迁移旧共用凭据，无配置时仅天气工具返回尚未配置。发布入口在受保护配置备份后清除旧 QWEATHER_API_HOST、QWEATHER_API_KEY。服务端环境不再配置共享微信 Token、管理员迁移手机号、百炼或天气密钥。邮箱验证码使用 SMTP TLS，手机验证码使用阿里云号码认证；供应商调用前持久预占额度，失败或不确定结果不自动重发。

## 构建与发布

1. 核对工作区、目标远程、生产提交、发布清单、配置及在途任务；只提交本次改动。
2. 必要时在服务器隔离库实际恢复备份，验证结构、权限和数据转换。项目回归入口 `scripts/check.py` 仅接受服务器 `TEST_DATABASE_URL`。
3. 完成必要检查，提交推送并等待对应提交的 CI 通过；构建前端并使用既有发布入口制作可核对产物。
4. 用 `scripts/build_release.py --baseline <已核验线上快照> --nginx-sha256 <网关哈希>` 生成带源码、依赖和产物清单的发布包。
5. 核对目标远程提交。上传受限项目中转目录，复用 `deploy/update.py <发布包> <SHA256>`。已构建的依赖环境可用 `--prepared` 复用。
6. 发布器核对漂移、暂存回调、停止业务进程、备份数据库、显式升级、切换源码与依赖、恢复 Web、导入缓冲并启动 Worker。停站标记存在时 Worker 保持停止。
7. 核对 API、Worker、前端与运行版本，完成本次生产业务验收。只接受与当前提交相符的验收记录。

公网源沿用 `PUBLIC_URL` 和原生 Nginx 当前配置。证书通过 `qingda-certificate-renew.timer` 调用原生 Certbot 续期；不使用容器网关。域名切换见[域名与 HTTPS](domain-https.md)。

## 故障恢复

回滚仅复用已验证可恢复的 Git 版本、发布包、依赖环境及配置，避免反复全量备份。失败按原案例复现、修复和复验；无法验证的范围如实报告，不将恢复进程等同于业务验收完成。

发布失败时先核对数据库实际版本。结构未变化可恢复已核验的同结构源码和依赖；结构提交后保留最新数据库与回调缓冲，使用同结构修复版本恢复服务。禁止通过删除版本号或恢复旧库丢弃新数据。

业务启动不会自动迁移。未知发送结果不会自动重试，桌面已开始但未确认的操作只核对原聊天；不能把租约到期当作重新执行许可。

## 日备与回收

```sh
/opt/qingda/.venv/bin/python /opt/qingda/deploy/server.py backup
/opt/qingda/.venv/bin/python /opt/qingda/deploy/storage_retention.py
```

第二条默认预览。生产验收通过后才接受对应发布并按保留规则回收。未验收、失败和显式保护材料继续保留；详见[存储保留](storage-retention.md)。

## 旧名称迁移与恢复映射

运维名称为 `qingda`：服务前缀、系统账号及组；应用 `/opt/qingda`，配置 `/etc/qingda`，数据 `/var/lib/qingda`，备份 `/var/backups/qingda`；证书环境 `/opt/qingda-acme`、状态 `/var/lib/qingda-acme`。专用账号保留原 UID/GID，数据库、角色、Python 模块和内部软件包仍使用既有名称。公网源、端口、API、用户回调及证书标识不随运维改名变更。

原 `wechat-manager` 路径与服务一一映射到上述新名。正常命令只使用新布局；`QINGDA_DEPLOY_LAYOUT=legacy` 仅供显式引导发布和旧布局恢复，不能按目录存在情况自动回退。历史文档、备份文件和回执不批量改写。

迁移使用同一提交的已核验候选代码及发布包，在生产源码外的受限中转目录执行：

```sh
python deploy/rename_project.py --migrate-project-name --state /var/tmp/qingda-name-migration-<提交> --check
python deploy/rename_project.py --migrate-project-name --state /var/tmp/qingda-name-migration-<提交> --bundle <发布包> --sha256 <SHA256>
```

入口持有新旧发布锁，核对路径、账户、结构版本与源码基线，先在显式旧布局发布同一版本，再记录目录 inode、原服务状态及配置。依赖从官方 HTTPS 源预先校验并构建为固定哈希的 wheel；不继承主机不安全的 HTTP 镜像，也不降低 TLS 校验。重建使用迁移目录外、版本一致的系统解释器。切换窗口暂停定时任务、缓冲回调并排空回复；目录移动后保持原账号 UID/GID，在最终路径离线重建应用与 Certbot 环境，恢复新服务、回调及定时任务。临时旧路径兼容链接只在切换窗口存在；原环境及配置继续保护。目标冲突、资源被替换或迁移记录不一致时停止，不覆盖。

迁移完成后重复执行只核对记录，不再次移动资源。中断时保留最新数据库、缓冲和迁移目录，检查 `state.json` 与受限日志后使用同一候选工具恢复旧布局：

```sh
python deploy/rename_project.py --migrate-project-name --state <原迁移记录目录> --rollback
```

恢复后仍是引导发布的同一业务版本；原数据库不回退。若还需恢复迁移前源码，须先恢复旧布局，再以 `QINGDA_DEPLOY_LAYOUT=legacy` 使用核验过的发布备份；禁止在新布局直接回退含旧路径的版本。恢复失败标记 `needs_repair`，保留回调缓冲及所有材料。迁移证据和受保护材料最终登记在新备份根目录，不以较新发布解除旧任务保护。

## 生产验收边界

本地静态检查、发布构建与 CI 不代替生产验收。默认只读核对真实账号、聊天、已有媒体、运行日志及用户隔离，不伪造会话、触发模型评测或主动发送测试微信。需要微信实收时由用户触发并单独记录；未完成实收不记为通过。命名迁移另外核对旧服务停用、新服务唯一运行、路径权限、停站行为、证书续期、日备及隔离恢复。
