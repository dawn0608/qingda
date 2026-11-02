import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from qingda.reply_tools import ReplyTools as BaseReplyTools
from qingda.reply_tools import ToolFailure, calculate
from qingda.tool_generation import Checkpoint, generate
from qingda.worker import Worker
from test_automation import NOW, deliver, setup
from test_worker import Gateway as SendingGateway


def config():
    return SimpleNamespace(
        qweather_api_host="test.re.qweatherapi.com",
        qweather_api_key="weather-secret",
    )


class NoWaitLimiter:
    async def wait(self, resource):
        pass

    def close(self):
        pass


async def fake_key():
    return "search-secret"


async def fake_weather():
    return "test.re.qweatherapi.com", "weather-secret"


async def public_resolver(url):
    pass


def ReplyTools(*args, **kwargs):
    kwargs.setdefault("key_provider", fake_key)
    kwargs.setdefault("weather_provider", fake_weather)
    kwargs.setdefault("limiter", NoWaitLimiter())
    kwargs.setdefault("resolver", public_resolver)
    return BaseReplyTools(*args, **kwargs)


def response(content="完成", calls=None, finish=None):
    return {
        "choices": [
            {
                "finish_reason": finish or ("tool_calls" if calls else "stop"),
                "message": {"content": content, **({"tool_calls": calls} if calls else {})},
            }
        ],
        "usage": {"total_tokens": 10, "completion_tokens": 3},
    }


def call(name="calculate", arguments=None, identity="c1"):
    return {
        "id": identity,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {"expression": "0.1+0.2"})},
    }


class Model:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    async def model_input(self, context):
        return context

    async def chat(self, config, messages, tools):
        self.requests.append(messages.copy())
        result = self.replies.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("1250×7.5%+19.9", "113.65"),
        ("0.1+0.2", "0.3"),
        ("(2+3)%^2", "0.0025"),
        ("2**10", "1024"),
        ("1e-2+1", "1.01"),
        ("(5-2)*(-4)/2", "-6"),
    ],
)
def test_decimal_and_percentage(expression, expected):
    assert calculate(expression)["result"] == expected


@pytest.mark.parametrize(
    "expression",
    ["1/0", "__import__('os')", "a+1", "[1][0]", "2**1000", "2**0.5", "1e9999", "9" * 513, "True+1", "1<<8"],
)
def test_unsafe_or_invalid_expression(expression):
    with pytest.raises(ToolFailure):
        calculate(expression)


async def test_time_and_parameter_validation():
    tools = ReplyTools(config(), clock=lambda: datetime(2026, 10, 14, 2, 3, 4, tzinfo=timezone.utc))
    try:
        assert (await tools.run("get_current_time", {}))["data"]["datetime"] == "2026-10-14T10:03:04+08:00"
        assert (await tools.run("get_current_time", {"timezone": "America/New_York"}))["data"][
            "datetime"
        ] == "2026-10-13T22:03:04-04:00"
        for name, arguments in [
            ("exec", {}),
            ("calculate", {}),
            ("calculate", {"expression": "1", "url": "evil"}),
            ("get_weather", {"location": "重庆", "location_evidence": "重庆", "days": True}),
            ("get_weather", {"location": "重庆", "location_evidence": "重庆", "days": 11}),
            ("get_current_time", {"timezone": "invalid"}),
        ]:
            assert not (await tools.run(name, arguments))["ok"]
    finally:
        await tools.close()


async def test_weather_geo_ambiguity_and_forecast_without_geo_persistence():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["X-QW-Api-Key"] == "weather-secret"
        if "/geo/" in request.url.path:
            if request.url.params["location"] == "朝阳":
                return httpx.Response(
                    200,
                    json={"location": [{"name": "朝阳", "adm1": "北京"}, {"name": "朝阳", "adm1": "辽宁"}]},
                )
            return httpx.Response(
                200,
                json={
                    "location": [
                        {
                            "name": "重庆",
                            "adm1": "重庆市",
                            "adm2": "重庆",
                            "lat": "29.56",
                            "lon": "106.55",
                            "tz": "Asia/Shanghai",
                        }
                    ]
                },
            )
        if "/current/" in request.url.path:
            assert request.url.path.endswith("/29.56/106.55")
            return httpx.Response(200, json={"temperature": {"value": 23}, "condition": {"text": "多云"}})
        assert request.url.params["days"] == "2"
        return httpx.Response(
            200,
            json={
                "days": [
                    {
                        "forecastStartTime": "2026-10-15T00:00:00+08:00",
                        "temperatureMax": {"value": 25},
                        "temperatureMin": {"value": 18},
                    }
                ]
            },
        )

    tools = ReplyTools(
        config(),
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 10, 14, tzinfo=timezone.utc),
    )
    try:
        ambiguity = await tools.run("get_weather", {"location": "朝阳", "location_evidence": "朝阳"})
        assert ambiguity["error"]["code"] == "ambiguous_location" and len(requests) == 1
        current = await tools.run("get_weather", {"location": "重庆", "location_evidence": "重庆"})
        assert current["data"]["temperature"]["value"] == 23
        daily = await tools.run(
            "get_weather", {"location": "重庆", "location_evidence": "重庆", "day_offset": 1}
        )
        assert daily["data"]["days"][0]["date"] == "2026-10-15"
        assert "29.56" not in json.dumps([current, daily]) and '"lat"' not in json.dumps([current, daily])
    finally:
        await tools.close()


