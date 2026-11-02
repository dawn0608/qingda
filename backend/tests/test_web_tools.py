"""DeepSeek网页契约、授权范围和服务器多进程限速。"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import httpx
import psycopg
import pytest
from qingda.providers import Gateway
from qingda.reply_tools import ReplyTools as RealTools
from qingda.reply_tools import ToolFailure
from qingda.security import Vault, create_key
from qingda.store import Store
from qingda.tenancy import current_user, user_scope
from qingda.tool_generation import Checkpoint, generate
from qingda.web_tools import WebRateLimiter, check_fetch_scope, public_url, resolve_public_url
from test_automation import deliver, setup
from test_database import database_url
from test_reply_tools import Model, NoWaitLimiter, ReplyTools, call, config, response


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        ("2026-10-17T16:00:00+00:00", True),
        ("2026-10-17T15:59:59Z", False),
        ("2026-10-18T16:00:00Z", False),
        (None, False),
    ],
)
def test_search_grading_uses_beijing_publication_date(monkeypatch, timestamp, expected):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from evaluate_reply_tools import published_on

    assert published_on(timestamp, "2026-10-18") is expected


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost/a",
        "http://127.0.0.1",
        "http://169.254.169.254/a",
        "http://[::1]/",
        "https://user:pass@example.com/a",
        "file:///tmp/a",
        "https://site.internal/a",
        "https://example.com\\@127.0.0.1/a",
        "https://example.com:0/a",
        "https://example.com/\na",
    ],
)
def test_reject_nonpublic_urls(value):
    with pytest.raises(ToolFailure, match="invalid_web_url"):
        public_url(value)


def test_url_scope_current_author_and_search_only():
    context = [
        {
            "role": "user",
            "current": True,
            "sender": "a",
            "content": "阅读 https://example.com/a，告诉我日期。",
        }
    ]
    check_fetch_scope(context, [], "https://example.com/a")
    trace = [
        {
            "name": "web_search",
            "result": {"ok": True, "data": {"results": [{"url": "https://example.com/b"}]}},
        }
    ]
    check_fetch_scope(context, trace, "https://example.com/b")
    for extra in (
        {"current": False},
        {"sender": "b"},
        {"context_scope": "background"},
        {"speaker_type": "owner"},
    ):
        other = {**context[0], "content": "https://example.com/c", **extra}
        with pytest.raises(ToolFailure, match="web_url_not_authorized"):
            check_fetch_scope(context + [other], [], "https://example.com/c")


async def test_dns_private_and_mixed_results_rejected(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.215.14", 443)), (2, 1, 6, "", ("10.0.0.1", 443))],
    )
    with pytest.raises(ToolFailure, match="invalid_web_url"):
        await resolve_public_url("https://example.com/a")


async def test_fetch_content_limits_and_disclosure():
    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/v1/tools/fetch"
        assert json.loads(request.content) == {"url": "https://example.com/a"}
        return httpx.Response(200, json={"title": "网页", "content": "文" * 21000 + "本文由AI生成"})

    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    try:
        result = await tools.run("web_fetch", {"url": "https://example.com/a"})
        assert result["ok"] and result["source"] == "DeepSeek Fetch"
        data = result["data"]
        assert len(data["content"]) == 20000 and data["content_truncated"] and data["ai_generated_disclosure"]
        assert data["original_length"] > 21000
    finally:
        await tools.close()


async def test_fetch_checkpoint_reuses_only_completed_current_task_result(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    checkpoint = Checkpoint(store, store.query("SELECT id FROM tasks")[0]["id"], "web-fingerprint")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"title": "原文", "content": "报名截止10月25日"})

    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    context = [{"role": "user", "current": True, "sender": "a", "content": "阅读https://example.com/a"}]
    try:
        with pytest.raises(asyncio.CancelledError):
            await generate(
                Model(
                    [
                        response(calls=[call("web_fetch", {"url": "https://example.com/a"})]),
                        asyncio.CancelledError(),
                    ]
                ),
                tools,
                {},
                "prompt",
                context,
                checkpoint,
            )
        result = await generate(
            Model(
                [
                    response(calls=[call("web_fetch", {"url": "https://example.com/a"}, "c2")]),
                    response("10月25日"),
                ]
            ),
            tools,
            {},
            "prompt",
            context,
            checkpoint,
        )
        assert len(requests) == 1 and result["tool_trace"]["tool_calls"][-1]["cached"]
        assert Checkpoint(store, checkpoint.task_id, "changed-tools-fingerprint").load() == ([], {}, [], 0)
        denied = await generate(
            Model(
                [
                    response(calls=[call("web_fetch", {"url": "https://example.com/guessed"})]),
                    response("无法读取"),
                ]
            ),
            tools,
            {},
            "prompt",
            context,
        )
        assert denied["tool_trace"]["tool_calls"][0]["result"]["error"]["code"] == "web_url_not_authorized"
        assert len(requests) == 1
    finally:
        await tools.close()


@pytest.mark.parametrize(
    "body,code",
    [
        ({"results": []}, None),
        ({}, "invalid_search_response"),
        ({"results": {}}, "invalid_search_response"),
        ({"results": [{"url": "https://example.com/a", "summary": {}}]}, "invalid_search_response"),
    ],
)
async def test_search_empty_and_malformed(body, code):
    tools = ReplyTools(config(), transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)))
    try:
        result = await tools.run("web_search", {"query": "词"})
        assert result["error"]["code"] == code if code else result["ok"] and not result["data"]["results"]
    finally:
        await tools.close()


@pytest.mark.parametrize("body", [{}, {"title": "空", "content": " "}, {"title": [], "content": "正文"}])
async def test_fetch_invalid_content(body):
    tools = ReplyTools(config(), transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)))
    try:
        result = await tools.run("web_fetch", {"url": "https://example.com/a"})
        assert result["error"]["code"] == "invalid_fetch_response"
    finally:
        await tools.close()


async def test_query_limit_and_old_parameter_rejected():
    tools = ReplyTools(config())
    try:
        for args in ({"query": "字" * 129}, {"query": "词", "freshness": "oneDay"}):
            assert not (await tools.run("web_search", args))["ok"]
        tools.validate("web_search", {"query": "字" * 128})
        tools.validate("web_fetch", {"url": "https://example.com/" + "a" * 1000})
    finally:
        await tools.close()


async def test_missing_key_and_concurrent_user_credentials():
    seen = []

    async def key():
        await asyncio.sleep(0)
        return "key-user-" + str(current_user.get())

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"results": []})

    tools = RealTools(
        config(), key_provider=key, limiter=NoWaitLimiter(), transport=httpx.MockTransport(handler)
    )

    async def run(uid):
        with user_scope(uid):
            return await tools.run("web_search", {"query": "测试"})

    try:
        results = await asyncio.gather(run(1), run(2))
        assert all(v["ok"] for v in results)
        assert set(seen) == {"Bearer key-user-1", "Bearer key-user-2"}
        assert "key-user" not in json.dumps(results)
    finally:
        await tools.close()


async def test_gateway_reads_encrypted_keys_under_real_rls(tmp_path, isolated_role):
    admin = Store(database_url(tmp_path / "rls"))
    role = isolated_role
    with admin.connect() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(
            "INSERT INTO users(id,channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url) VALUES(2,'email','isolated@example.com','隔离用户',0,'isolated-hash','isolated-key','https://example.com/callback')"
        )
        conn.execute(
            psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                psycopg.sql.Identifier(schema), psycopg.sql.Identifier(role)
            )
        )
        conn.execute(
            psycopg.sql.SQL("GRANT SELECT ON settings,schema_versions TO {}").format(
                psycopg.sql.Identifier(role)
            )
        )
        conn.execute(
            psycopg.sql.SQL("GRANT SELECT,UPDATE ON web_tool_rate_limits TO {}").format(
                psycopg.sql.Identifier(role)
            )
        )
    create_key(tmp_path / "vault.key")
    vault = Vault(tmp_path / "vault.key")
    for uid in (1, 2):
        with user_scope(uid):
            admin.set_setting("model_key", vault.encrypt("key-" + str(uid)))
    parsed = urlsplit(admin.url)
    query = parse_qs(parsed.query)
    query["options"] = [query.get("options", [""])[0] + " -c role=" + role]
    url = urlunsplit(parsed._replace(query=urlencode({k: v[0] for k, v in query.items()}, quote_via=quote)))
    runtime = Store(database_url(url))
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"results": []})

    gateway = Gateway(
        SimpleNamespace(store=runtime, vault=vault),
        transport=httpx.MockTransport(handler),
        tool_config=SimpleNamespace(database_url=url),
    )
    try:

        async def call_as(uid):
            with user_scope(uid):
                return await gateway.reply_tools.run("web_search", {"query": "公开关键词"})

        results = await asyncio.gather(call_as(1), call_as(2))
        assert all(row["ok"] for row in results), results
        assert set(seen) == {"Bearer key-1", "Bearer key-2"}
        with user_scope(1):
            admin.set_setting("model", {"web_search_enabled": False})
        assert (await call_as(1))["ok"]
        assert (await call_as(2))["ok"]
        assert seen[-1] == "Bearer key-2"
        assert not (await call_as(3))["ok"]
        with user_scope(None):
            assert not (await gateway.reply_tools.run("web_search", {"query": "公开关键词"}))["ok"]
        assert len(seen) == 4
    finally:
        await gateway.close()
        runtime.close()
    tools = RealTools(config(), limiter=NoWaitLimiter())
    try:
        assert (await tools.run("web_search", {"query": "词"}))["error"]["code"] == "missing_deepseek_key"
    finally:
        await tools.close()


@pytest.mark.parametrize(
    "status,attempts", [(400, 1), (401, 1), (402, 1), (403, 1), (404, 1), (408, 2), (429, 2), (504, 2)]
)
async def test_fetch_retry_and_usage(status, attempts):
    tools = ReplyTools(config(), transport=httpx.MockTransport(lambda req: httpx.Response(status, json={})))
    try:
        result = await tools.run("web_fetch", {"url": "https://example.com/a"})
        assert result["error"]["code"] == "provider_http_" + str(status)
        assert result["request_usage"]["requests"] == attempts
    finally:
        await tools.close()


async def test_shared_rate_clock_two_processes_and_crash(tmp_path):
    store = Store(database_url(tmp_path / "clock"))
    actor = Path(__file__).with_name("web_rate_actor.py")
    env = {**os.environ, "ACTOR_DATABASE_URL": store.url}
    first = subprocess.Popen(
        [sys.executable, str(actor), "run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        [sys.executable, str(actor), "run"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    one, err_one = await asyncio.to_thread(first.communicate, timeout=15)
    two, err_two = await asyncio.to_thread(second.communicate, timeout=15)
    assert first.returncode == second.returncode == 0, err_one + err_two
    starts = sorted(json.loads(one) + json.loads(two))
    assert all(b - a >= 0.23 for a, b in zip(starts, starts[1:]))
    limiter = WebRateLimiter(store.url)
    try:
        assert limiter.acquire("fetch") == 0
        assert limiter.acquire("fetch") > 0
        await asyncio.sleep(0.3)
        crashed = await asyncio.to_thread(
            subprocess.run, [sys.executable, str(actor), "crash"], env=env, capture_output=True, timeout=10
        )
        assert crashed.returncode == 23
        await asyncio.wait_for(limiter.wait("search"), timeout=1)
        waiting = asyncio.create_task(limiter.wait("search"))
        await asyncio.sleep(0.02)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await asyncio.wait_for(limiter.wait("search"), timeout=1)
    finally:
        limiter.close()
    restarted = WebRateLimiter(store.url)
    try:
        started = time.monotonic()
        await restarted.wait("search")
        assert time.monotonic() - started >= 0.15
        with store.connect() as conn:
            conn.execute(
                "UPDATE web_tool_rate_limits SET next_allowed_at=clock_timestamp()+interval '10 seconds' WHERE resource='fetch'"
            )
        with pytest.raises(ToolFailure, match="web_tool_busy"):
            await restarted.wait("fetch")
    finally:
        restarted.close()
