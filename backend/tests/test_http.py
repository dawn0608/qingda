import json

from auth_fixtures import login_response
from business_fixtures import allow_reply
from fastapi.testclient import TestClient
from qingda.app import create_app
from qingda.config import Config
from qingda.security import create_key
from qingda.store import Store
from test_database import database_url


def client(tmp_path, gateway_factory=None, **config_values):
    create_key(tmp_path / "master.key")
    for key in (
        "aliyun_access_key_id",
        "aliyun_access_key_secret",
        "aliyun_sms_sign_name",
        "aliyun_sms_template_code",
    ):
        config_values.setdefault(key, "fixture")
    config = Config(tmp_path, tmp_path / "master.key", cookie_secure=False, **config_values)
    store = Store(database_url(config.database_url))
    from test_authentication import Clock, Sms

    app = create_app(config) if gateway_factory is None else create_app(config, gateway_factory)
    app.state.authentication.sms = Sms()
    app.state.authentication.clock = Clock()
    return TestClient(app), store


def test_account_auto_reply_switch_and_prompt_save_are_independent(tmp_path):
    c, store = client(tmp_path)
    manager = c.app.state.manager
    manager.configure_model({}, api_key="isolated-account-switch-key")
    accounts = []
    for i in range(2):
        account = manager.add_account(f"switch-device-{i}")
        manager.verify_identity(f"switch-device-{i}", {"wxid": f"switch-self-{i}"})
        manager.configure_automation(account["id"], prompt=f"账号 {i} 已保存提示词")
        accounts.append(account)
    csrf = login_response(c).json()["csrf"]
    headers = {"X-CSRF-Token": csrf}
    paths = [f"/wechat/api/v1/accounts/{a['id']}/automation" for a in accounts]
    enabled = c.patch(paths[0], headers=headers, json={"ai_enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["prompt"] == "账号 0 已保存提示词" and enabled.json()["ai_enabled"] == 1
    assert manager.account(accounts[1]["id"])["ai_enabled"] == 0
    assert c.patch(paths[0], headers=headers, json={"prompt": "账号 0 新提示词"}).json()["ai_enabled"] == 1
    assert c.patch(paths[1], headers=headers, json={"ai_enabled": True}).is_success
    tasks = []
    for account in accounts:
        conv = manager.ensure_conversation(account["id"], "filehelper")
        allow_reply(manager, conv["id"], True)
        assert manager.conversation(conv["id"])["auto_reply_status"] == "enabled"
        with store.transaction() as conn:
            tasks.append(manager.create_task(conn, "auto", manager.account(account["id"], conn), conv))
    assert c.patch(paths[0], headers=headers, json={"ai_enabled": False}).is_success
    assert manager.account(accounts[0]["id"])["prompt"] == "账号 0 新提示词"
    assert manager.account(accounts[1]["id"])["ai_enabled"] == 1
    states = {
        t["id"]: t["state"]
        for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    }
    assert states[tasks[0]["id"]] == "canceled" and states[tasks[1]["id"]] == "queued"
    assert "master_enabled" not in c.get("/wechat/api/v1/settings").json()
    assert (
        c.patch("/wechat/api/v1/settings", headers=headers, json={"master_enabled": True}).status_code == 422
    )
    assert c.patch(paths[0], headers=headers, json={}).status_code == 400


def test_account_auto_reply_enable_requires_saved_prompt_and_model_key(tmp_path):
    c, _ = client(tmp_path)
    manager = c.app.state.manager
    account = manager.add_account("switch-validation-device")
    manager.verify_identity("switch-validation-device", {"wxid": "switch-validation-self"})
    csrf = login_response(c).json()["csrf"]
    path = f"/wechat/api/v1/accounts/{account['id']}/automation"
    headers = {"X-CSRF-Token": csrf}
    assert c.patch(path, headers=headers, json={"ai_enabled": True}).status_code == 400
    assert c.patch(path, headers=headers, json={"prompt": "已保存提示词"}).is_success
    missing_key = c.patch(path, headers=headers, json={"ai_enabled": True})
    assert missing_key.status_code == 400 and "模型 Key" in missing_key.json()["error"]
    assert manager.account(account["id"])["ai_enabled"] == 0


def test_callback_is_durable_before_ack_and_invalid_json_is_preserved(tmp_path):
    c, store = client(tmp_path)
    path = "/wechatapi/callback/" + "a" * 48
    assert c.get(path).status_code == 200
    assert c.head(path).status_code == 200
    assert c.post(path, content=b"").status_code == 200
    assert c.post(path, content=b"{broken").status_code == 200
    assert store.query("SELECT raw_sha256,parse_error,payload FROM events WHERE id=1")[0] == {
        "raw_sha256": __import__("hashlib").sha256(b"{broken").hexdigest(),
        "parse_error": "invalid_json",
        "payload": "{}",
    }
    assert c.post("/wechatapi/callback/wrong", json={}).status_code == 404
    assert c.get("/wechat/api/v1/events/1").status_code == 404


def test_session_csrf_and_secret_settings_do_not_return_secrets(tmp_path, monkeypatch):
    c, store = client(tmp_path)
    store.query(
        "UPDATE users SET wechat_token=%s WHERE id=1 RETURNING id",
        (c.app.state.manager.vault.encrypt("token-private"),),
    )

    async def catalog(*_):
        return [{"id": "deepseek-flash", "name": "DeepSeek Flash"}]

    monkeypatch.setattr(c.app.state.gateway, "validate_model_key", catalog)
    response = login_response(c)
    assert response.status_code == 200
    csrf = response.json()["csrf"]
    assert c.get("/wechat/api/v1/auth/session").json()["csrf"] == csrf
    assert c.get("/wechat/api/v1/auth/session").json()["csrf"] == csrf
    assert c.patch("/wechat/api/v1/settings", json={"wechat_token": "token-private"}).status_code == 403
    assert (
        c.patch(
            "/wechat/api/v1/settings",
            headers={"X-CSRF-Token": csrf},
            json={"model_key": "key-private"},
        ).status_code
        == 200
    )
    view = c.get("/wechat/api/v1/settings").json()
    assert view["wechat_token_configured"] is True
    assert "token-private" not in json.dumps(view)
    assert "key-private" not in json.dumps(view)
    assert store.setting("wechat_token") is None
    assert "key-private" not in store.setting("model_key")


def test_storage_failure_does_not_acknowledge_callback(tmp_path, monkeypatch):
    import psycopg

    c, _ = client(tmp_path)

    def fail(*args, **kwargs):
        raise psycopg.OperationalError("test disk failure")

    monkeypatch.setattr(Store, "receive", fail)
    assert c.post("/wechatapi/callback/" + "a" * 48, json={"TypeName": "AddMsg"}).status_code == 503


def test_key_previews_follow_authenticated_user_and_never_replace_keys(tmp_path):
    from qingda.tenancy import user_scope
    from test_authentication import code_login

    c, store = client(tmp_path)
    vault = c.app.state.manager.vault
    first = login_response(c).json()
    values = {"model_key": "sk-first-model-private-1234", "asr_key": "sk-first-voice-private-5678"}
    for name, value in values.items():
        store.set_setting(name, vault.encrypt(value))
    before = {name: store.setting(name) for name in values}
    result = c.get("/wechat/api/v1/settings")
    view = result.json()
    assert result.headers["Cache-Control"] == "no-store"
    assert view["model_key_preview"] == "sk-firs********1234"
    assert view["asr"]["key_preview"] == "sk-firs********5678"
    assert not any(value in result.text for value in values.values())
    blank = c.patch("/wechat/api/v1/settings", json={}, headers={"X-CSRF-Token": first["csrf"]})
    assert blank.status_code == 200 and blank.json()["model_key_preview"] == view["model_key_preview"]
    assert {name: store.setting(name) for name in values} == before

    second = code_login(c, target="13900000008").json()
    empty = c.get("/wechat/api/v1/settings").json()
    assert not empty["model_key_configured"] and empty["model_key_preview"] == ""
    assert not empty["asr"]["key_configured"] and empty["asr"]["key_preview"] == ""
    with user_scope(second["user_id"]):
        store.set_setting("asr_key", vault.encrypt("sk-second-voice-private-9999"))
    other = c.get("/wechat/api/v1/settings").json()
    assert other["model_key_preview"] == ""
    assert other["asr"]["key_preview"] == "sk-seco********9999"
    assert view["asr"]["key_preview"] not in json.dumps(other)
