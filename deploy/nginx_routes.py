"""当前业务及逐用户回调的原生 Nginx 路由。"""

HOST = "127.0.0.1"


def routes():
    common = f"""
    proxy_pass http://{HOST}:17950;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Connection "";
    proxy_connect_timeout 2s;
    proxy_read_timeout 70s;
    proxy_next_upstream off;
"""
    return (
        """# qingda: 原生 Nginx 业务路由
access_log off;
error_log /dev/null crit;
location = /wechat { return 302 /wechat/; }
location ^~ /wechat/ {
  access_log /var/log/nginx/qingda_access.log qingda_safe;
  client_max_body_size 16m;
"""
        + common
        + """
}
location ^~ /wechatapi/callback/ {
  access_log /var/log/nginx/qingda-callback_access.log qingda_safe;
  client_max_body_size 16m;
  client_body_timeout 5s;
  proxy_pass http://"""
        + HOST
        + """:17950;
  proxy_http_version 1.0;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $remote_addr;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header Connection close;
  proxy_connect_timeout 1s; proxy_send_timeout 2s; proxy_read_timeout 2s;
  proxy_buffering off; proxy_request_buffering on; proxy_next_upstream off;
  limit_except GET POST { deny all; }
}
location / { add_header Cache-Control "no-store" always; return 410; }
"""
    )
