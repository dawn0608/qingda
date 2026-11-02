"""唯一线上运维命名；旧布局仅供显式迁移与恢复，不按文件存在情况回退。"""

import os
from pathlib import Path

PROFILE = os.environ.get("QINGDA_DEPLOY_LAYOUT", "qingda")
if PROFILE not in {"qingda", "legacy"}:
    raise ValueError("unknown_deployment_layout")
NAME = "wechat-manager" if PROFILE == "legacy" else "qingda"
APP = Path("/opt") / NAME
CONFIG = Path("/etc") / NAME
DATA = Path("/var/lib") / NAME
ROOT = Path("/var/backups") / NAME
ACME = Path("/var/lib") / (NAME + "-acme")
CERTBOT = Path("/opt") / (NAME + "-acme/bin/certbot")
LOCK = Path("/run/lock") / (NAME + "-release.lock")


def unit(suffix):
    return NAME + "-" + suffix


def service_template(source):
    """源文件始终使用新名；引导发布只转换安装到 systemd 的运维引用。"""
    text = Path(source).read_text().replace("qingda", NAME)
    if PROFILE == "legacy":
        text = text.replace("[Service]\n", "[Service]\nEnvironment=QINGDA_DEPLOY_LAYOUT=legacy\n")
    return text


def check_rollback_layout(files):
    if PROFILE != "legacy" and "deploy/wechat-manager-web.service" in files:
        raise ValueError("legacy_backup_requires_layout_recovery")