@pytest.mark.parametrize("status,attempts", [(401, 1), (402, 1), (429, 2), (500, 2)])
async def test_http_failure_retry_rules(status, attempts):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={})

    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    try:
        result = await tools.run("web_search", {"query": "今天新闻"})
        assert not result["ok"] and len(seen) == attempts
        assert "search-secret" not in json.dumps(result)
    finally:
        await tools.close()


async def test_search_sources_and_fixed_request():
    def handler(request):
        assert json.loads(request.content) == {
            "query": "新闻 after:2026-10-14",
            "need_content": False,
            "count": 5,
        }
        assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
        assert request.headers["Authorization"] == "Bearer search-secret"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "新闻标题",
                        "url": "https://example.com/news",
                        "summary": "摘要",
                        "site_name": "来源",
                        "date": "2026-10-14",
                        "authority": 3,
                        "rerank_score": 2.4,
                    }
                ]
            },
        )

    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    try:
        result = await tools.run("web_search", {"query": "新闻 after:2026-10-14"})
        row = result["data"]["results"][0]
        assert row["published_at"] == "2026-10-14" and row["authority"] == 3
        assert result["retrieved_at"] and result["source"] == "DeepSeek Search"
        assert result["request_usage"]["billable_results"] == 1
    finally:
        await tools.close()


async def test_http_total_deadline_cancels_slow_connection(monkeypatch):
    monkeypatch.setattr("qingda.reply_tools.HTTP_TIMEOUT", 0.05)
    canceled = []

    async def handler(request):
        try:
            await asyncio.sleep(10)
        finally:
            canceled.append(True)

    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    try:
        result = await tools.run("web_search", {"query": "新闻"})
        assert result["error"]["code"] == "provider_timeout" and len(canceled) == 2
    finally:
        await tools.close()


async def test_native_multiple_rounds_unknown_and_bad_parameters():
    tools = ReplyTools(config())
    model = Model(
        [
            response(calls=[call("unknown"), call("calculate", {"extra": 1}, "c2")]),
            response(calls=[call(identity="c3")]),
            response("0.3"),
        ]
    )
    try:
        result = await generate(model, tools, {}, "prompt", "context")
        assert result["choices"][0]["message"]["content"] == "0.3"
        assert result["usage"]["total_tokens"] == 30
        trace = result["tool_trace"]["tool_calls"]
        assert [t["result"].get("error", {}).get("code") for t in trace[:2]] == [
            "unknown_tool",
            "invalid_arguments",
        ]
        assert trace[2]["result"]["data"]["result"] == "0.3"
        assert model.requests[1][-2]["tool_call_id"] == "c1"
    finally:
        await tools.close()


