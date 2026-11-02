import json

import httpx
import pytest
from qingda.accounts import AccountService
from qingda.providers import Gateway, ProviderError
from qingda.security import Vault, create_key
from test_automation import setup


async def test_wechat_https_path_and_deepseek_nonthinking_contract(tmp_path):
    s, m, _, _ = setup(tmp_path)
    create_key(tmp_path / "key")
    m.vault = Vault(tmp_path / "key")
    s.set_setting("wechat_token", m.vault.encrypt("isolated-token"))
    s.query("UPDATE users SET wechat_token=%s WHERE id=1 RETURNING id", (m.vault.encrypt("isolated-token"),))
    s.set_setting("model_key", m.vault.encrypt("isolated-model-key"))
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "wechatapi.net":
            assert str(request.url) == "https://wechatapi.net/finder/v2/api/login/checkOnline"
            assert request.headers["wechat-token"] == "isolated-token"
            return httpx.Response(200, json={"ret": 200, "data": True})
        body = json.loads(request.content)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert body["thinking"] == {"type": "disabled"} and body["stream"] is False
        assert body["temperature"] == 0.2
        assert "tools" not in body and body["model"] == "deepseek-flash"
        assert json.loads(body["messages"][-1]["content"]) == {
            "conversation_kind": "private",
            "reply_target": None,
            "participant_profiles": {},
            "profile_schema_version": None,
            "conversation_history": [],
            "history_coverage": {},
            "group_background": [],
            "current_messages": [{"content": "测试"}],
            "pending_messages": [],
            "interaction_signals": [],
            "presence_suppressed": False,
        }
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "通过"}}]}
        )

    g = Gateway(m, transport=httpx.MockTransport(handler))
    assert await g.online("device-a") is True
    response = await g.model(s.setting("model"), "模拟提示词", [{"role": "user", "content": "测试"}])
    assert response["choices"][0]["finish_reason"] == "stop"
    assert len(requests) == 2
    await g.close()


async def test_user_token_is_bound_and_legacy_or_injected_token_is_never_a_fallback(tmp_path):
    s, m, _, _ = setup(tmp_path)
    create_key(tmp_path / "key")
    m.vault = Vault(tmp_path / "key")
    s.set_setting("wechat_token", m.vault.encrypt("obsolete-database-token"))
    s.query(
        "UPDATE users SET wechat_token=%s WHERE id=1 RETURNING id", (m.vault.encrypt("isolated-user-token"),)
    )
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["wechat-token"] == "isolated-user-token"
        return httpx.Response(200, json={"ret": 200, "data": True})

    gateway = Gateway(m, httpx.MockTransport(handler))
    assert await gateway.online("account-a") is True
    assert await gateway.online("account-b") is True
    await gateway.close()
    s.query("UPDATE users SET wechat_token='' WHERE id=1 RETURNING id")
    missing = Gateway(m, httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="missing_wechat_token"):
        await missing.online("account-a")
    assert len(requests) == 2
    await missing.close()


async def test_remaining_wechat_contracts_refresh_profile_and_send_text(tmp_path):
    store, manager, account, _ = setup(tmp_path)
    create_key(tmp_path / "key")
    manager.vault = Vault(tmp_path / "key")
    store.query(
        "UPDATE users SET wechat_token=%s WHERE id=1 RETURNING id",
        (manager.vault.encrypt("isolated-shared-token"),),
    )
    requests = []
    expected = {
        "/login/checkOnline": {"appId": "device-a"},
        "/personal/getProfile": {"appId": "device-a"},
        "/message/postText": {"appId": "device-a", "toWxid": "friend", "content": "文本正文", "ats": ""},
    }

    def handler(request):
        path = request.url.path.removeprefix("/finder/v2/api")
        assert request.method == "POST"
        assert request.url.host == "wechatapi.net"
        assert request.headers["wechat-token"] == "isolated-shared-token"
        assert json.loads(request.content) == expected[path]
        requests.append(path)
        data = (
            {"wxid": "wxid_me", "nickName": "刷新后", "sex": 2, "mobile": "private"}
            if path == "/personal/getProfile"
            else True
        )
        return httpx.Response(200, json={"ret": 200, "data": data})

    gateway = Gateway(manager, httpx.MockTransport(handler))
    try:
        refreshed = await AccountService(manager, gateway).online(account["id"])
        assert refreshed["online"] == 1 and refreshed["nickname"] == "刷新后"
        assert refreshed["profile"]["sex"] == 2 and refreshed["profile"]["mobile"] == "private"
        assert (await gateway.send("device-a", "friend", "文本正文"))["ret"] == 200
        assert requests == list(expected)
    finally:
        await gateway.close()
