"""仅服务器隔离库与独立Nginx实例验证；不访问真实平台或生产业务端口。"""

import asyncio
import json
import multiprocessing
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))
import domain
import native_nginx
import update
from qingda.config import Config
from qingda.profile import Wechat, bind_wechat
from qingda.providers import ProviderError
from test_authentication import setup


def test_public_filing_and_strict_origin(tmp_path):
    http, store, config, sms, clock = setup(tmp_path)
    config.icp_filing_number = "川ICP备00000000号-1"
    filing = http.get("/wechat/api/v1/auth/options").json()["filing"]
    assert filing["icp_url"] == "https://beian.miit.gov.cn/"
    assert set(filing) == {"icp_number", "icp_url", "public_security_number", "public_security_url"}
    assert (
        http.post(
            "/wechat/api/v1/auth/login",
            headers={"Origin": "https://203.0.113.10"},
            json={"method": "code", "identifier": "13900000000", "challenge_id": "x" * 43, "code": "000000"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize("uncertain", [False, True])
def test_callback_origin_only_migrates_after_official_success(tmp_path, uncertain):
    http, store, config, sms, clock = setup(tmp_path)
    manager = http.app.state.manager
    key = manager.vault.decrypt(store.query("SELECT callback_key FROM users WHERE id=1")[0]["callback_key"])
    previous = "https://203.0.113.10/wechatapi/callback/" + key
    with store.transaction() as c:
        c.execute(
            "UPDATE users SET wechat_token=%s,callback_url=%s,binding_state='configured' WHERE id=1",
            (manager.vault.encrypt("isolated-token"), previous),
        )
    config.public_url = domain.ORIGIN
    desired = config.public_url + "/wechatapi/callback/" + key

    class Provider:
        async def call(self, *args, **kwargs):
            # 外部调用期间，其他连接仍能取得用户锁，证明短事务已释放。
            with store.transaction() as c:
                c.execute("SELECT id FROM users WHERE id=1 FOR UPDATE NOWAIT")
            if uncertain:
                raise ProviderError("isolated-timeout", uncertain=True)
            return {"ret": 200}

    from fastapi import HTTPException

    if uncertain:
        with pytest.raises(HTTPException):
            asyncio.run(bind_wechat(manager, config, Provider(), Wechat(revision=0)))
    else:
        asyncio.run(bind_wechat(manager, config, Provider(), Wechat(revision=0)))
    row = store.query("SELECT * FROM users WHERE id=1")[0]
    assert row["callback_url"] == (previous if uncertain else desired)
    assert manager.vault.decrypt(row["wechat_token"]) == "isolated-token"
    assert row["binding_state"] == ("uncertain" if uncertain else "configured")


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(port, *, tls=False, host="example.com", sni="example.com", path="/wechat/", payload=b""):
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # 仅本机隔离自签证书，真实HTTPS不使用此上下文。
        sock = context.wrap_socket(sock, server_hostname=sni)
    method = "POST" if payload else "GET"
    with sock:
        sock.sendall(
            f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nContent-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )
        answer = b""
        while part := sock.recv(65536):
            answer += part
        return answer


def test_real_isolated_nginx_host_sni_callback_and_buffer(tmp_path):
    from shutil import which

    assert which("nginx") and which("openssl"), "本案例只在服务器运行"
    http_port, tls_port = _port(), _port()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def do_POST(self):
            requests.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self.send_response(200)
            self.send_header("X-Callback-Receiver", "isolated-buffer")
            self.end_headers()

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    cert = tmp_path / "cert"
    cert.mkdir()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=example.com",
            "-addext",
            "subjectAltName=DNS:example.com",
            "-keyout",
            str(cert / "privkey.pem"),
            "-out",
            str(cert / "fullchain.pem"),
        ],
        check=True,
        capture_output=True,
    )
    try:
        for mode in ("paused", "active"):
            body = native_nginx.render_domain("example.com", mode, test=True, lineage=cert)
            if mode == "active":
                body = update.buffer_gateway(SimpleNamespace(native=True, npm=False), body)
                assert body.count(b":17952;") == 1
            body = body.replace(b"127.0.0.1:18080", f"127.0.0.1:{http_port}".encode()).replace(
                b"127.0.0.1:18443", f"127.0.0.1:{tls_port}".encode()
            )
            body = body.replace(b"/run/wechat-domain-candidate.pid", str(tmp_path / "nginx.pid").encode())
            body = body.replace(b"127.0.0.1:17952", f"127.0.0.1:{upstream.server_port}".encode()).replace(
                b"127.0.0.1:17950", f"127.0.0.1:{upstream.server_port}".encode()
            )
            conf = tmp_path / "nginx.conf"
            conf.write_bytes(body)
            subprocess.run(["nginx", "-t", "-c", str(conf)], check=True, capture_output=True)
            process = subprocess.Popen(
                ["nginx", "-c", str(conf), "-g", "daemon off;"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(60):
                    try:
                        answer = _request(http_port)
                        break
                    except OSError:
                        time.sleep(0.05)
                else:
                    raise AssertionError("隔离Nginx未就绪")
                assert not _request(http_port, host="203.0.113.10")
                assert not _request(tls_port, tls=True, host="203.0.113.10")
                rejected = _request(tls_port, tls=True, sni="unknown.invalid")
                assert not rejected or b"421" in rejected.split(b"\r\n")[0]
                if mode == "paused":
                    assert b"503" in answer.split(b"\r\n")[0]
                    assert not _request(tls_port, tls=True)
                else:
                    assert b"Location: https://example.com/wechat/" in answer
                    from urllib.parse import urlsplit

                    target = _request(tls_port, tls=True, path="/").split(b"Location: ")[1].split(b"\r\n")[0]
                    assert urlsplit(target.decode()).hostname == "example.com"
                    assert urlsplit(target.decode()).path == "/wechat/"
                    assert (
                        b"401"
                        in _request(tls_port, tls=True, path="/wechat/api/v1/accounts").split(b"\r\n")[0]
                    )
                    payload = b'{"fixture":"same-bytes"}'
                    assert b"isolated-buffer" in _request(
                        tls_port, tls=True, path="/wechatapi/callback/isolated-path", payload=payload
                    )
                    assert requests == [payload]
            finally:
                process.terminate()
                process.wait(timeout=5)
    finally:
        upstream.shutdown()
        upstream.server_close()


def _bind_actor(folder, lock, output):
    try:
        with domain.storage_lock(Path(lock)):
            state = asyncio.run(domain.bind_users(Path(folder)))
        Path(output).write_text(state["stage"])
    except Exception as error:
        Path(output).write_text(str(error))


@pytest.mark.parametrize("failure", ["complete", "cancel", "crash"])
def test_two_process_binding_replay_capacity_cancel_crash_and_expiry(tmp_path, monkeypatch, failure):
    http, store, config, sms, clock = setup(tmp_path)
    manager = http.app.state.manager
    config.public_url = domain.ORIGIN
    with store.transaction() as c:
        c.execute(
            "UPDATE users SET wechat_token=%s,binding_state='configured' WHERE id=1",
            (manager.vault.encrypt("process-fixture-token"),),
        )
    folder = tmp_path / "operation"
    folder.mkdir()
    pause = tmp_path / "pause"
    pause.write_text("{}")
    monkeypatch.setattr(domain, "PAUSE", pause)
    monkeypatch.setattr(domain, "CURRENT", tmp_path / "current.json")
    monkeypatch.setattr(domain, "folder_path", lambda p: Path(p))
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: config))
    domain.save(folder, dict(domain="example.com", stage="binding", users={}))
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    calls = tmp_path / "provider-calls"

    class Provider:
        def __init__(self, manager):
            pass

        async def call(self, *args, **kwargs):
            with calls.open("a") as stream:
                stream.write("official-side-effect\n")
                stream.flush()
                os.fsync(stream.fileno())
            entered.set()
            assert release.wait(20)
            return {"ret": 200}

        async def close(self):
            pass

    import qingda.providers

    monkeypatch.setattr(qingda.providers, "Gateway", Provider)
    lock = tmp_path / "operation.lock"
    actor = context.Process(target=_bind_actor, args=(str(folder), str(lock), str(tmp_path / "first")))
    actor.start()
    try:
        assert entered.wait(10)
        competing = context.Process(
            target=_bind_actor, args=(str(folder), str(lock), str(tmp_path / "second"))
        )
        competing.start()
        competing.join(10)
        assert competing.exitcode == 0
        assert (tmp_path / "second").read_text() == "storage_operation_in_progress"
        with store.transaction() as c:
            c.execute("SELECT id FROM users WHERE id=1 FOR UPDATE NOWAIT")
        if failure == "complete":
            release.set()
            actor.join(10)
            assert actor.exitcode == 0 and (tmp_path / "first").read_text() == "bound"
        else:
            os.kill(actor.pid, signal.SIGTERM if failure == "cancel" else signal.SIGKILL)
            actor.join(10)
            with store.transaction() as c:
                c.execute("UPDATE users SET binding_started=%s WHERE id=1", (time.time() - 120,))
        with domain.storage_lock(lock):
            pass
        replay = context.Process(target=_bind_actor, args=(str(folder), str(lock), str(tmp_path / "replay")))
        replay.start()
        replay.join(10)
        assert replay.exitcode == 0
        assert (tmp_path / "replay").read_text() == (
            "bound" if failure == "complete" else "用户绑定结果待核实，不得自动重复调用"
        )
        assert calls.read_text().splitlines() == ["official-side-effect"]
    finally:
        if actor.is_alive():
            actor.kill()
        actor.join(5)


def test_approval_and_resume_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "CURRENT", tmp_path / "current.json")
    monkeypatch.setattr(domain, "folder_path", lambda p: Path(p))
    domain.save(tmp_path, dict(domain="example.com", stage="paused", users={}))
    evidence = tmp_path / "approval.json"
    evidence.write_text(
        json.dumps(dict(domain="other.invalid", status="approved", icp_number="川ICP备00000000号"))
    )
    with pytest.raises(ValueError):
        domain.approve(tmp_path, evidence)
    with pytest.raises(ValueError):
        domain.resume(tmp_path)
    with pytest.raises(ValueError):
        domain.certificate(tmp_path)


