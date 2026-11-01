"""PostgreSQL 显式迁移与原生备份恢复契约。"""

import json
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from test_database import database_url
from wechat_manager.migration import backup_database, migrate_database, restore_database
from wechat_manager.store import Store


def test_runtime_rejects_sqlite_and_missing_database():
    for value in ("sqlite:///manager.sqlite3", "", None):
        with pytest.raises(ValueError):
            Store(value)


def test_release_rollback_preserves_configuration_permissions(tmp_path, monkeypatch):
    import runpy
    import stat
    from pathlib import Path

    release = runpy.run_path(str(Path(__file__).resolve().parents[2] / "deploy/update.py"))
    backup = tmp_path / "backup-env"
    backup.write_text("before")
    backup.chmod(0o600)
    target = tmp_path / ".env"
    target.write_text("after")
    target.chmod(0o640)
    function = release["restore_environment"]
    monkeypatch.setitem(function.__globals__, "APP", tmp_path)
    function(backup)
    assert target.read_text() == "before"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_release_retires_shared_speech_environment(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path

    release = runpy.run_path(str(Path(__file__).resolve().parents[2] / "deploy/update.py"))
    path = tmp_path / ".env"
    before = "PUBLIC_URL=https://example.com\nBAILIAN_ASR_MODEL=voice-model\nBAILIAN_VISION_MODEL=retired\nBAILIAN_VIDEO_MODEL=retired\nFFPROBE_BINARY=retired\nTENCENT_ASR_APP_ID=123\nTENCENT_ASR_SECRET_ID=test-id\nTENCENT_ASR_SECRET_KEY=test-key\nOTHER_SETTING=keep\n"
    path.write_text(before)
    path.chmod(0o640)
    function = release["update_env"]
    monkeypatch.setitem(function.__globals__, "APP", tmp_path)
    function("postgresql://isolated/test", "new-revision")
    result = path.read_text()
    assert "OTHER_SETTING=keep" in result
    assert not any(
        key in result
        for key in (
            "BAILIAN_ASR_MODEL",
            "BAILIAN_VISION_MODEL",
            "BAILIAN_VIDEO_MODEL",
            "FFPROBE_BINARY",
            "TENCENT_ASR_APP_ID",
            "TENCENT_ASR_SECRET_ID",
            "TENCENT_ASR_SECRET_KEY",
        )
    )
    assert "APP_REVISION=new-revision" in result and path.stat().st_mode & 0o777 == 0o640


def test_release_waits_for_worker_lease_recovery(monkeypatch):
    import runpy
    from pathlib import Path

    release = runpy.run_path(str(Path(__file__).resolve().parents[2] / "deploy/update.py"))
    function = release["wait_runtimes"]
    versions = iter(("old", "new"))
    monkeypatch.setitem(
        function.__globals__,
        "run_python",
        lambda *args: json.dumps([["web", "new", 42], ["worker", next(versions), 42]]),
    )
    monkeypatch.setitem(function.__globals__, "command", lambda *args: b"42")
    monkeypatch.setattr(function.__globals__["time"], "sleep", lambda *_: None)
    function("isolated", "new")


@pytest.mark.parametrize("revision,pid,passed", [("new", 42, True), ("old", 42, False), ("new", 41, False)])
def test_release_requires_actual_worker_revision_and_pid(monkeypatch, revision, pid, passed):
    import runpy
    from pathlib import Path

    release = runpy.run_path(str(Path(__file__).resolve().parents[2] / "deploy/update.py"))
    function = release["wait_runtimes"]
    monkeypatch.setitem(
        function.__globals__,
        "run_python",
        lambda *args: json.dumps([["web", "new", 42], ["worker", revision, pid]]),
    )
    monkeypatch.setitem(function.__globals__, "command", lambda *args: b"42")
    if passed:
        function("isolated", "new", timeout=0)
    else:
        with pytest.raises(RuntimeError, match="运行版本"):
            function("isolated", "new", timeout=0)


def test_explicit_migration_is_idempotent_and_restart_does_not_delete(tmp_path):
    store = Store(database_url(tmp_path / "pg"))
    store.set_setting("preserved", {"value": "永久保留"})
    migrate_database(store.url)
    reopened = Store(store.url)
    assert reopened.setting("preserved") == {"value": "永久保留"}
    assert reopened.health() == {
        "database_backend": "postgresql",
        "database_ready": True,
        "schema_version": 59,
    }


def test_postgres_custom_backup_restores_readable_data(tmp_path):
    store = Store(database_url(tmp_path / "pg"))
    store.set_setting("restore_marker", {"text": "恢复核验", "number": 42})
    dump = tmp_path / "database.dump"
    backup_database(store, dump)
    assert dump.read_bytes().startswith(b"PGDMP")
    base = os.environ["TEST_DATABASE_URL"]
    name = "wechat_manager_restore_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute('CREATE DATABASE "' + name + '"')
    target = urlunsplit(urlsplit(store.url)._replace(path="/" + name))
    recovered = None
    try:
        restore_database(target, dump)
        recovered = Store(database_url(target))
        assert recovered.setting("restore_marker") == {"text": "恢复核验", "number": 42}
    finally:
        if recovered:
            recovered.close()
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute('DROP DATABASE "' + name + '"')
