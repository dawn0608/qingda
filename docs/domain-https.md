# 公网入口与域名切换

公网入口以服务器根`PUBLIC_URL`和已核验的原生Nginx配置为准。运维名称调整保持现有入口；不能根据历史文档自动切换域名、证书、回调或停站状态。备案展示只使用实际审核通过的号码。

公开版 `deploy/domain.py` 的 `DOMAIN` 使用 `example.com` 占位；使用前须明确配置为本部署的真实目标，`LINEAGE` 默认由目标域名派生，已有证书目录不同时须同步配置。占位配置不会执行运维动作。`evals/frontend/notices-contract.js` 的浏览器检查也须显式传入已授权的 `expectedOrigin`。

`deploy/domain.py`保留未来域名切换的受控入口：prepare保存当前数据库、配置和网关；pause设置`/etc/qingda/outbound-paused`；approve读取真实审批证据；certificate使用Certbot；activate与bind分别启用源地址和逐用户绑定；resume在检查点确认后恢复Worker。阶段共用现有存储锁，调用前持久化意图，不确定结果须核实后处理。

停站标记阻止发布、重启或机器启动提前恢复Worker。Web可在回环提供健康检查；停站期间公网仅保留证书挑战。日备包含停站标记、域名检查点、网关和服务单元。

切换域名必须同步所有用户回调和桌面源地址，保留用户ID、配对及业务数据；域名Cookie变化需要重新登录。原生Nginx配置拒绝未授权Host/SNI，HTTP重定向固定源，不重定向回调POST。证书沿用现有自动续期。

验收包括真实证书与续期、Web和Worker版本、逐用户回调。备份及证据按[保留规则](storage-retention.md)保护，不根据文档删除旧证书或未验收材料。

Certbot 的运行环境和状态目录分别为 `/opt/qingda-acme`、`/var/lib/qingda-acme`。改名保留已有证书标识、账户和私钥，联动迁移续期配置中的目录、Nginx 引用及定时服务；核验 `nginx -t`、HTTPS、续期演练和停站状态，不触发域名切换或逐用户回调重绑。
