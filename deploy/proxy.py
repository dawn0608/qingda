"""原生 Nginx 发布、检查及证书状态备份。"""

import os
import subprocess
import tarfile
from pathlib import Path

from dotenv import dotenv_values
from layout import ACME, APP, CONFIG, unit


def settings():
    return dotenv_values(APP / ".env")


def health_url():
    host = settings().get("WEB_BIND_HOST") or "127.0.0.1"
    return "http://" + ("[" + host + "]" if ":" in host else host) + ":17950/internal/health"


class Gateway:
    backend = "native"
    native = True

    def __init__(self):
        self.path = Path("/etc/nginx/nginx.conf")

    def read_bytes(self):
        return self.path.read_bytes()

    def write_bytes(self, body):
        with self.path.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())

    def check(self, body):
        if body != self.read_bytes():
            raise ValueError("候选配置尚未安装")
        try:
            subprocess.run(["nginx", "-t", "-c", str(self.path)], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            raise RuntimeError("网关配置检查失败；详细配置未输出") from None

    def reload(self):
        for command in (["nginx", "-t"], ["nginx", "-s", "reload"]):
            subprocess.run(command, check=True, capture_output=True)

    def put(self, body):
        before = self.read_bytes()
        try:
            self.write_bytes(body)
            self.check(body)
            self.reload()
        except BaseException:
            self.write_bytes(before)
            self.reload()
            raise


def backup_state(target):
    target = Path(target)
    paths = [
        Path("/etc/nginx"),
        ACME,
        (CONFIG / "outbound-paused"),
        (CONFIG / "domain-switch.json"),
    ]
    for name in (
        unit("worker.service"),
        unit("worker.service.d"),
        unit("web.service"),
        unit("backup.service"),
        unit("backup.timer"),
        unit("certificate-renew.service"),
        unit("certificate-renew.timer"),
    ):
        paths.append(Path("/etc/systemd/system") / name)
    with tarfile.open(target, "w:gz") as archive:
        for path in paths:
            if path.exists():
                archive.add(path, arcname=str(path).lstrip("/"))
    target.chmod(0o600)
