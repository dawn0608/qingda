"""原生工具调用循环和持久检查点；不持久化初始聊天或媒体输入的副本。"""

import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .database_async import database_call
from .emoji_policy import VERSION as EMOJI_POLICY
from .model_settings import available_tools as select_tools
from .protocol import redact
from .providers import ProviderError
from .reply_decisions import TOOL as FINISH_TOOL
from .reply_decisions import validate as validate_decision
from .reply_execution import DEEPSEEK_REQUEST_LIMIT
from .reply_prompt import fixed
from .reply_tools import TOOLS, ToolFailure


def runtime_clock(tools=None):
    current = tools.clock() if tools is not None else datetime.now(timezone.utc)
    current = current.astimezone(ZoneInfo("Asia/Shanghai"))
    return {"now": current.isoformat(), "timezone": "Asia/Shanghai", "date": current.date().isoformat()}


def history_state(messages, trace, transform, tool_name=None):
    """仅历史工具结果使用引用清单；外部工具原有检查点协议保持不变。"""
    from .history_tool import NAME

    NAME = tool_name or NAME

    ids = {v["call_id"] for v in trace if v["name"] == NAME}
    saved_messages = [
        {**v, "content": json.dumps(transform(json.loads(v["content"])), ensure_ascii=False)}
        if v.get("role") == "tool" and v.get("tool_call_id") in ids
        else v
        for v in messages
    ]
    saved_trace = [{**v, "result": transform(v["result"])} if v["name"] == NAME else v for v in trace]
    return saved_messages, saved_trace


class Checkpoint:
    def __init__(self, store, task_id, fingerprint):
        self.store, self.task_id, self.fingerprint = store, task_id, fingerprint

    def load(self):
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM reply_generation_checkpoints WHERE task_id=%s AND fingerprint=%s",
                (self.task_id, self.fingerprint),
            ).fetchone()
        if not row:
            return [], {}, [], 0
        return (
            json.loads(row["messages_json"]),
            json.loads(row["usage_json"]),
            json.loads(row["trace_json"]),
            row["rounds"],
        )

    def save(self, messages, usage, trace, rounds):
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO reply_generation_checkpoints VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,task_id) DO UPDATE SET fingerprint=excluded.fingerprint,messages_json=excluded.messages_json,usage_json=excluded.usage_json,trace_json=excluded.trace_json,rounds=excluded.rounds,updated_at=excluded.updated_at",
                (
                    self.task_id,
                    self.fingerprint,
                    json.dumps(messages, ensure_ascii=False),
                    json.dumps(usage),
                    json.dumps(trace, ensure_ascii=False),
                    rounds,
                    time.time(),
                ),
            )


