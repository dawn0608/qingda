"""天气原案与证据边界；仅在服务器隔离库执行。"""

import json

import httpx
import pytest
from test_reply_tools import Model, ReplyTools, call, config, response
from wechat_manager.reply_profiles import check_weather_location
from wechat_manager.reply_tools import ToolFailure
from wechat_manager.tool_generation import generate


def context(body="重庆今天天气怎么样", **changes):
    return [
        {
            "role": "user",
            "current": True,
            "sender": "a",
            "reply_target": "a",
            "message_id": 35,
            "content": body,
            **changes,
        }
    ]


@pytest.mark.parametrize(
    "place,quote,matched",
    [
        ("重庆市", "重庆今天天气怎么样", "重庆"),
        ("武汉", "武汉呢", "武汉"),
        ("武汉市", "武汉呢", "武汉"),
        ("重庆市江北区", "重庆市江北区天气", "重庆市江北区"),
        ("香港特别行政区", "香港天气", "香港"),
        ("内蒙古自治区", "内蒙古天气", "内蒙古"),
        ("大理自治州", "大理天气", "大理"),
        ("阿里地区", "阿里天气", "阿里"),
        ("四川省", "四川天气", "四川"),
        ("江北区", "江北天气", "江北"),
        ("奉节县", "奉节天气", "奉节"),
        ("重庆市", "重庆", "重庆"),
    ],
)
def test_requester_quote_and_single_administrative_suffix(place, quote, matched):
    proof = check_weather_location(context(quote), {"location": place, "location_evidence": quote})
    assert proof == {"quote": quote, "matched_location": matched, "message_id": 35}


@pytest.mark.parametrize(
    "change",
    [
        {"current": False},
        {"sender": "b"},
        {"role": "assistant"},
        {"speaker_type": "owner"},
        {"context_scope": "background"},
        {"message_kind": "pat"},
        {"content": "这里天气怎么样", "quoted_message": {"content": "重庆天气"}},
        {"content": "这里天气怎么样", "participant_profiles": {"a": {"city": "重庆"}}},
    ],
)
def test_sentence_evidence_never_expands_authorized_source(change):
    with pytest.raises(ToolFailure) as caught:
        check_weather_location(
            context(**change), {"location": "重庆", "location_evidence": "重庆今天天气怎么样"}
        )
    assert caught.value.details == {"stage": "validation", "request_sent": False}


@pytest.mark.parametrize(
    "place,quote,body",
    [
        ("重庆", "武汉呢", "武汉呢"),
        ("重庆", "重庆天气", "武汉天气"),
        ("重庆市江北区", "重庆今天天气怎么样", "重庆今天天气怎么样"),
        ("成都市市", "成都天气", "成都天气"),
        ("长市", "长天气", "长天气"),
        ("重庆", "重庆", "这里天气怎么样"),
    ],
)
def test_no_arbitrary_overlap_alias_or_forged_evidence(place, quote, body):
    with pytest.raises(ToolFailure):
        check_weather_location(context(body), {"location": place, "location_evidence": quote})


def provider_response(request):
    if "/geo/" in request.url.path:
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
    return httpx.Response(200, json={"temperature": {"value": 23}, "condition": {"text": "多云"}})


async def test_original_sentence_recovers_and_cache_still_requires_current_evidence():
    requests = []
    source = context()
    args = {"location": "重庆市", "location_evidence": "重庆今天天气怎么样"}

    def handler(request):
        requests.append(request)
        return provider_response(request)

    class ChangingModel(Model):
        async def chat(self, *args):
            if len(self.requests) == 1:
                source[0]["content"] = "这里天气怎么样"
            return await super().chat(*args)

    model = ChangingModel(
        [
            response(calls=[call("get_weather", args, "a")]),
            response(calls=[call("get_weather", args, "b")]),
            response("请提供城市"),
        ]
    )
    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    try:
        result = await generate(model, tools, {}, "p", source)
    finally:
        await tools.close()
    trace = result["tool_trace"]["tool_calls"]
    assert len(requests) == 2 and trace[0]["result"]["ok"]
    assert trace[0]["location_evidence"]["matched_location"] == "重庆"
    assert trace[0]["location_evidence"]["message_id"] == 35
    assert trace[1]["result"]["error"]["request_sent"] is False
    assert trace[1]["cached"] is False


