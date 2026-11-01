"""退役结构显式迁移、历史保留及旧入口关闭。仅服务器隔离 PostgreSQL。"""

import os
import subprocess
import sys
import time

import psycopg
import pytest
from test_automation import setup
from test_http import client
from wechat_manager.migration import migrate_database

TABLES = (
    "bridge_actions",
    "bridge_artifacts",
    "bridge_clients",
    "bridge_computer_credentials",
    "bridge_computer_pairings",
    "bridge_files",
    "bridge_items",
    "bridge_messages",
    "bridge_portal_actions",
    "bridge_portal_items",
    "bridge_portal_verifications",
    "bridge_thread_states",
    "credential_routes",
)


def legacy(store, version=57):
    with store.transaction() as conn:
        for name in TABLES:
            conn.execute(f"CREATE TABLE {name}(id text PRIMARY KEY)")
            conn.execute(f"INSERT INTO {name} VALUES('retired')")
        conn.execute(
            "CREATE FUNCTION register_credential_route() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
        )
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions VALUES(%s)", (version,))
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('bridge','{}'),('bridge_file_signing_key','\"secret\"')"
        )


@pytest.mark.parametrize("version", [51, 52, 53, 54, 55, 56, 57])
def test_upgrade_removes_only_retired_objects_and_preserves_sent_text(tmp_path, version):
    store, manager, account, conv = setup(tmp_path)
    legacy(store, version)
    with store.transaction() as conn:
        if version < 54:
            conn.execute("DROP TABLE reply_input_waits")
        task = manager.create_task(
            conn, "bridge", manager.account(account["id"], conn), conv, content="已发送正文", state="accepted"
        )
        conn.execute(
            "INSERT INTO message_records(id,account_id,conversation_id,direction,source,msg_type,sender,created_at,received_at,outbound_task_id) VALUES(999,%s,%s,'outbound','bridge','1','me',0,0,%s)",
            (account["id"], conv["id"], task["id"]),
        )
        before = dict(conn.execute("SELECT * FROM conversations WHERE id=%s", (conv["id"],)).fetchone())
    migrate_database(store.url)
    migrate_database(store.url)
    with store.connect() as conn:
        assert all(conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is None for name in TABLES)
        assert conn.execute("SELECT to_regprocedure('register_credential_route()')").fetchone()[0] is None
        assert conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0] == 58
        assert not conn.execute("SELECT 1 FROM settings WHERE key LIKE 'bridge%'").fetchone()
        assert not conn.execute("SELECT 1 FROM tasks WHERE kind='bridge'").fetchone()
        row = conn.execute("SELECT * FROM messages WHERE id=999").fetchone()
        assert row["text"] == "已发送正文" and row["outbound_task_id"] is None
        assert (
            dict(conn.execute("SELECT * FROM conversations WHERE id=%s", (conv["id"],)).fetchone()) == before
        )
        assert conn.execute("SELECT to_regclass('reply_input_waits')").fetchone()[0]


def test_unexpected_dependency_rolls_back_whole_migration(tmp_path):
    store, *_ = setup(tmp_path)
    legacy(store)
    with store.transaction() as conn:
        conn.execute("CREATE VIEW unexpected_dependency AS SELECT * FROM bridge_clients")
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        migrate_database(store.url)
    with store.connect() as conn:
        assert conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0] == 57
        assert all(conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] for name in TABLES)
        conn.execute("DROP VIEW unexpected_dependency")
    migrate_database(store.url)


def test_independent_processes_serialize_and_recover_after_crash(tmp_path):
    store, *_ = setup(tmp_path)
    legacy(store)
    ready = tmp_path / "ready"
    env = {**os.environ, "RETIREMENT_TEST_URL": store.url}
    actor = "import os;from wechat_manager.migration import migrate_database;migrate_database(os.environ['RETIREMENT_TEST_URL'])"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,time,psycopg;from pathlib import Path;c=psycopg.connect(os.environ['RETIREMENT_TEST_URL']);c.execute('SELECT pg_advisory_xact_lock(17950,2)');c.execute('CREATE TABLE uncommitted_marker(id int)');Path("
            + repr(str(ready))
            + ").write_text('ready');time.sleep(30)",
        ],
        env=env,
    )
    children = []
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        children = [subprocess.Popen([sys.executable, "-c", actor], env=env) for _ in range(2)]
        time.sleep(0.3)
        assert all(p.poll() is None for p in children)
        holder.kill()
        holder.wait(timeout=5)
        assert all(p.wait(timeout=20) == 0 for p in children)
        with store.connect() as conn:
            assert conn.execute("SELECT to_regclass('uncommitted_marker')").fetchone()[0] is None
            assert conn.execute("SELECT count(*) FROM schema_versions WHERE version=58").fetchone()[0] == 1
    finally:
        for proc in [holder, *children]:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


def test_retired_routes_are_404_and_new_users_have_no_retired_settings(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>console</html>")
    http, store = client(tmp_path, frontend_dir=dist)
    paths = (
        "/wechat/bridge/",
        "/wechat/bridge/items/old",
        "/wechat/bridge-icon.png",
        "/wechat/installer/windows-x64",
        "/wechat/api/v1/bridge/settings",
        "/wechat/api/v1/bridge/heartbeat",
        "/wechat/api/v1/bridge/files/old",
        "/wechat/api/v1/bridge/installer/windows-x64",
    )
    for path in paths:
        for method in ("GET", "POST"):
            assert http.request(method, path).status_code == 404
    assert http.get("/wechat/").status_code == 200
    assert http.get("/wechat//").status_code == 200
    assert http.get("/wechat/api/v1/accounts").status_code == 401
    assert not store.query("SELECT 1 FROM settings WHERE key LIKE 'bridge%'")