async def test_checkpoint_resumes_after_completed_query_and_keeps_initial_media_out(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    deliver(store, manager, 1)
    checkpoint = Checkpoint(
        store,
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["id"],
        "fingerprint",
    )
    tools = ReplyTools(config())
    original = tools.run
    seen = []

    async def counting(name, arguments):
        seen.append(name)
        return await original(name, arguments)

    tools.run = counting
    try:
        with pytest.raises(asyncio.CancelledError):
            await generate(
                Model([response(calls=[call()]), asyncio.CancelledError()]),
                tools,
                {},
                "secret-initial-prompt",
                "initial-media-payload",
                checkpoint,
            )
        loaded = checkpoint.load()
        assert loaded[1]["total_tokens"] == 10
        assert "initial-media-payload" not in json.dumps(loaded)
        restarted = Model([response("0.3")])
        result = await generate(
            restarted, tools, {}, "secret-initial-prompt", "initial-media-payload", checkpoint
        )
        assert seen == ["calculate"] and result["usage"]["total_tokens"] == 20
        assert restarted.requests[0][-1]["role"] == "tool"
        assert (
            Checkpoint(
                store,
                store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["id"],
                "changed",
            ).load()[0]
            == []
        )
    finally:
        await tools.close()


async def test_clock_is_refreshed_after_restart(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    checkpoint = Checkpoint(
        store,
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["id"],
        "same",
    )
    tools = ReplyTools(config())
    original, seen = tools.run, []

    async def counting(name, arguments):
        seen.append(name)
        return await original(name, arguments)

    tools.run = counting
    try:
        with pytest.raises(asyncio.CancelledError):
            await generate(
                Model(
                    [
                        response(calls=[call("get_current_time", {"timezone": "Asia/Shanghai"})]),
                        asyncio.CancelledError(),
                    ]
                ),
                tools,
                {},
                "p",
                [],
                checkpoint,
            )
        tools.clock = lambda: datetime(2026, 10, 15, tzinfo=timezone.utc)
        restarted = Model([response()])
        await generate(
            restarted,
            tools,
            {},
            "p",
            [],
            checkpoint,
        )
        assert seen == ["get_current_time", "get_current_time"]
        assert json.loads(restarted.requests[0][-1]["content"])["data"]["date"] == "2026-10-15"
    finally:
        await tools.close()


@pytest.mark.parametrize("mode", ["takeover", "cancel", "timeout"])
async def test_worker_cancels_inflight_query_and_never_sends(tmp_path, mode, monkeypatch):
    store, manager, account, conv = setup(tmp_path)
    deliver(store, manager, 1)

    class Gateway(SendingGateway, Model):
        def __init__(self):
            SendingGateway.__init__(self)
            Model.__init__(
                self,
                [
                    response(
                        calls=[
                            call("get_current_time", {"timezone": "Asia/Shanghai"}, "clock"),
                            call("web_search", {"query": "新闻"}),
                        ]
                    )
                ],
            )
            self.reply_tools = ReplyTools(config())

        async def model_with_tools(self, config, prompt, context, **runtime):
            return await generate(self, self.reply_tools, config, prompt, context, **runtime)

    gateway = Gateway()
    started, stopped = asyncio.Event(), asyncio.Event()
    original_run = gateway.reply_tools.run

    async def slow(name, args):
        if name == "get_current_time":
            return await original_run(name, args)
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()

    gateway.reply_tools.run = slow
    if mode == "timeout":
        monkeypatch.setattr("qingda.model_settings.TEXT_TIMEOUT", 2.0)
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    future = asyncio.create_task(worker.once())
    await asyncio.wait_for(started.wait(), 5)
    if mode == "takeover":
        deliver(store, manager, 2, sender="wxid_me", ToUserName={"string": "friend"})
    elif mode == "cancel":
        with store.connect() as conn:
            conn.execute("UPDATE tasks SET state='canceled' WHERE kind='auto'")
    await asyncio.wait_for(future, 5)
    assert stopped.is_set() and not gateway.sends
    assert store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[-1][
        "state"
    ] in (
        "failed",
        "canceled",
    )
    worker.release()
    await gateway.reply_tools.close()


async def test_worker_recovery_final_only_and_no_duplicate_send(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)

    class Gateway(SendingGateway, Model):
        def __init__(self):
            SendingGateway.__init__(self)
            Model.__init__(
                self,
                [
                    response(calls=[call()]),
                    response(
                        calls=[
                            call(
                                "finish_reply",
                                {
                                    "action": "reply",
                                    "content": "0.3",
                                    "reason": "answer",
                                    "close_dialogue": False,
                                    "handled_message_ids": [1],
                                },
                                "final",
                            )
                        ]
                    ),
                ],
            )
            self.reply_tools = ReplyTools(config())

        async def model_with_tools(self, config, prompt, context, **runtime):
            return await generate(self, self.reply_tools, config, prompt, context, **runtime)

    gateway = Gateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    await worker.once()
    assert [item[2] for item in gateway.sends] == ["0.3"]
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "accepted"
    )
    worker.release()
    restarted = Worker(manager, gateway, clock=lambda: NOW + 10)
    await restarted.once()
    assert len(gateway.sends) == 1
    restarted.release()
    await gateway.reply_tools.close()