def fingerprint(config, prompt, context):
    from .context_budget import VERSION as CONTEXT_POLICY
    from .history_tool import TOOL as HISTORY_TOOL
    from .speech import FINGERPRINT as SPEECH_POLICY

    return hashlib.sha256(
        json.dumps(
            [
                os.getenv("APP_REVISION", "development"),
                EMOJI_POLICY,
                TOOLS,
                FINISH_TOOL,
                CONTEXT_POLICY,
                SPEECH_POLICY,
                HISTORY_TOOL,
                runtime_clock()["date"],
                config,
                prompt,
                context,
            ],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()


def add_usage(total, current):
    for key, value in current.items():
        if isinstance(value, dict):
            nested = total.setdefault(key, {})
            if isinstance(nested, dict):
                add_usage(nested, value)
        elif type(value) in (int, float):
            total[key] = total.get(key, 0) + value


async def generate(
    gateway,
    tools,
    config,
    prompt,
    context,
    checkpoint=None,
    guard=None,
    progress=None,
    decision_mode=False,
    history=None,
    execution=None,
):
    from .model_settings import effective_model

    config = {**config, **{key: effective_model()[key] for key in ("thinking_enabled", "web_search_enabled")}}
    messages, usage, trace, rounds = (await database_call(checkpoint.load)) if checkpoint else ([], {}, [], 0)
    from .context_snapshot import pack
    from .history_tool import NAME as HISTORY_NAME
    from .history_tool import TOOL as HISTORY_TOOL

    if history is not None:
        messages, trace = await database_call(history_state, messages, trace, history.hydrate)
    # 时间工具必须重新读取；已完成的天气/搜索结果仍通过本任务缓存复用。
    for index, message in enumerate(messages):
        if any(
            call.get("function", {}).get("name") == "get_current_time"
            for call in message.get("tool_calls", [])
        ):
            # 保留原生请求并撤掉其结果，服务端直接重读时钟，不依赖模型主动再问。
            messages = messages[: index + 1]
            break
    clock = runtime_clock(tools)
    initial = [
        {"role": "system", "content": prompt + "\n\nruntime_clock=" + json.dumps(clock)},
        {"role": "user", "content": await gateway.model_input(context)},
    ]
    available_tools = (
        (select_tools(config, TOOLS) if tools is not None else [])
        + ([HISTORY_TOOL] if history is not None else [])
        + ([FINISH_TOOL] if decision_mode else [])
    )
    protocol_errors = 0
    request_config = dict(config)

    def assistant(message, calls=None):
        value = {"role": "assistant", "content": message.get("content") or ""}
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ProviderError("invalid_model_output")
        if config.get("thinking_enabled", False) or reasoning is not None:
            value["reasoning_content"] = reasoning or ""
        if calls:
            value["tool_calls"] = calls
        return value

    def finished(decision):
        packed_trace = history_state([], trace, pack)[1]
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": decision["content"]}}],
            "reply_decision": decision,
            "usage": usage,
            "tool_trace": {
                "tool_calls": [item for item in packed_trace if item["name"] != "finish_reply"],
                "model_rounds": rounds,
                "style_reviewed": False,
                "reply_decision": decision,
            },
        }

    def protocol_error():
        nonlocal protocol_errors
        protocol_errors += 1
        if protocol_errors > 2:
            raise ProviderError("invalid_reply_decision")

    async def check():
        if guard:
            reason = await database_call(guard)
            if reason:
                raise ProviderError(reason)

    async def save():
        await check()
        saved_messages, saved_trace = await database_call(history_state, messages, trace, pack)
        if checkpoint:
            await database_call(checkpoint.save, saved_messages, usage, saved_trace, rounds)
        if progress:
            await database_call(progress, {"tool_calls": saved_trace, "model_rounds": rounds}, usage)

    def cache_key(name, arguments):
        return json.dumps([name, arguments], sort_keys=True, ensure_ascii=False, allow_nan=False)

    def cache_valid(name, arguments, result):
        if name == "get_weather" and "day_offset" in arguments:
            data = result["data"]
            target = tools.clock().astimezone(ZoneInfo(data["timezone"])).date() + timedelta(
                days=arguments["day_offset"]
            )
            return bool(data.get("days") and data["days"][0]["date"] == target.isoformat())
        return True

    cache = {}
    for item in trace:
        if item["name"] == "get_current_time" or not item["result"].get("ok"):
            continue
        try:
            fetched = datetime.fromisoformat(item["result"]["retrieved_at"])
            if 0 <= (datetime.now(timezone.utc) - fetched).total_seconds() <= 300:
                cache[cache_key(item["name"], item["arguments"])] = item["result"]
        except (ValueError, KeyError, TypeError):
            continue

    while True:
        await check()
        if decision_mode:
            finished_ids = {message.get("tool_call_id") for message in messages if message["role"] == "tool"}
            for item in reversed(trace):
                if (
                    item["name"] == "finish_reply"
                    and item["call_id"] in finished_ids
                    and item["result"].get("ok")
                ):
                    return finished(validate_decision(item["result"]["data"], context))
        pending = None
        for message in reversed(messages):
            if message["role"] == "assistant":
                pending = message
                break
        if not decision_mode and pending and not pending.get("tool_calls") and messages[-1] is pending:
            return {
                "choices": [{"finish_reason": "stop", "message": pending}],
                "usage": usage,
                "tool_trace": {"tool_calls": trace, "model_rounds": rounds},
            }
        completed = {message.get("tool_call_id") for message in messages if message["role"] == "tool"}
        calls = [call for call in (pending or {}).get("tool_calls", []) if call["id"] not in completed]
        if not calls:
            from .context_budget import INPUT_TOKENS, request_tokens

            clock = runtime_clock(tools)
            initial[0]["content"] = prompt + "\n\nruntime_clock=" + json.dumps(clock)
            if (
                not getattr(gateway, "validates_request_capacity", False)
                and await asyncio.to_thread(request_tokens, initial + messages, available_tools)
                > INPUT_TOKENS
            ):
                raise ProviderError("model_context_capacity")

            async def model_call():
                return await gateway.chat(request_config, initial + messages, available_tools)

            response = (
                await execution.slot(
                    "deepseek", DEEPSEEK_REQUEST_LIMIT, lambda: execution.run("text", model_call)
                )
                if execution
                else await model_call()
            )
            await check()
            rounds += 1
            current_usage = response.get("usage") or {}
            if isinstance(current_usage, dict):
                add_usage(usage, current_usage)
            await save()
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ProviderError("invalid_model_output")
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict):
                raise ProviderError("invalid_model_output")
            if decision_mode and choice.get("finish_reason") == "length":
                protocol_error()
                messages.append(assistant(message))
                request_config["max_tokens"] = min(request_config.get("max_tokens", 4096) * 2, 393216)
                messages.append(
                    {
                        "role": "user",
                        "content": fixed("reply_repair", reason="上次输出被截断。"),
                    }
                )
                await save()
                continue
            calls = message.get("tool_calls")
            if calls:
                if choice.get("finish_reason") != "tool_calls" or not isinstance(calls, list):
                    raise ProviderError("invalid_model_output")
                ids = set(completed)
                for call in calls:
                    if (
                        not isinstance(call, dict)
                        or not isinstance(call.get("id"), str)
                        or not call["id"]
                        or len(call["id"]) > 256
                        or call["id"] in ids
                        or call.get("type") != "function"
                        or not isinstance(call.get("function"), dict)
                        or not isinstance(call["function"].get("name"), str)
                        or len(call["function"]["name"]) > 128
                        or not isinstance(call["function"].get("arguments"), str)
                        or len(call["function"]["arguments"])
                        > (
                            70000
                            if decision_mode and call["function"].get("name") == "finish_reply"
                            else 4096
                        )
                    ):
                        raise ProviderError("invalid_model_tool_call")
                    ids.add(call["id"])
                messages.append(assistant(message, calls))
            else:
                content = message.get("content")
                if decision_mode:
                    protocol_error()
                    messages.append(assistant(message))
                    messages.append(
                        {
                            "role": "user",
                            "content": fixed(
                                "reply_repair", reason="尚未调用 finish_reply，直接输出不构成有效决策。"
                            ),
                        }
                    )
                    await save()
                    continue
                if (
                    choice.get("finish_reason") != "stop"
                    or not isinstance(content, str)
                    or not content.strip()
                    or len(content) > 10000
                ):
                    raise ProviderError("invalid_model_output")
                messages.append(assistant({**message, "content": content.strip()}))
                await save()
                continue
            await save()
        for call in calls:
            await check()
            name, started = call["function"]["name"], time.monotonic()
            cached = False
            location_evidence = None
            arguments = {}
            try:
                raw = call["function"].get("arguments")
                arguments = json.loads(raw) if isinstance(raw, str) else None
                if runtime_clock(tools)["date"] != clock["date"]:
                    protocol_error()
                    raise ToolFailure(
                        "runtime_date_changed",
                        "服务端已跨日，请依据更新后的 runtime_clock 重新核对相对日期。",
                    )
                if decision_mode and name == "finish_reply":
                    if len(calls) != 1:
                        raise ProviderError("invalid_reply_decision")
                    decision = validate_decision(arguments, context)
                    result = {"ok": True, "data": decision}
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    trace.append(
                        {
                            "call_id": call["id"],
                            "name": name,
                            "arguments": decision,
                            "result": result,
                            "cached": False,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        }
                    )
                    await save()
                    return finished(decision)
                if name == HISTORY_NAME and history is not None:
                    result = (
                        await execution.run("text", lambda: database_call(history.run, arguments, call["id"]))
                        if execution
                        else await database_call(history.run, arguments, call["id"])
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    trace.append(
                        {
                            "call_id": call["id"],
                            "name": name,
                            "arguments": arguments,
                            "result": result,
                            "cached": False,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        }
                    )
                    await save()
                    continue
                if tools is None:
                    raise ToolFailure("unknown_tool", "当前仅支持finish_reply")
                tools.validate(name, arguments)
                if name == "web_fetch":
                    from .web_tools import check_fetch_scope

                    check_fetch_scope(context, trace, arguments["url"])
                if name == "get_weather":
                    from .reply_profiles import check_weather_location

                    location_evidence = check_weather_location(context, arguments)
                key = cache_key(name, arguments)
                if name != "get_current_time" and key in cache and cache_valid(name, arguments, cache[key]):
                    result, cached = cache[key], True
                else:
                    result = (
                        await execution.run("text", lambda: tools.run(name, arguments))
                        if execution
                        else await tools.run(name, arguments)
                    )
                    if name != "get_current_time" and result.get("ok"):
                        cache[key] = result
            except (ValueError, TypeError):
                if decision_mode and name == "finish_reply":
                    protocol_error()
                arguments = {}
                result = {
                    "ok": False,
                    "error": {
                        "code": "invalid_arguments",
                        "message": "工具参数不是有效JSON对象，请更正后重新调用",
                    },
                }
            except ProviderError:
                if name != "finish_reply":
                    raise
                protocol_error()
                arguments = {}
                result = {
                    "ok": False,
                    "error": {
                        "code": "invalid_reply_decision",
                        "message": fixed(
                            "reply_repair", reason="回复决策字段、来源或组合无效。请按决策契约核对。"
                        ),
                    },
                }
            except ToolFailure as error:
                result = {
                    "ok": False,
                    "error": {"code": error.code, "message": error.message, **error.details},
                }
            if name == "get_weather" and not result.get("ok"):
                result["error"].setdefault("stage", "validation")
                result["error"].setdefault("request_sent", False)
            await check()
            safe_result, safe_arguments = redact(result), redact(arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(safe_result, ensure_ascii=False),
                }
            )
            trace.append(
                {
                    "call_id": call["id"],
                    "name": name,
                    "arguments": safe_arguments,
                    "result": safe_result,
                    "cached": cached,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    **({"location_evidence": redact(location_evidence)} if location_evidence else {}),
                }
            )
            await save()
