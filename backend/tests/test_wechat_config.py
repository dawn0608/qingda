"""服务器隔离库验证Token回显、用户隔离与自动回调绑定；平台替身不证明真实绑定。"""

import pytest
from fastapi.testclient import TestClient
from test_authentication import code_login, setup, sms_login
from wechat_manager.providers import ProviderError
from wechat_manager.security import digest
from wechat_manager.tenancy import initialize_user

PATH = "/wechat/api/v1/wechat-config"


def authenticated(tmp_path):
    http, store, config, _, _ = setup(tmp_path)
    session = sms_login(http).json()
    return http, store, config, {"X-CSRF-Token": session["csrf"]}


def test_token_readback_is_authenticated_uncached_and_user_scoped(tmp_path):
    http, store, config, _ = authenticated(tmp_path)
    vault = http.app.state.manager.vault
    key = "isolated-other-user-" * 4
    with store.transaction() as c:
        c.execute("UPDATE users SET wechat_token=%s WHERE id=1", (vault.encrypt("first-user-token"),))
        c.execute(
            "INSERT INTO users(id,channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url,wechat_token) VALUES(2,'phone','13900000002','隔离用户',0,%s,%s,%s,%s)",
            (
                digest(key),
                vault.encrypt(key),
                config.public_url + "/wechatapi/callback/" + key,
                vault.encrypt("second-user-token"),
            ),
        )
        initialize_user(c, 2)
    first = http.get(PATH)
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.json()["token"] == "first-user-token"
    other = TestClient(http.app)
    assert other.get(PATH).status_code == 401
    login = code_login(other, "13900000002")
    assert login.status_code == 200
    second = other.get(PATH)
    assert second.headers["cache-control"] == "no-store"
    assert second.json()["token"] == "second-user-token"
    assert first.json()["callback_url"] != second.json()["callback_url"]
    assert "first-user-token" not in second.text and "second-user-token" not in first.text


def test_token_only_save_uses_server_callback_and_supports_editing(tmp_path, monkeypatch):
    http, store, _, headers = authenticated(tmp_path)
    expected = http.get(PATH).json()["callback_url"]
    calls = []

    async def bind(path, payload, **kwargs):
        assert path == "/login/setCallback"
        assert payload["callbackUrl"] == expected
        assert kwargs["token"] == payload["token"]
        calls.append(payload["token"])
        return {"ret": 200}

    monkeypatch.setattr(http.app.state.gateway, "call", bind)
    for revision, token in enumerate(("first-token", "edited-token")):
        result = http.put(PATH, headers=headers, json={"token": token, "revision": revision})
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json()["token"] == token and result.json()["state"] == "configured"
        stored = store.query("SELECT wechat_token FROM users WHERE id=1")[0]["wechat_token"]
        assert stored != token and http.app.state.manager.vault.decrypt(stored) == token
        assert http.get(PATH).json()["token"] == token
    stale = http.put(PATH, headers=headers, json={"token": "stale-token", "revision": 0})
    assert stale.status_code == 409 and calls == ["first-token", "edited-token"]


def test_callback_override_and_missing_csrf_cannot_bind(tmp_path, monkeypatch):
    http, _, _, headers = authenticated(tmp_path)

    async def unexpected(*args, **kwargs):
        raise AssertionError("校验失败不得调用平台")

    monkeypatch.setattr(http.app.state.gateway, "call", unexpected)
    assert http.put(PATH, json={"token": "test-token", "revision": 0}).status_code == 403
    result = http.put(
        PATH,
        headers=headers,
        json={"token": "test-token", "revision": 0, "callback_url": "http://testserver/other-user"},
    )
    assert result.status_code == 422
    assert http.get(PATH).json()["token"] == ""


@pytest.mark.parametrize("uncertain", [False, True])
def test_failed_or_uncertain_binding_preserves_active_token_and_can_retry(tmp_path, monkeypatch, uncertain):
    http, store, _, headers = authenticated(tmp_path)
    vault = http.app.state.manager.vault
    with store.transaction() as c:
        c.execute(
            "UPDATE users SET wechat_token=%s,binding_state='configured' WHERE id=1",
            (vault.encrypt("active-token"),),
        )

    async def failure(*args, **kwargs):
        if uncertain:
            raise ProviderError("isolated-timeout", uncertain=True)
        return {"ret": 500}

    monkeypatch.setattr(http.app.state.gateway, "call", failure)
    result = http.put(PATH, headers=headers, json={"token": "candidate-token", "revision": 0})
    assert result.status_code == 502
    row = store.query("SELECT * FROM users WHERE id=1")[0]
    assert vault.decrypt(row["wechat_token"]) == "active-token"
    value = http.get(PATH).json()
    assert value["token"] == ("candidate-token" if uncertain else "active-token")
    assert value["state"] == ("uncertain" if uncertain else "configured")
    if uncertain:

        async def success(path, payload, **kwargs):
            assert payload["token"] == "candidate-token"
            return {"ret": 200}

        monkeypatch.setattr(http.app.state.gateway, "call", success)
        retried = http.put(PATH, headers=headers, json={"token": "", "revision": value["revision"]})
        assert retried.status_code == 200 and retried.json()["token"] == "candidate-token"
