"""拍问输入、处理归属和工具事实边界；仅在服务器隔离 PostgreSQL 执行。"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_automation import NOW, deliver, setup
from test_dialogue_windows import private_pat
from test_group_wakeup import group_setup, pat, text
from test_reply_strategy import decision
from test_reply_tools import ReplyTools, config
from test_worker import Gateway
from wechat_manager.providers import Gateway as RealGateway
from wechat_manager.providers import ProviderError
from wechat_manager.reply_decisions import validate
from wechat_manager.reply_profiles import check_weather_location
from wechat_manager.reply_tools import ToolFailure
from wechat_manager.worker import Worker


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("order", ["pat_first", "question_first", "callback_reversed"])
async def test_pat_is_signal_and_question_has_single_coverage(tmp_path, group, order):
    s, m, _, _ = (group_setup if group else setup)(tmp_path)
    p = pat if group else private_pat

    def q(mid, **kw):
        if group:
            text(s, m, mid=mid, body="评价作品", **kw)
        else:
            deliver(s, m, mid, Content={"string": "评价作品"}, **kw)

    if order == "pat_first":
        p(s, m, mid=1)
        q(2)
    else:
        q(1, created=NOW + 1)
        p(s, m, mid=2, created=NOW if order == "callback_reversed" else NOW + 2, received=NOW + 2)
    captured = []
    gateway = Gateway()
    original = gateway.model

    async def capture(*args):
        captured.append(args[-1])
        return await original(*args)

    gateway.model = capture
    worker = Worker(m, gateway, clock=lambda: NOW + 8)
    try:
        await worker.once()
        await worker.once()
        assert len(gateway.sends) == 1
        view = json.loads(await RealGateway.model_input(SimpleNamespace(manager=m), captured[0]))
        assert len(view["interaction_signals"]) == 1
        questions = view["current_messages"] or view["pending_messages"]
        assert [v["content"] for v in questions] == ["评价作品"]
        record = s.query("SELECT handled_message_ids FROM tasks WHERE handled_message_ids != '[]'::jsonb")[0]
        assert json.loads(record["handled_message_ids"]) == [questions[0]["message_id"]]
    finally:
        worker.release()


@pytest.mark.parametrize("group", [False, True])
async def test_presence_repeat_persists_but_new_question_is_answered(tmp_path, group):
    s, m, _, _ = (group_setup if group else setup)(tmp_path)
    p = pat if group else private_pat
    p(s, m, mid=1)
    g = Gateway()
    worker = Worker(m, g, clock=lambda: NOW + 6)
    await worker.once()
    worker.release()
    assert len(g.sends) == 1
    p(s, m, mid=2, created=NOW + 10, received=NOW + 10)
    worker = Worker(m, g, clock=lambda: NOW + 15)
    try:
        await worker.once()
        assert len(g.sends) == 1
        assert s.query(
            "SELECT t.state,t.reason FROM tasks t JOIN messages m ON m.id=t.message_id WHERE m.platform_id='2'"
        )[0] == {"state": "skipped", "reason": "pat_already_answered"}
        if group:
            text(s, m, mid=3, body="新的明确问题", created=NOW + 16, received=NOW + 16)
        else:
            deliver(s, m, 3, created=NOW + 16, received=NOW + 16)
        worker.clock = lambda: NOW + 20
        await worker.once()
        assert len(g.sends) == 2
    finally:
        worker.release()


@pytest.mark.parametrize("group", [False, True])
async def test_question_during_pat_generation_invalidates_old_result(tmp_path, group):
    s, m, _, _ = (group_setup if group else setup)(tmp_path)
    (pat if group else private_pat)(s, m, mid=1)
    g, started = Gateway(), asyncio.Event()
    contexts = []
    original = g.model

    async def model(*args):
        contexts.append(args[-1])
        if len(contexts) == 1:
            started.set()
            await asyncio.Event().wait()
        return await original(*args)

    g.model = model
    worker = Worker(m, g, clock=lambda: NOW + 6)
    try:
        running = asyncio.create_task(worker.once())
        await asyncio.wait_for(started.wait(), 3)
        if group:
            text(s, m, mid=2, body="只输出新答案", created=NOW + 7, received=NOW + 7)
        else:
            deliver(s, m, 2, Content={"string": "只输出新答案"}, created=NOW + 7, received=NOW + 7)
        await asyncio.wait_for(running, 3)
        assert not g.sends
        worker.clock = lambda: NOW + 12
        await worker.once()
        assert len(g.sends) == 1 and len(contexts) == 2
        assert (
            s.query("SELECT handled_message_ids FROM tasks WHERE handled_message_ids != '[]'::jsonb")[0][
                "handled_message_ids"
            ]
            == "[2]"
        )
    finally:
        worker.release()


def test_coverage_rejects_signal_reference_history_and_missing_current():
    context = [
        {"current": True, "message_id": 1, "sender": "a", "reply_target": "a", "message_kind": "pat"},
        {"current": True, "message_id": 2, "sender": "a", "message_kind": "text"},
        {"current": True, "message_id": 3, "sender": "a", "input_scope": "pending"},
        {"message_id": 4, "sender": "a"},
    ]
    assert validate(decision(ids=[2]), context)["handled_message_ids"] == [2]
    assert validate(decision(ids=[2, 3]), context)["handled_message_ids"] == [2, 3]
    for ids in ([], [1], [1, 2], [2, 4], [2, 2]):
        with pytest.raises(ProviderError):
            validate(decision(ids=ids), context)
    with pytest.raises(ProviderError):
        validate(decision(reason="presence"), context)


def test_stop_requires_a_current_handled_message_not_an_old_instruction():
    context = [
        {"message_id": 1, "sender": "a", "content": "先别说了", "context_scope": "background"},
        {"current": True, "message_id": 2, "sender": "a", "input_scope": "pending", "content": "只输出H2141"},
        {"current": True, "message_id": 3, "sender": "a", "message_kind": "pat"},
    ]
    with pytest.raises(ProviderError, match="invalid_reply_decision"):
        validate(decision("silent", "", "explicit_silence", True), context)
    assert validate(decision(content="H2141", ids=[2]), context)["handled_message_ids"] == [2]
    assert validate(decision("silent", "", "explicit_silence", True, [2]), context)["close_dialogue"]


async def test_same_sender_unhandled_chat_stays_background(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m, mid=1, body="同成员对别人说的闲聊")
    text(s, m, mid=2, body="@WeMind 第一次问题", created=NOW + 3, received=NOW + 3)
    g = Gateway()
    worker = Worker(m, g, clock=lambda: NOW + 7)
    try:
        await worker.once()
        text(s, m, mid=3, body="第二次问题", created=NOW + 10, received=NOW + 10)
        task = s.query(
            "SELECT t.* FROM tasks t JOIN messages m ON m.id=t.message_id WHERE m.platform_id='3'"
        )[0]
        view = m.context(task)
        assert next(v for v in view if v["message_id"] == 1)["context_scope"] == "background"
        assert next(v for v in view if v["message_id"] == 2)["context_scope"] == "related"
    finally:
        worker.release()


def test_weather_requires_requester_evidence_without_profiles():
    context = [
        {"role": "user", "current": True, "sender": "a", "reply_target": "a", "content": "明天天气如何"},
        {"role": "user", "sender": "b", "content": "北京", "context_scope": "background"},
    ]
    with pytest.raises(ToolFailure):
        check_weather_location(context, {"location": "北京", "location_evidence": "北京"})
    context[0]["content"] = "重庆明天天气如何"
    check_weather_location(context, {"location": "重庆", "location_evidence": "重庆"})


def test_weather_accepts_explicit_location_in_own_pending_question():
    context = [
        {
            "role": "user",
            "current": True,
            "sender": "a",
            "reply_target": "a",
            "input_scope": "pending",
            "content": "重庆天气怎么样",
        },
        {"role": "user", "current": True, "sender": "a", "message_kind": "pat"},
    ]
    arguments = {"location": "重庆", "location_evidence": "重庆"}
    check_weather_location(context, arguments)
    for change in ({"sender": "b"}, {"context_scope": "background"}, {"speaker_type": "owner"}):
        with pytest.raises(ToolFailure):
            check_weather_location([{**context[0], **change}, context[1]], arguments)


async def test_search_preserves_generation_disclosure_beyond_summary_limit():
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/a",
                        "title": "评价",
                        "summary": "文" * 2100 + "本文由AI生成",
                        "date": None,
                    }
                ]
            },
        )
    )
    tools = ReplyTools(config(), transport=transport)
    try:
        result = await tools.run("web_search", {"query": "作品评价"})
        value = result["data"]["results"][0]
        assert value["published_at"] is None and value["crawled_at"] is None
        assert value["ai_generated_disclosure"] and value["summary_truncated"]
    finally:
        await tools.close()


@pytest.mark.parametrize("termination", ["revision", "crash"])
def test_two_process_pat_revision_capacity_and_crash_recovery(tmp_path, termination):
    s, m, _, _ = setup(tmp_path)
    private_pat(s, m, mid=1)
    actor = Path(__file__).with_name("pg_process_actor.py")
    env = {
        **os.environ,
        "ACTOR_DATABASE_URL": s.url,
        "ACTOR_CLOCK": str(NOW + 10),
        "PGAPPNAME": "wechat-manager-reply-policy-actor",
    }
    first = subprocess.Popen(
        [sys.executable, str(actor), "hold"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 10
        while not s.query("SELECT * FROM audit WHERE action='isolated_generation_started'"):
            assert first.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        excess = subprocess.run(
            [sys.executable, str(actor), "lease"], env=env, capture_output=True, text=True, timeout=10
        )
        assert excess.returncode == 0 and "lease_busy" in excess.stdout
        deliver(s, m, 2, created=NOW + 11, received=NOW + 11, Content={"string": "更正，只输出新的答案"})
        if termination == "crash":
            first.kill()
        first.communicate(timeout=10)
        assert not s.query("SELECT * FROM audit WHERE action='isolated_send_boundary'")
        resumed = subprocess.run(
            [sys.executable, str(actor), "resume"],
            env={**env, "ACTOR_CLOCK": str(NOW + 150)},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert resumed.returncode == 0, resumed.stderr
        assert len(s.query("SELECT * FROM audit WHERE action='isolated_send_boundary'")) == 1
        assert (
            s.query("SELECT handled_message_ids FROM tasks WHERE handled_message_ids != '[]'::jsonb")[0][
                "handled_message_ids"
            ]
            == "[2]"
        )
        assert s.query("SELECT * FROM worker_lock") == []
        assert (
            s.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name='wechat-manager-reply-policy-actor'"
            )[0]["n"]
            == 0
        )
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)
