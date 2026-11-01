"""原生 PostgreSQL 日备与恢复演练；复用既有保留入口。"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import time

from dotenv import dotenv_values

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from proxy import backup_state
from storage_retention import configured, register, sha, storage_lock

APP = pathlib.Path("/opt/wechat-manager")
ROOT = pathlib.Path("/var/backups/wechat-manager")


def backup():
    sys.path.insert(0, str(APP / "backend"))
    from update import pg_url
    from wechat_manager.migration import backup_database, restore_database
    from wechat_manager.store import Store

    values = dotenv_values(APP / ".env")
    url = values["DATABASE_URL"]
    with storage_lock():
        folder = ROOT / ("daily-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
        folder.mkdir(mode=0o700)
        register(folder, "daily", "preparing")
        backup_database(url, folder / "manager.dump")
        for key, default, name in [
            ("MASTER_KEY_FILE", "/etc/wechat-manager/master.key", "master.key"),
        ]:
            shutil.copy2(values.get(key) or default, folder / name)
        shutil.copy2(APP / ".env", folder / "root-env")
        with tarfile.open(folder / "attachments.tar.gz", "w:gz") as archive:
            data = pathlib.Path(values.get("DATA_DIR") or "/var/lib/wechat-manager")
            for name in ("attachments", "incoming-media"):
                if (data / name).exists():
                    archive.add(data / name, arcname=name)
        backup_state(folder / "proxy-state.tar.gz")
        database = "wechat_manager_backup_check_" + str(os.getpid())
        subprocess.run(
            ["runuser", "-u", "postgres", "--", "createdb", "-O", "wechat_manager_migration", database],
            check=True,
            capture_output=True,
        )
        try:
            restore_database(pg_url(database), folder / "manager.dump")
            store = Store(pg_url(database))
            assert store.health()["database_ready"]
            store.close()
        finally:
            if not database.startswith("wechat_manager_backup_check_"):
                raise ValueError("恢复演练库名称非法")
            subprocess.run(
                ["runuser", "-u", "postgres", "--", "dropdb", database], check=True, capture_output=True
            )
        manifest = {p.name: sha(p) for p in folder.iterdir() if p.is_file() and p.name != "retention.json"}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
        register(folder, "daily", "completed")
        result = configured().run(apply=True, lock=False)
        print(
            json.dumps(
                {
                    "backup": str(folder),
                    "database_backend": "postgresql",
                    "restore_check": True,
                    "storage_retention": {"deleted": len(result["deleted"]), "policy": result["policy"]},
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup"])
    parser.parse_args()
    os.umask(0o077)
    backup()