def test_operations_readonly_sees_rows_hidden_without_user_context(tmp_path, monkeypatch):
    # 运维子进程复用本次隔离解释器，不依赖尚未部署的生产目录。
    import sys

    class IsolatedApp:
        def __truediv__(self, name):
            return Path(sys.executable) if name == ".venv/bin/python" else tmp_path / name

    monkeypatch.setattr(domain, "APP", IsolatedApp())
    http, store, config, sms, clock = setup(tmp_path)
    schema = store.query("SELECT current_schema() AS name")[0]["name"]
    with store.transaction() as c:
        c.execute("CREATE TABLE domain_ops_probe(user_id bigint)")
        c.execute("INSERT INTO domain_ops_probe VALUES(1)")
        c.execute("ALTER TABLE domain_ops_probe ENABLE ROW LEVEL SECURITY")
        c.execute("ALTER TABLE domain_ops_probe FORCE ROW LEVEL SECURITY")
        c.execute(
            "CREATE POLICY own ON domain_ops_probe USING(user_id=nullif(current_setting('app.user_id',true),'')::bigint)"
        )
    with psycopg.connect(config.database_url) as c:
        assert c.execute("SELECT count(*) FROM domain_ops_probe").fetchone()[0] == 0
    monkeypatch.setattr(domain, "dotenv_values", lambda path: {"DATABASE_URL": config.database_url})
    statement = (
        psycopg.sql.SQL("SELECT count(*) FROM {}.domain_ops_probe")
        .format(psycopg.sql.Identifier(schema))
        .as_string()
    )
    assert domain.query(statement) == [[1]]
    with pytest.raises(RuntimeError):
        domain.query(
            psycopg.sql.SQL("DELETE FROM {}.domain_ops_probe")
            .format(psycopg.sql.Identifier(schema))
            .as_string()
        )
    assert domain.query(statement) == [[1]]


def test_systemd_pause_condition_prevents_restart(tmp_path):
    marker = tmp_path / "pause"
    marker.touch()
    result = tmp_path / "started"
    for iteration in range(2):
        subprocess.run(
            [
                "systemd-run",
                "--unit=qingda-domain-test-" + str(os.getpid()) + "-" + str(iteration),
                "--collect",
                "--wait",
                "--property=ConditionPathExists=!" + str(marker),
                "/usr/bin/touch",
                str(result),
            ],
            check=True,
            capture_output=True,
        )
        assert result.exists() == bool(iteration)
        marker.unlink(missing_ok=True)
