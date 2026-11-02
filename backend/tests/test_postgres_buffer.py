"""真实隔离 HTTP 缓冲、连接池过载和取消后的资源归还。"""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import httpx
import pytest
from psycopg_pool import PoolTimeout, TooManyRequests
from qingda.callback_buffer import replay
from qingda.database_async import database_call
from qingda.security import digest
from qingda.store import Store
from test_database import database_url
from test_inbox import event


def test_callback_buffer_commits_before_ack_and_replay_is_deduplicated(tmp_path, unused_tcp_port):
    store = Store(database_url(tmp_path / "postgres"))
    key = "b" * 48
    store.query("UPDATE users SET callback_key_hash=%s WHERE id=1 RETURNING id", (digest(key),))
    env = {
        **os.environ,
        "APP_ROOT": str(tmp_path),
        "DATABASE_URL": store.url,
        "WEB_BIND_HOST": "127.0.0.1",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "qingda.callback_buffer", "--port", str(unused_tcp_port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = "http://127.0.0.1:" + str(unused_tcp_port) + "/wechatapi/callback/" + key
    try:
        with httpx.Client(trust_env=False, timeout=3) as client:
            deadline = time.monotonic() + 10
            while True:
                try:
                    response = client.get(url)
                    assert response.headers["X-Callback-Receiver"] == "qingda-postgres-buffer"
                    break
                except httpx.RequestError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.05)
            raw = json.dumps(event(ImgBuf={"buffer": "DO_NOT_STORE_BASE64"}), ensure_ascii=False).encode()
            for _ in range(2):
                assert client.post(url, content=raw).status_code == 200
            assert client.post(url, content=b"{invalid").status_code == 200
        rows = store.query("SELECT * FROM migration_inbox ORDER BY id")
        assert len(rows) == 3
        assert "DO_NOT_STORE_BASE64" not in rows[0]["body"]
        assert rows[0]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=10)
        assert process.returncode == 0, err
        assert replay(store) == 3 and replay(store) == 0
        actual = store.events()
        assert len(actual) == 2
        valid = next(row for row in actual if row["message_id"])
        assert valid["deliveries"] == 2 and valid["raw_sha256"] == hashlib.sha256(raw).hexdigest()
        assert any(row["parse_error"] == "invalid_json" for row in actual)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)


def test_connection_pool_overload_is_bounded_and_returns_connections(tmp_path):
    store = Store(database_url(tmp_path / "postgres"))
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(store.pool.connection())

        def query():
            try:
                store.query("SELECT 1")
                return "unexpected"
            except (PoolTimeout, TooManyRequests) as error:
                return type(error).__name__

        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(lambda _: query(), range(20)))
        assert "unexpected" not in results
        assert "TooManyRequests" in results
    assert store.query("SELECT 1 AS value") == [{"value": 1}]
    assert store.pool.get_stats().get("requests_waiting", 0) == 0


async def test_cancel_during_database_call_returns_pool_capacity(tmp_path):
    import threading

    store = Store(database_url(tmp_path / "postgres"))
    started = threading.Event()

    def query():
        started.set()
        return store.query("SELECT pg_sleep(0.15)")

    task = asyncio.create_task(database_call(query))
    assert await asyncio.to_thread(started.wait, 2)
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.query("SELECT 1 AS value") == [{"value": 1}]
    assert store.pool.get_stats().get("requests_waiting", 0) == 0
