"""青答唯一运行布局；已退役的布局参数不可启用。"""

import os
from pathlib import Path

if os.environ.get("QINGDA_DEPLOY_LAYOUT") not in {None, "qingda"}:
    raise ValueError("retired_deployment_layout")
APP = Path("/opt/qingda")
CONFIG = Path("/etc/qingda")
DATA = Path("/var/lib/qingda")
ROOT = Path("/var/backups/qingda")
ACME = Path("/var/lib/qingda-acme")
CERTBOT = Path("/opt/qingda-acme/bin/certbot")
LOCK = Path("/run/lock/qingda-release.lock")


def unit(suffix):
    return "qingda-" + suffix


def service_template(source):
    return Path(source).read_text()


def require_project_identity(manifest):
    if manifest.get("project_identity") != "qingda":
        raise ValueError("project_identity_mismatch")
