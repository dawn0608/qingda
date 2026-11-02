# 原生 Nginx

HTTPS 入口由服务器原生 Nginx 管理，配置 `/etc/nginx/nginx.conf`。`deploy/proxy.py` 负责读取、精确更新、检查与重载；配置失败恢复原内容。当前公网源以 `PUBLIC_URL` 与已核验的生产配置为准。

`deploy/nginx_routes.py` 保存业务路由。管理页、API、文件上传与逐用户回调分别配置超时和大小；回调发布缓冲仅改变该路由的后端端口，不改业务入口。日志不记录含密钥的路径、查询或请求正文。

证书使用现有原生 Certbot 与 `qingda-certificate-renew.timer`。`deploy/native_nginx.py renew` 续期并重载 Nginx，`--dry-run` 用于已授权续期演练。证书、挑战目录和 systemd 配置纳入项目备份。

域名生命周期操作由 `deploy/domain.py` 管理；停站标记须同时阻止发布和重启恢复 Worker。不存在 NPM 或容器网关切换入口。
