"""原生 Nginx 域名配置与现有 Certbot 证书续期。"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from nginx_routes import routes
from proxy import ACME, Gateway
from storage_retention import ROOT, storage_lock

WEBROOT = ACME / "webroot"
CERTBOT = Path("/opt/wechat-manager-acme/bin/certbot")
LINEAGE = ACME / "certbot/live/codex-console"


def render_domain(domain, mode="active", test=False, lineage=None):
    """独立域名站点；默认虚拟主机不暴露任何业务，停站仍可验证ACME。"""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", domain):
        raise ValueError("域名格式不符")
    if mode not in {"active", "paused", "challenge"}:
        raise ValueError("未知域名站点阶段")
    http, https = ("127.0.0.1:18080", "127.0.0.1:18443") if test else ("80", "443")
    pid = "/run/wechat-domain-candidate.pid" if test else "/run/nginx.pid"
    certificate = lineage or ACME / "certbot/live" / domain.replace(".", "-")
    default_certificate = lineage or (certificate if (certificate / "fullchain.pem").exists() else LINEAGE)
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(certificate)):
        raise ValueError("证书路径格式不符")
    body = routes()
    body += "\nlocation = / { return 308 /wechat/; }\n"
    tls = ""
    if mode == "active":
        tls = f"""server {{
    listen {https} ssl;
    server_name {domain};
    ssl_certificate {certificate}/fullchain.pem;
    ssl_certificate_key {certificate}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    if ($host != {domain}) {{ return 444; }}
    if ($ssl_server_name != {domain}) {{ return 421; }}
    {body}
  }}"""
    redirect = f"return 308 https://{domain}$request_uri;" if mode == "active" else "return 503;"
    return f"""# wechat-manager managed domain gateway: {mode}
user www-data;
worker_processes 2;
pid {pid};
error_log /dev/null crit;
events {{ worker_connections 512; }}
http {{
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  sendfile on;
  keepalive_timeout 65;
  server_tokens off;
  access_log off;
  log_format wechat_manager_safe '$msec $server_port $request_method $status $body_bytes_sent';
  server {{ listen {http} default_server; server_name _;
    location ^~ /.well-known/acme-challenge/ {{ root {WEBROOT}; }}
    location / {{ return 444; }}
  }}
  server {{ listen {http}; server_name {domain};
    location ^~ /.well-known/acme-challenge/ {{ root {WEBROOT}; }}
    location / {{ {redirect} }}
  }}
  server {{ listen {https} ssl default_server; server_name _;
    ssl_certificate {default_certificate}/fullchain.pem;
    ssl_certificate_key {default_certificate}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    return 444;
  }}
  {tls}
}}
""".encode()


def run(*args, timeout=90, cwd=None, data=None):
    result = subprocess.run([str(a) for a in args], input=data, capture_output=True, timeout=timeout, cwd=cwd)
    if result.returncode:
        # 命令和配置可能包含密钥；调用者只能看到无敏感参数的错误。
        raise RuntimeError("运维命令失败: " + str(args[0]))
    return result.stdout.decode()


def folder_path(value):
    value = Path(value)
    if value.is_symlink() or value.absolute() != value.resolve() or value.parent != ROOT:
        raise ValueError("迁移目录必须为项目备份根的直接子目录")
    return value


def renew(dry_run=False):
    args = [
        str(CERTBOT),
        "renew",
        "--config-dir",
        str(ACME / "certbot"),
        "--work-dir",
        str(ACME / "acme-work"),
        "--logs-dir",
        str(ACME / "acme-logs"),
        "--webroot-path",
        str(WEBROOT),
        "--non-interactive",
        "--no-random-sleep-on-renew",
    ]
    if dry_run:
        args.append("--dry-run")
    run(*args, timeout=300)
    Gateway().reload()
    return {"renewal_ok": True, "dry_run": dry_run}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["renew"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("仅限服务器 root 运维")
    with storage_lock():
        print(json.dumps(renew(args.dry_run)))
