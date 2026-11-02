"""一次性迁移缓冲的真实磁盘与数据库事务验收。"""

import base64
import importlib
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


def module():
    assert (Path(__file__).with_name("identity_spool.py")).is_file(), "缺少独立持久缓冲"
    return importlib.import_module("identity_spool")


def test_durable_spool_preserves_bytes_and_rejects_capacity(tmp_path):
    spool = module()
    directory = tmp_path / "spool"
    directory.mkdir()
    raw = b'{"text":"hello"}'
    entry = spool.append(directory, 42, raw, now=123.5, min_free=0)
    record = next(spool.records(directory))
    assert record["id"] == entry
    assert record["user_id"] == 42 and record["received_at"] == 123.5
    assert base64.b64decode(record["raw"]) == raw
    assert (directory / (entry + ".json")).stat().st_mode & 0o777 == 0o600
    with pytest.raises(spool.CapacityError):
        spool.append(directory, 42, b"next", limit=1, min_free=0)
    assert len(list(spool.records(directory))) == 1


def test_spool_refuses_corrupt_or_symlink_records(tmp_path):
    spool = module()
    entry = spool.append(tmp_path, 1, b"original", min_free=0)
    path = tmp_path / (entry + ".json")
    path.write_text(path.read_text().replace("b3JpZ2luYWw=", "Y2hhbmdlZA=="))
    with pytest.raises(ValueError, match="checksum"):
        list(spool.records(tmp_path))
    path.unlink()
    path.symlink_to(tmp_path / "other")
    with pytest.raises(ValueError, match="unsafe"):
        list(spool.records(tmp_path))


def test_spool_does_not_report_success_when_fsync_fails(tmp_path, monkeypatch):
    spool = module()
    monkeypatch.setattr(spool.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError, match="disk failure"):
        spool.append(tmp_path, 1, b"not accepted", min_free=0)
    assert not list(tmp_path.glob("*.json"))


def test_committed_receipt_prevents_replay_after_progress_write_crash(tmp_path):
    spool = module()
    url = os.environ["TEST_DATABASE_URL"]
    schema = "spool_test_" + uuid.uuid4().hex
    batch = uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.effects(id bigint GENERATED ALWAYS AS IDENTITY, body bytea)")
        spool.prepare_receipts(conn, schema)
        try:
            spool.append(tmp_path, 1, b"without message id", min_free=0)

            def receive(connection, record):
                connection.execute(
                    f"INSERT INTO {schema}.effects(body) VALUES(%s)", (base64.b64decode(record["raw"]),)
                )

            def crash(_):
                raise RuntimeError("after commit")

            with pytest.raises(RuntimeError, match="after commit"):
                spool.replay(tmp_path, conn, batch, receive, schema=schema, progress=crash)
            assert spool.replay(tmp_path, conn, batch, receive, schema=schema) == 0
            assert conn.execute(f"SELECT count(*) FROM {schema}.effects").fetchone()[0] == 1
        finally:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_failed_receive_rolls_back_receipt_and_can_retry(tmp_path):
    spool = module()
    schema = "spool_test_" + uuid.uuid4().hex
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        spool.prepare_receipts(conn, schema)
        try:
            spool.append(tmp_path, 1, b"event", min_free=0)
            batch = uuid.uuid4().hex

            def fail(*_):
                raise ValueError("business failed")

            with pytest.raises(ValueError, match="business failed"):
                spool.replay(tmp_path, conn, batch, fail, schema=schema)
            assert spool.replay(tmp_path, conn, batch, lambda *_: None, schema=schema) == 1
        finally:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def test_two_processes_cannot_overrun_shared_capacity(tmp_path):
    import subprocess

    implementation = module()
    code = (
        "from pathlib import Path;from identity_spool import append,CapacityError;import sys;"
        "\ntry: append(Path(sys.argv[1]),1,b'event',limit=350,min_free=0)"
        "\nexcept CapacityError: sys.exit(3)"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(implementation.__file__).parent)}
    children = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], env=env) for _ in range(2)]
    assert sorted(child.wait(timeout=10) for child in children) == [0, 3]
    assert len(list(implementation.records(tmp_path))) == 1


