"""服务器隔离验收：逐用户固定模型、思考协议、联网开关与统一预算。"""

import asyncio
import json

import httpx
import pytest
from test_automation import deliver, setup
from test_reply_strategy import decision, final
from test_reply_tools import Model, ReplyTools, call, config, response
from wechat_manager.model_settings import DEFAULT_MODEL, effective_model, masked_key
from wechat_manager.providers import Gateway
from wechat_manager.reply_execution import Execution, estimate
from wechat_manager.security import Vault, create_key
from wechat_manager.tenancy import current_user, user_scope
from wechat_manager.tool_generation import Checkpoint, generate


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", ""),
        ("short", "********"),
        ("123456789012345", "********"),
        ("1234567890123456", "1234567********3456"),
        ("sk-isolated-private-key-1234", "sk-isol********1234"),
    ],
)
def test_key_preview_never_exposes_full_or_short_keys(value, expected):
    assert masked_key(value) == expected
    if value:
        assert value not in expected


def vault(manager, tmp_path):
    create_key(tmp_path / "model-test.key")
    manager.vault = Vault(tmp_path / "model-test.key")


@pytest.mark.parametrize("model", ["deepseek-flash"])
@pytest.mark.parametrize("thinking", [False, True])
async def test_thinking_request_contract(tmp_path, model, thinking):
    store, manager, _, _ = setup(tmp_path)
    vault(manager, tmp_path)
    manager.configure_model({}, api_key="isolated-key")

    def handler(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert body["tool_choice"] == "required"
        assert body["temperature"] == 0.2
        assert body["stream"] is False and body["model"] == model
        return httpx.Response(200, json=response("正文"))

    gateway = Gateway(manager, httpx.MockTransport(handler))
    try:
        await gateway.chat(
            {**DEFAULT_MODEL, "model": model, "thinking_enabled": thinking},
            [{"role": "user", "content": "公开验收"}],
            [{"type": "function", "function": {"name": "finish_reply"}}],
        )
    finally:
        await gateway.close()


async def test_reasoning_survives_persistent_restart(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    task = store.query("SELECT id FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 1")[0]
    checkpoint = Checkpoint(store, task["id"], "thinking-fingerprint")
    first = response(calls=[call()])
    first["choices"][0]["message"]["reasoning_content"] = "internal-evidence"
    tools = ReplyTools(config())
    try:
        with pytest.raises(asyncio.CancelledError):
            await generate(
                Model([first, asyncio.CancelledError()]),
                tools,
                {"thinking_enabled": True},
                "prompt",
                "context",
                checkpoint,
            )
        assert checkpoint.load()[0][0]["reasoning_content"] == "internal-evidence"
        model = Model([response("最终正文")])
        result = await generate(model, tools, {"thinking_enabled": True}, "prompt", "context", checkpoint)
        assert model.requests[0][2]["reasoning_content"] == "internal-evidence"
        assert result["choices"][0]["message"]["content"] == "最终正文"
    finally:
        await tools.close()


@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_reasoning_survives_protocol_and_length_correction(finish):
    first = response("尚未形成决策", finish=finish)
    first["choices"][0]["message"]["reasoning_content"] = "keep-internal-reasoning"
    model = Model([first, final(decision())])
    result = await generate(
        model, None, {"thinking_enabled": True, "max_tokens": 16384}, "prompt", [], decision_mode=True
    )
    assert result["reply_decision"]["content"] == "已了解"
    assert model.requests[1][2]["reasoning_content"] == "keep-internal-reasoning"


async def test_legacy_web_switch_cannot_filter_or_reject_tools():
    class Capturing(Model):
        async def chat(self, cfg, messages, tools):
            assert {"web_search", "web_fetch"} <= {t["function"]["name"] for t in tools}
            return await super().chat(cfg, messages, tools)

    tools = ReplyTools(
        config(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []}))
    )
    try:
        model = Capturing([response(calls=[call("web_search", {"query": "公开关键词"})]), response("完成")])
        result = await generate(model, tools, {"web_search_enabled": False}, "prompt", "context")
        assert result["tool_trace"]["tool_calls"][0]["result"]["ok"]
    finally:
        await tools.close()


async def test_shared_gateway_web_permission_is_per_user(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    vault(manager, tmp_path)
    seen = []
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO users(id,channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url) VALUES(2,'email','model-test@example.com','用户二',0,'model-test-hash','model-test-key','https://example.com/callback')"
        )
    for uid, enabled in ((1, False), (2, True)):
        with user_scope(uid):
            manager.configure_model({}, api_key=f"key-{uid}")
            store.set_setting("model", {"web_search_enabled": enabled, "thinking_enabled": True})

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"results": []})

    gateway = Gateway(manager, httpx.MockTransport(handler), tool_config=config())
    from test_reply_tools import NoWaitLimiter

    gateway.reply_tools.limiter = NoWaitLimiter()

    async def run(uid):
        with user_scope(uid):
            return await gateway.reply_tools.run("web_search", {"query": "公开关键词"})

    try:
        result = await asyncio.gather(run(1), run(2))
        assert all(row["ok"] for row in result)
        assert set(seen) == {"Bearer key-1", "Bearer key-2"}
        assert (await run(1))["ok"] and seen[-1] == "Bearer key-1"
        assert current_user.get() == 1
    finally:
        await gateway.close()


def test_uniform_budget_and_flash_estimate():
    assert effective_model({"timeout": 5, "max_tokens": 128})["timeout"] == 120
    manager = type("Manager", (), {"store": None})()
    execution = Execution(manager, {"id": "t", "_owner": "owner"}, None, None, text_limit=300)
    assert execution.text_limit == 120
    assert estimate("deepseek-flash", {"prompt_tokens": 1000, "completion_tokens": 100}) == 0.0028
