"""在显式服务器测试库核验单表备份与真实恢复，不访问业务库。"""

import importlib
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="需要服务器隔离 PostgreSQL")
def test_scoped_backup_restores_only_requested_table(tmp_path, monkeypatch):
    url = os.environ["TEST_DATABASE_URL"]
    parsed = urlsplit(url)
    assert "test" in parsed.path and parsed.path != "/qingda"
    server = importlib.import_module("server")
    retention = importlib.import_module("storage_retention")
    schema = "scoped_" + uuid.uuid4().hex
    app, root = tmp_path / "app", tmp_path / "backups"
    app.mkdir()
    root.mkdir()
    (app / ".env").write_text("DATABASE_URL=" + url + "\n")
    monkeypatch.setattr(server, "APP", app)
    monkeypatch.setattr(server, "ROOT", root)
    monkeypatch.setattr(retention, "ROOT", root)
    monkeypatch.setattr(server, "storage_lock", lambda: retention.storage_lock(tmp_path / "lock"))
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            conn.execute(f'CREATE TABLE "{schema}".kept(id integer PRIMARY KEY, body text)')
            conn.execute(f'CREATE TABLE "{schema}".untouched(id integer PRIMARY KEY)')
            conn.execute(f'INSERT INTO "{schema}".kept VALUES(1, %s)', ("unique core data",))
            conn.execute(f'INSERT INTO "{schema}".untouched VALUES(7)')
            result = server.backup(
                reason="isolated irreversible-change rehearsal",
                tables=[schema + ".kept"],
                files=[],
                release_condition="test complete",
            )
            dump = Path(result["backup"]) / "data.dump"
            listing = subprocess.check_output(["pg_restore", "--list", str(dump)], text=True)
            assert "kept" in listing and "untouched" not in listing
            with pytest.raises(subprocess.CalledProcessError):
                server.backup(
                    reason="isolated output limit",
                    tables=[schema + ".kept"],
                    files=[],
                    release_condition="test complete",
                    max_bytes=1,
                )
            failed = [p for p in root.iterdir() if p != dump.parent]
            assert len(failed) == 1 and not (failed[0] / "data.dump").exists()
            assert conn.execute(f'SELECT count(*) FROM "{schema}".kept').fetchone()[0] == 1
            conn.execute(f'DROP TABLE "{schema}".kept')
            from qingda.migration import pg_environment

            subprocess.run(
                [
                    "pg_restore",
                    "--dbname",
                    parsed.path.lstrip("/"),
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    str(dump),
                ],
                env=pg_environment(url),
                check=True,
                capture_output=True,
            )
            assert conn.execute(f'SELECT * FROM "{schema}".kept').fetchall() == [(1, "unique core data")]
            assert conn.execute(f'SELECT * FROM "{schema}".untouched').fetchall() == [(7,)]
        finally:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
