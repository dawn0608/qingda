"""显式 PostgreSQL 结构迁移及原生备份。"""

import os
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psycopg

VERSION = 59
SQL = Path(__file__).with_name("sql") / "058_baseline.sql"


def migrate_database(url):
    from .store import configure, record_factory

    with psycopg.connect(url, autocommit=True, row_factory=record_factory) as conn:
        configure(conn)
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(17950,2)")
            if conn.execute(
                "SELECT 1 FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE n.nspname=current_schema() AND (r.rolname='qingda_owner' OR (r.rolname='pg_database_owner' AND (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database())='qingda_owner')) AND pg_has_role(current_user,'qingda_owner','USAGE')"
            ).fetchone():
                conn.execute("SET LOCAL ROLE qingda_owner")
            if not conn.execute("SELECT to_regclass('schema_versions')").fetchone()[0]:
                conn.execute(SQL.read_text(encoding="utf-8"))
            current = conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
            if current in (51, 52, 53):
                conn.execute(SQL.with_name("054_input_waits.sql").read_text(encoding="utf-8"))
                current = 54
            if current in (54, 55, 56, 57):
                conn.execute(SQL.with_name("058_remove_bridge.sql").read_text(encoding="utf-8"))
                current = 58
            if current == 58:
                conn.execute(SQL.with_name("059_chat_history_deletions.sql").read_text(encoding="utf-8"))
                current = 59
            if current != VERSION:
                raise RuntimeError("数据库结构不受支持，需要受控升级")


def pg_environment(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("postgresql", "postgres") or not parsed.path.strip("/"):
        raise ValueError("必须指定 PostgreSQL 数据库")
    env = dict(os.environ)
    env.update(
        PGHOST=parsed.hostname or "127.0.0.1",
        PGPORT=str(parsed.port or 5432),
        PGUSER=unquote(parsed.username or ""),
        PGPASSWORD=unquote(parsed.password or ""),
        PGDATABASE=unquote(parsed.path.lstrip("/")),
        PGCONNECT_TIMEOUT="5",
    )
    return env


def backup_database(store_or_url, target):
    url = getattr(store_or_url, "url", store_or_url)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    parsed = urlsplit(url)
    if os.name != "nt" and os.geteuid() == 0 and parsed.hostname in ("127.0.0.1", "localhost", None):
        # RLS业务身份不能证明全库备份；服务器受限运维以本机peer身份导出全部用户。
        env = dict(os.environ)
        env.update(
            PGHOST="/var/run/postgresql", PGUSER="postgres", PGDATABASE=unquote(parsed.path.lstrip("/"))
        )
        env.pop("PGPASSWORD", None)
        with target.open("wb") as stream:
            subprocess.run(
                ["runuser", "-u", "postgres", "--", "pg_dump", "--format=custom", "--no-owner", "--no-acl"],
                env=env,
                stdout=stream,
                stderr=subprocess.PIPE,
                check=True,
            )
    else:
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--file", str(target)],
            env=pg_environment(url),
            check=True,
            capture_output=True,
        )
    target.chmod(0o600)
    subprocess.run(["pg_restore", "--list", str(target)], check=True, capture_output=True)
    return {"format": "postgresql-custom", "bytes": target.stat().st_size}


def restore_database(url, source):
    subprocess.run(
        [
            "pg_restore",
            "--dbname",
            pg_environment(url)["PGDATABASE"],
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            str(source),
        ],
        env=pg_environment(url),
        check=True,
        capture_output=True,
    )
