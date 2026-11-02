"""在独立 PostgreSQL 集群中验证 OID 改名日志与中断恢复。"""

import importlib
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent))


def module():
    assert Path(__file__).with_name("identity_database.py").is_file(), "缺少 OID 迁移日志"
    return importlib.import_module("identity_database")


@pytest.fixture
def objects():
    url = os.environ["MIGRATION_TEST_ADMIN_URL"]
    assert "15432" in url, "只允许本次独立 PostgreSQL 集群"
    tag = uuid.uuid4().hex[:12]
    roles = [(f"old_role_{tag}", f"new_role_{tag}")]
    databases = [(f"old_db_{tag}", f"new_db_{tag}")]
    conn = psycopg.connect(url, autocommit=True)
    conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(roles[0][0])))
    conn.execute(
        sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(databases[0][0]), sql.Identifier(roles[0][0])
        )
    )
    try:
        yield conn, databases, roles
    finally:
        for name in databases[0]:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        for name in roles[0]:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        conn.close()


def test_rename_recovers_after_database_commit_without_checkpoint(tmp_path, objects):
    implementation = module()
    conn, dbs, roles = objects
    journal = implementation.RenameJournal(conn, tmp_path / "names.json", dbs, roles)
    journal.prepare()
    oid = conn.execute("SELECT oid FROM pg_database WHERE datname=%s", (dbs[0][0],)).fetchone()[0]
    # 模拟 SQL 已提交、进程尚未写完成标记时退出。
    conn.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(*map(sql.Identifier, dbs[0])))
    journal.forward()
    journal.forward()
    assert conn.execute("SELECT oid FROM pg_database WHERE datname=%s", (dbs[0][1],)).fetchone()[0] == oid
    journal.backward()
    journal.backward()
    assert conn.execute("SELECT oid FROM pg_database WHERE datname=%s", (dbs[0][0],)).fetchone()[0] == oid
    assert conn.execute("SELECT datallowconn FROM pg_database WHERE oid=%s", (oid,)).fetchone()[0]


def test_name_collision_does_not_change_source(tmp_path, objects):
    implementation = module()
    conn, dbs, roles = objects
    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbs[0][1])))
    journal = implementation.RenameJournal(conn, tmp_path / "names.json", dbs, roles)
    with pytest.raises(ValueError, match="collision"):
        journal.prepare()
    assert conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbs[0][0],)).fetchone()


def test_reverse_preserves_disabled_connections_and_role_identity(tmp_path, objects):
    implementation = module()
    conn, dbs, roles = objects
    conn.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(dbs[0][0])))
    oid = conn.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (roles[0][0],)).fetchone()[0]
    journal = implementation.RenameJournal(conn, tmp_path / "names.json", dbs, roles)
    journal.prepare()
    journal.forward()
    journal.restore_connections()
    assert (
        conn.execute("SELECT datallowconn FROM pg_database WHERE datname=%s", (dbs[0][1],)).fetchone()[0]
        is False
    )
    journal.backward()
    assert conn.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (roles[0][0],)).fetchone()[0] == oid
    assert (
        conn.execute("SELECT datallowconn FROM pg_database WHERE datname=%s", (dbs[0][0],)).fetchone()[0]
        is False
    )
