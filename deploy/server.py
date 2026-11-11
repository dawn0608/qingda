"""仅显式保护无法重新取得的核心数据；不提供默认全量日备。"""

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import nullcontext

from dotenv import dotenv_values

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from layout import APP, CONFIG, DATA, ROOT
from storage_retention import register, save_json, sha, storage_lock


def validate_scope(reason, tables, files, release_condition):
    if not isinstance(tables, list) or not isinstance(files, list):
        raise ValueError("backup_scope_must_be_explicit_lists")
    if not reason.strip() or not release_condition.strip() or not (tables or files):
        raise ValueError("critical_backup_requires_reason_scope_and_release_condition")
    if len(set(tables)) != len(tables) or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*", name) for name in tables
    ):
        raise ValueError("backup_tables_must_be_explicit_schema_and_table")
    selected = []
    for value in files:
        path = pathlib.Path(value)
        if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_file():
            raise ValueError("backup_files_must_be_explicit_regular_files")
        roots = [("config", CONFIG), ("data", DATA), ("app", APP)]
        root = next(((label, parent) for label, parent in roots if path.is_relative_to(parent)), None)
        if root is None or (root[0] == "app" and path != APP / ".env"):
            raise ValueError("backup_file_outside_persistent_scope")
        label, parent = root
        selected.append((path, pathlib.Path("files") / label / path.relative_to(parent)))
    if len({str(path) for path, _ in selected}) != len(selected):
        raise ValueError("duplicate_backup_file")
    return selected


def backup(*, reason, tables, files, release_condition, lock=True):
    selected = validate_scope(reason, tables, files, release_condition)
    with storage_lock() if lock else nullcontext():
        folder = ROOT / (
            "critical-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        )
        folder.mkdir(mode=0o700)
        register(folder, "protected", "preparing", reason)
        try:
            if tables:
                sys.path.insert(0, str(APP / "backend"))
                from qingda.migration import pg_environment

                values = dotenv_values(APP / ".env")
                env = pg_environment(values["DATABASE_URL"])
                prefix = []
                if os.geteuid() == 0 and env["PGHOST"] in {"127.0.0.1", "localhost"}:
                    env.update(PGHOST="/var/run/postgresql", PGUSER="postgres")
                    env.pop("PGPASSWORD", None)
                    prefix = ["runuser", "-u", "postgres", "--"]
                args = prefix + ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--strict-names"]
                for name in tables:
                    args += ["--table", ".".join('"' + part + '"' for part in name.split("."))]
                target = folder / "data.dump"
                with target.open("wb") as stream:
                    subprocess.run(args, env=env, stdout=stream, stderr=subprocess.PIPE, check=True)
                target.chmod(0o600)
                subprocess.run(["pg_restore", "--list", str(target)], check=True, capture_output=True)
            for source, relative in selected:
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                target.chmod(0o600)
                if sha(source) != sha(target):
                    raise ValueError("critical_file_changed_during_copy")
            hashes = {
                str(p.relative_to(folder)): sha(p)
                for p in folder.rglob("*")
                if p.is_file() and p.name != "retention.json"
            }
            save_json(
                folder / "manifest.json",
                {
                    "format": "qingda-critical-v1",
                    "reason": reason,
                    "tables": tables,
                    "files": [str(path) for path, _ in selected],
                    "sha256": hashes,
                    "release_condition": release_condition,
                    "created_at": time.time(),
                },
            )
            register(folder, "protected", "completed", reason)
            return {
                "backup": str(folder),
                "tables": tables,
                "file_count": len(selected),
                "restore_check": False,
            }
        except BaseException:
            register(folder, "protected", "needs_repair", "核心数据保护未完成，禁止据此执行不可逆操作")
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup"])
    parser.add_argument("--reason", required=True)
    parser.add_argument("--release-condition", required=True)
    parser.add_argument("--table", action="append", default=[])
    parser.add_argument("--file", action="append", default=[])
    args = parser.parse_args()
    os.umask(0o077)
    print(
        json.dumps(
            backup(
                reason=args.reason,
                tables=args.table,
                files=args.file,
                release_condition=args.release_condition,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