def test_http_buffer_checks_identity_and_finishes_inflight_on_stop(tmp_path):
    import hashlib
    import http.client
    import json
    import socket
    import subprocess
    import time

    implementation = module()
    spool = tmp_path / "data"
    spool.mkdir()
    allowlist = tmp_path / "allowlist.json"
    key = "a" * 48
    allowlist.write_text(json.dumps({hashlib.sha256(key.encode()).hexdigest(): 7}))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    child = subprocess.Popen(
        [sys.executable, implementation.__file__, str(spool), str(allowlist), "--port", str(port)]
    )
    try:
        end = time.monotonic() + 10
        while True:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", "/wechatapi/callback/" + key)
                response = conn.getresponse()
                assert response.status == 200
                response.read()
                conn.close()
                break
            except (OSError, http.client.HTTPException):
                if time.monotonic() > end:
                    raise
                time.sleep(0.05)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("POST", "/wechatapi/callback/wrong", b"event")
        assert conn.getresponse().status == 404
        conn.close()
        raw = b'{"unfinished":"done"}'
        stream = socket.create_connection(("127.0.0.1", port), timeout=5)
        stream.sendall(
            (
                "POST /wechatapi/callback/"
                + key
                + " HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
                + str(len(raw))
                + "\r\n\r\n"
            ).encode()
            + raw[:3]
        )
        # 已接入的请求必须在关闭监听后仍完成，不依赖一次空目录判断结束。
        time.sleep(0.15)
        child.terminate()
        stream.sendall(raw[3:])
        response = stream.recv(4096)
        assert b" 200 " in response
        stream.close()
        assert child.wait(timeout=10) == 0
        record = next(implementation.records(spool))
        assert record["user_id"] == 7 and base64.b64decode(record["raw"]) == raw
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=20)


def test_receipt_acl_repairs_partial_setup_and_rolls_back_failed_grant():
    import identity_cutover
    from psycopg import sql

    schema = "acl_test_" + uuid.uuid4().hex
    with identity_cutover.peer("qingda_identity_test", port=15432) as admin:
        try:
            module().prepare_receipts(admin, schema)
            assert hasattr(identity_cutover, "ensure_receipt_access"), "收据权限必须事务化且可重复修复"
            identity_cutover.ensure_receipt_access(admin, "qingda_test", schema)
            identity_cutover.ensure_receipt_access(admin, "qingda_test", schema)
            with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as user:
                user.execute(
                    sql.SQL(
                        "INSERT INTO {}.callback_imports(batch,record_id,sha256) VALUES(%s,%s,%s)"
                    ).format(sql.Identifier(schema)),
                    ("b", "r", "h"),
                )
                assert (
                    user.execute(
                        sql.SQL("SELECT count(*) FROM {}.callback_imports").format(sql.Identifier(schema))
                    ).fetchone()[0]
                    == 1
                )
        finally:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        broken = "acl_test_" + uuid.uuid4().hex
        with pytest.raises(psycopg.errors.UndefinedObject):
            identity_cutover.ensure_receipt_access(admin, "absent_role_" + uuid.uuid4().hex, broken)
        assert not admin.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (broken,)).fetchone()


def test_replay_waits_for_independent_business_receive_lock(tmp_path):
    import subprocess
    import time

    from psycopg import sql

    implementation = module()
    schema = "lock_test_" + uuid.uuid4().hex
    url = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        implementation.prepare_receipts(conn, schema)
        try:
            implementation.append(tmp_path, 1, b"callback", min_free=0)
            actor = """import os,psycopg
from pathlib import Path
from identity_spool import replay
with psycopg.connect(os.environ['TEST_DATABASE_URL'],autocommit=True) as conn:
 def receive(c,r): Path(os.environ['EFFECT_PATH']).write_text('accepted')
 replay(Path(os.environ['SPOOL_PATH']),conn,'batch',receive,schema=os.environ['RECEIPT_SCHEMA'])
"""
            env = {
                **os.environ,
                "PYTHONPATH": str(Path(implementation.__file__).parent),
                "EFFECT_PATH": str(tmp_path / "effect"),
                "SPOOL_PATH": str(tmp_path),
                "RECEIPT_SCHEMA": schema,
            }
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(17950,1)")
                child = subprocess.Popen([sys.executable, "-c", actor], env=env)
                try:
                    end = time.monotonic() + 2
                    while child.poll() is None and time.monotonic() < end:
                        time.sleep(0.02)
                    assert not (tmp_path / "effect").exists(), "回放绕过实时接收锁"
                finally:
                    pass
            assert child.wait(timeout=10) == 0
            assert (tmp_path / "effect").read_text() == "accepted"
        finally:
            if "child" in locals() and child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