@pytest.mark.parametrize(
    "status,kind,attempts,phrase",
    [
        (401, "unauthorized", 1, "密钥"),
        (403, "no-credit", 1, "额度不足"),
        (403, "invalid-host", 1, "Host"),
        (403, "security-restriction", 1, "安全限制"),
        (403, "forbidden", 1, "无权"),
        (403, "unknown-secret-title", 1, "尚未确认"),
        (429, "over-monthly-limit", 1, "本月"),
        (429, "too-many-requests", 2, "频繁"),
        (500, "unknown-error", 2, "异常"),
        (400, "no-such-location", 1, "地点"),
    ],
)
async def test_weather_problem_types_retry_and_redaction(status, kind, attempts, phrase):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={
                "error": {
                    "type": "https://dev.qweather.com/docs/resource/error-code/#" + kind,
                    "title": "private-key-and-title",
                    "detail": "weather-secret",
                }
            },
        )

    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    try:
        result = await tools.run("get_weather", {"location": "重庆", "location_evidence": "重庆"})
    finally:
        await tools.close()
    error = result["error"]
    assert len(requests) == attempts and phrase in error["message"]
    assert error["request_sent"] and error["stage"] == "location_lookup"
    assert error["http_status"] == status and error["provider"] == "qweather"
    assert "private-key-and-title" not in json.dumps(result) and "weather-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "failure,expected,stage,attempts",
    [
        ("missing_evidence", "invalid_arguments", "validation", 0),
        ("timeout", "provider_timeout", "location_lookup", 2),
        ("json", "invalid_provider_response", "location_lookup", 1),
        ("shape", "invalid_weather_response", "weather_query", 2),
    ],
)
async def test_validation_timeout_and_invalid_response_stages(failure, expected, stage, attempts):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private", request=request)
        if failure == "json":
            return httpx.Response(200, content=b"not-json")
        if failure == "shape" and "/current/" in request.url.path:
            return httpx.Response(200, json={})
        return provider_response(request)

    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    args = {"location": "重庆", **({} if failure == "missing_evidence" else {"location_evidence": "重庆"})}
    try:
        result = await tools.run("get_weather", args)
    finally:
        await tools.close()
    assert result["error"]["code"] == expected and result["error"]["stage"] == stage
    assert result["error"]["request_sent"] is bool(attempts)
    assert len(requests) == attempts


async def test_full_district_never_accepts_parent_city_as_only_result():
    requests = []

    def handler(request):
        requests.append(request)
        return provider_response(request)

    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    try:
        result = await tools.run(
            "get_weather", {"location": "重庆市江北区", "location_evidence": "重庆市江北区"}
        )
    finally:
        await tools.close()
    assert result["error"]["code"] == "location_not_found" and len(requests) == 1


async def test_successful_weather_does_not_require_source():
    requests = []

    def handler(request):
        requests.append(request)
        return provider_response(request)

    decision = {
        "action": "reply",
        "reason": "answer",
        "content": "重庆多云23度",
        "close_dialogue": False,
        "handled_message_ids": [35],
    }
    model = Model(
        [
            response(calls=[call("get_weather", {"location": "重庆", "location_evidence": "重庆"}, "w")]),
            response(calls=[call("finish_reply", decision, "f1")]),
        ]
    )
    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    try:
        result = await generate(model, tools, {}, "p", context(), decision_mode=True)
    finally:
        await tools.close()
    assert len(requests) == 2 and "和风天气" not in result["reply_decision"]["content"]
    assert len(model.requests) == 2
    assert result["tool_trace"]["tool_calls"][0]["result"]["source"] == "和风天气"
