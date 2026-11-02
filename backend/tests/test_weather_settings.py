"""服务器隔离：天气配置接口、RLS、跨进程快照和请求生命周期。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import httpx
import psycopg.sql
import pytest
from auth_fixtures import login_response
from qingda.providers import Gateway
from qingda.reply_tools import ReplyTools
from qingda.security import Vault, create_key
from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import user_scope
from qingda.weather_settings import credentials, save
from test_automation import deliver, setup
from test_database import database_url
from test_http import client


def test_weather_api_atomic_encrypted_and_blank_key_preserved(tmp_path):
    c, store = client(tmp_path)
    headers = {"X-CSRF-Token": login_response(c).json()["csrf"]}
    url = "/wechat/api/v1/settings"
    assert c.get(url).json()["weather"] == {"api_host": "", "key_configured": False, "key_preview": ""}
    assert (
        c.patch(url, json={"weather": {"api_host": "test.qweatherapi.com"}}, headers=headers).status_code
        == 400
    )
    key = "weather-private-key-for-user-1"
    body = {"weather": {"api_host": " TEST.qweatherapi.com ", "api_key": key}}
    response = c.patch(url, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["weather"]["api_host"] == "test.qweatherapi.com"
    assert response.json()["weather"]["key_configured"]
    assert key not in response.text and key not in json.dumps(store.setting("weather"))
    encrypted = store.setting("weather")["api_key"]
    assert c.app.state.manager.vault.decrypt(encrypted) == key
    revision = store.setting("model_revision", 0)
    body = {"weather": {"api_host": "next.qweatherapi.com", "api_key": ""}}
    assert c.patch(url, json=body, headers=headers).status_code == 200
    assert store.setting("weather") == {"api_host": "next.qweatherapi.com", "api_key": encrypted}
    for host in (
        "",
        "https://test.qweatherapi.com",
        "127.0.0.1",
        "test.qweatherapi.com:443",
        "test.qweatherapi.com/a",
        "test.qweatherapi.com.evil.com",
    ):
        saved = store.setting("weather")
        assert (
            c.patch(
                url, json={"weather": {"api_host": host, "api_key": "changed"}}, headers=headers
            ).status_code
            == 400
        )
        assert store.setting("weather") == saved
    assert store.setting("model_revision", 0) == revision


def test_weather_save_does_not_cancel_or_replay_tasks(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    create_key(tmp_path / "weather.key")
    manager.vault = Vault(tmp_path / "weather.key")
    deliver(store, manager, 1)
    before = store.query("SELECT id,state,reason,config_version FROM tasks")
    save(manager, "test.qweatherapi.com", "weather-key")
    save(manager, "test.qweatherapi.com", "weather-key")
    assert store.query("SELECT id,state,reason,config_version FROM tasks") == before


async def test_weather_has_no_environment_fallback(monkeypatch):
    monkeypatch.setenv("QWEATHER_API_HOST", "old.qweatherapi.com")
    monkeypatch.setenv("QWEATHER_API_KEY", "old-global-key")
    tools = ReplyTools(
        SimpleNamespace(qweather_api_host="old.qweatherapi.com", qweather_api_key="old-global-key"),
        transport=httpx.MockTransport(lambda _: pytest.fail("未配置不得联网")),
    )
    try:
        result = await tools.run("get_weather", {"location": "重庆", "location_evidence": "重庆"})
        assert result["error"] == {
            "code": "missing_weather_config",
            "message": "天气查询尚未配置",
            "stage": "configuration",
            "request_sent": False,
        }
    finally:
        await tools.close()


def restricted_store(tmp_path, role):
    admin = Store(database_url(tmp_path / "weather-rls"))
    with admin.connect() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(
            "INSERT INTO users(id,channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url) VALUES(2,'email','weather@example.com','天气用户',0,'weather-hash','weather-key','https://example.com/callback')"
        )
        conn.execute(
            psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                psycopg.sql.Identifier(schema), psycopg.sql.Identifier(role)
            )
        )
        conn.execute(
            psycopg.sql.SQL(
                "GRANT SELECT ON schema_versions TO {}; GRANT SELECT,UPDATE ON users TO {}; GRANT SELECT,INSERT,UPDATE ON settings,audit TO {}; GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}"
            ).format(
                psycopg.sql.Identifier(role),
                psycopg.sql.Identifier(role),
                psycopg.sql.Identifier(role),
                psycopg.sql.Identifier(schema),
                psycopg.sql.Identifier(role),
            )
        )
    parsed = urlsplit(admin.url)
    query = parse_qs(parsed.query)
    query["options"] = [query.get("options", [""])[0] + " -c role=" + role]
    url = urlunsplit(parsed._replace(query=urlencode({k: v[0] for k, v in query.items()}, quote_via=quote)))
    return admin, Store(url)


def test_weather_two_processes_real_rls(tmp_path, isolated_role):
    admin, runtime = restricted_store(tmp_path, isolated_role)
    key_file = tmp_path / "weather-process.key"
    create_key(key_file)
    env = {**os.environ, "WEATHER_ACTOR_DATABASE": runtime.url, "WEATHER_ACTOR_KEY_FILE": str(key_file)}
    actors = [
        subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("weather_actor.py")), str(uid)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for uid in (1, 2)
    ]
    try:
        assert all(actor.stdout.readline().strip() == "ready" for actor in actors)
        for actor in actors:
            actor.stdin.write("go\n")
            actor.stdin.flush()
        for actor in actors:
            output, error = actor.communicate(timeout=30)
            assert actor.returncode == 0, error
            assert json.loads(output)["isolated"]
        manager = Manager(runtime, Vault(key_file))
        for uid in (1, 2):
            with user_scope(uid):
                assert credentials(manager) == (f"u{uid}-34.qweatherapi.com", f"key-user-{uid}-34")
                assert len(runtime.query("SELECT * FROM settings WHERE key='weather'")) == 1
        with user_scope(None):
            assert credentials(manager) == ("", "")
    finally:
        for actor in actors:
            if actor.poll() is None:
                actor.kill()
                actor.wait()
        runtime.close()
        admin.close()


async def test_weather_request_uses_one_snapshot_then_reads_updated_credentials(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    create_key(tmp_path / "weather.key")
    manager.vault = Vault(tmp_path / "weather.key")
    save(manager, "old.qweatherapi.com", "old-user-key")
    requests = []

    def handler(request):
        requests.append((request.url.host, request.headers["X-QW-Api-Key"]))
        if request.url.path.startswith("/geo/"):
            save(manager, "new.qweatherapi.com", "new-user-key")
            return httpx.Response(
                200,
                json={
                    "location": [
                        {
                            "name": "重庆",
                            "adm1": "重庆",
                            "adm2": "重庆",
                            "lat": "29.56",
                            "lon": "106.55",
                            "tz": "Asia/Shanghai",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"temperature": {"value": 20}, "condition": {"text": "晴"}})

    gateway = Gateway(manager, httpx.MockTransport(handler), tool_config=SimpleNamespace())
    try:
        for _ in range(2):
            result = await gateway.reply_tools.run(
                "get_weather", {"location": "重庆", "location_evidence": "重庆"}
            )
            assert result["ok"], result
        assert (
            requests
            == [("old.qweatherapi.com", "old-user-key")] * 2 + [("new.qweatherapi.com", "new-user-key")] * 2
        )
    finally:
        await gateway.close()
