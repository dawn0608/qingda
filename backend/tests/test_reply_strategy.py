"""场景策略服务器隔离回归；不访问真实模型或微信。"""

import asyncio
import json

import pytest
from test_automation import NOW, deliver, setup
from test_group_wakeup import GROUP, group_setup, text
from test_reply_tools import Model, call, response
from test_worker import Gateway
from wechat_manager import reply_decisions
from wechat_manager.providers import ProviderError
from wechat_manager.tool_generation import Checkpoint, generate
from wechat_manager.worker import Worker


def decision(action="reply", content="已了解", reason="answer", close=False, ids=None):
    return {
        "action": action,
        "content": content,
        "reason": reason,
        "close_dialogue": close,
        "handled_message_ids": ids or [],
    }


def final(value, identity="final"):
    return response(calls=[call("finish_reply", value, identity)])


def latest(store):
    return store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 1")[0]


@pytest.mark.parametrize(
    "value",
    [
        decision("silent", "仍然有正文", "ack_only"),
        decision("reply", "", "answer"),
        decision("silent", "", "answer"),
        decision("silent", "", "off_topic", True),
        {**decision(), "recipient": "other"},
        {**decision(), "close_dialogue": 1},
        decision("reply", "x" * 10001),
    ],
)
def test_invalid_decisions_are_never_silence(value):
    with pytest.raises(ProviderError, match="invalid_reply_decision"):
        reply_decisions.validate(value)


@pytest.mark.parametrize("value", [decision(), decision("silent", "", "social_close", True)])
async def test_native_finish_has_single_round_and_no_business_tool(value):
    model = Model([final(value)])
    result = await generate(model, None, {}, "p", [], decision_mode=True)
    assert result["reply_decision"] == value
    assert result["choices"][0]["message"]["content"] == value["content"]
    assert len(model.requests) == 1


async def test_malformed_or_plain_response_is_corrected_not_sent():
    model = Model([response("不应发送的原文"), final(decision("silent", "", "ack_only", True))])
    result = await generate(model, None, {}, "p", [], decision_mode=True)
    assert result["reply_decision"]["action"] == "silent" and len(model.requests) == 2
    with pytest.raises(ProviderError, match="invalid_reply_decision"):
        await generate(Model([response("")] * 3), None, {}, "p", [], decision_mode=True)


async def test_invalid_finish_arguments_can_repair_and_long_answer_is_complete():
    value = decision(content="长" * 6000)
    model = Model([final(decision("silent", "错误正文", "ack_only")), final(value, "fixed")])
    result = await generate(model, None, {}, "p", [], decision_mode=True)
    assert result["reply_decision"] == value


@pytest.mark.parametrize(
    "question,expected_calls", [("比较两个方案", 1), ("完整原样转述下面的内容", 1), ("写1500字故事", 1)]
)
async def test_long_reply_does_not_add_rewrite_or_truncate(question, expected_calls):
    value = decision(content="必要条件" * 80, ids=[1])
    context = [{"current": True, "role": "user", "message_id": 1, "content": question}]
    model = Model([final(value), final(value, "reviewed")])
    result = await generate(model, None, {}, "p", context, decision_mode=True)
    assert len(model.requests) == expected_calls
    assert result["reply_decision"]["content"] == value["content"]
    assert result["tool_trace"]["style_reviewed"] == (expected_calls == 2)


async def test_long_reply_checkpoint_restart_never_rewrites(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    task = latest(s)
    context = [
        {"current": True, "role": "user", "message_id": task["message_id"], "content": "简单比较两个方案"}
    ]
    value = decision(content="必要条件" * 80, ids=[task["message_id"]])
    checkpoint = Checkpoint(s, task["id"], "style-review")
    await generate(Model([final(value)]), None, {}, "p", context, checkpoint=checkpoint, decision_mode=True)
    model = Model([])
    result = await generate(model, None, {}, "p", context, checkpoint=checkpoint, decision_mode=True)
    assert len(model.requests) == 0 and result["reply_decision"] == value


async def test_final_checkpoint_restart_does_not_call_model_again(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    checkpoint = Checkpoint(s, latest(s)["id"], "same")
    value = decision("silent", "", "ack_only", True)
    await generate(Model([final(value)]), None, {}, "p", [], checkpoint=checkpoint, decision_mode=True)
    result = await generate(Model([]), None, {}, "p", [], checkpoint=checkpoint, decision_mode=True)
    assert result["reply_decision"] == value


async def test_silence_is_terminal_releases_queue_and_is_not_assistant_history(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    gateway = Gateway()
    original = gateway.model

    async def silent(*args):
        return {**response(""), "reply_decision": decision("silent", "", "ack_only", True)}

    gateway.model = silent
    worker = Worker(m, gateway, clock=lambda: NOW + 5)
    try:
        await worker.once()
        assert latest(s)["state"] == "skipped" and latest(s)["reason"] == "reply_silent_ack_only"
        assert not gateway.sends and not s.query("SELECT * FROM account_send_clock")
        await worker.once()
        gateway.model = original
        deliver(s, m, 2, received=NOW + 10, Content={"string": "新的问题"})
        worker.clock = lambda: NOW + 13
        await worker.once()
        assert len(gateway.sends) == 1
        context = json.loads(latest(s)["context_json"])
        assert not [x for x in context if x.get("role") == "assistant"]
    finally:
        worker.release()


@pytest.mark.parametrize(
    "body,wake",
    [("WeMind，你好", True), (" WeMind", True), ("WeMind123", False), ("有人说WeMind，你好", False)],
)
def test_bare_name_requires_full_prefix_boundary(tmp_path, body, wake):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m, body=body)
    assert (latest(s)["state"] == "queued") is wake


async def test_group_waits_two_seconds_and_regenerates_corrected_input(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m, body="@WeMind 原编号A")
    gateway, now = Gateway(), [NOW + 3]
    contexts, started = [], asyncio.Event()

    async def model(*args):
        contexts.append(args[-1])
        if len(contexts) == 1:
            started.set()
            await asyncio.Event().wait()
        return response("B")

    gateway.model = model
    worker = Worker(m, gateway, clock=lambda: now[0])
    try:
        assert await worker.once() == 0
        now[0] = NOW + 4
        running = asyncio.create_task(worker.once())
        await asyncio.wait_for(started.wait(), 2)
        text(s, m, mid=2, body="更正为B，只输出编号", received=NOW + 5)
        await asyncio.wait_for(running, 2)
        assert not gateway.sends
        now[0] = NOW + 7
        await worker.once()
        assert [row[2] for row in gateway.sends] == ["B"]
        assert len([x for x in contexts[-1] if x.get("current")]) == 2
    finally:
        worker.release()


def test_group_collection_is_member_scoped_and_capped(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    for i, at in enumerate((4, 8, 11), 2):
        text(s, m, mid=i, body="补充", received=NOW + at)
    deliver(s, m, 8, peer=GROUP, received=NOW + 9, Content={"string": "another:\n@WeMind 另一个问题"})
    rounds = s.query("SELECT * FROM group_reply_rounds ORDER BY first_at")
    assert len(rounds) == 2 and rounds[0]["collect_until"] == NOW + 12
    with s.connect() as c:
        from wechat_manager.reply_queue import members

        batch = members(c, s.query("SELECT * FROM tasks WHERE id=%s", (rounds[0]["task_id"],))[0])
    assert len(batch) == 4


def test_continuation_candidates_do_not_renew_window(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    text(s, m, mid=2, body="别人的闲聊", created=NOW + 100)
    window = s.query("SELECT * FROM dialogue_windows")[0]
    assert window["expires_at"] == NOW + 122
    text(s, m, mid=3, body="已经超时", created=NOW + 200)
    assert latest(s)["reason"] == "not_explicitly_mentioned"


async def test_accepted_reply_renews_and_silence_closes_only_own_member(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    gateway = Gateway()
    worker = Worker(m, gateway, clock=lambda: NOW + 10)
    try:
        await worker.once()
        assert s.query("SELECT * FROM dialogue_windows")[0]["expires_at"] == NOW + 130
        text(s, m, mid=2, body="谢谢", received=NOW + 20)

        async def silent(*args):
            return {**response(""), "reply_decision": decision("silent", "", "social_close", True)}

        gateway.model = silent
        worker.clock = lambda: NOW + 22
        await worker.once()
        assert s.query("SELECT * FROM dialogue_windows")[0]["closed_reason"] == "social_close"
        text(s, m, mid=3, body="继续", received=NOW + 25)
        assert latest(s)["reason"] == "not_explicitly_mentioned"
        text(s, m, mid=4, body="@WeMind 新问题", received=NOW + 26)
        assert latest(s)["state"] == "queued"
    finally:
        worker.release()


async def test_verified_assistant_quote_wakes_but_other_targets_do_not(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    worker = Worker(m, Gateway(), clock=lambda: NOW + 5)
    try:
        await worker.once()
    finally:
        worker.release()
    for mid, reference, source, expected in [
        (2, "8895067626106766584", "", "reply_reference"),
        (3, "1", "", None),
        (4, "8895067626106766584", "<msgsource><atuserlist>other</atuserlist></msgsource>", None),
    ]:
        deliver(
            s,
            m,
            mid,
            peer=GROUP,
            received=NOW + 10 + mid,
            MsgType=49,
            source=source,
            Content={
                "string": f"another{mid}:\n<msg><appmsg><type>57</type><title>请解释</title><refermsg><svrid>{reference}</svrid><content>伪造引用正文</content></refermsg></appmsg></msg>"
            },
        )
        task = latest(s)
        metadata = json.loads(
            s.query("SELECT metadata FROM messages WHERE id=%s", (task["message_id"],))[0]["metadata"]
        )
        assert metadata["wakeup_reason"] == expected
        if expected:
            context = m.context(task)
            assert context[-1]["reference"]["content"] == "收到"
            assert context[-1]["content"] == "请解释"
            assert "伪造引用正文" not in json.dumps(context, ensure_ascii=False)


async def test_group_context_separates_background_and_reply_target(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    deliver(s, m, 1, peer=GROUP, Content={"string": "someone:\n我的编号OTHER"})
    text(s, m, mid=2, body="@WeMind 我的编号A", received=NOW + 3)
    deliver(s, m, 3, peer=GROUP, received=NOW + 4, Content={"string": "someone:\n我的编号是B"})
    text(s, m, mid=4, body="只输出我的编号", received=NOW + 5)
    task = s.query("SELECT * FROM tasks WHERE state='queued'")[0]
    context = m.context(task)
    current = [x for x in context if x.get("current")]
    background = [x for x in context if x.get("context_scope") == "background"]
    assert len(current) == 2 and all(x["reply_target"] == "wxid_friend" for x in current)
    assert len(background) == 2 and all(x["sender"] == "someone" for x in background)


async def test_truncated_decision_is_retried_with_more_capacity():
    class CapacityModel(Model):
        async def chat(self, config, messages, tools):
            self.capacities = getattr(self, "capacities", []) + [config["max_tokens"]]
            return await super().chat(config, messages, tools)

    model = CapacityModel([response("半截正文", finish="length"), final(decision(content="完整正文"))])
    result = await generate(model, None, {"max_tokens": 4096}, "p", [], decision_mode=True)
    assert model.capacities == [4096, 8192]
    assert result["reply_decision"]["content"] == "完整正文"


async def test_reference_isolation_and_revocation_cancel_queued_reply(tmp_path):
    from wechat_manager.reply_references import assistant_reply

    s, m, a, group = group_setup(tmp_path)
    text(s, m)
    worker = Worker(m, Gateway(), clock=lambda: NOW + 5)
    try:
        await worker.once()
    finally:
        worker.release()
    pid = "8895067626106766584"
    with s.connect() as conn:
        assert assistant_reply(conn, a["id"], group["id"], pid, NOW + 10)
        assert not assistant_reply(conn, a["id"] + 1, group["id"], pid, NOW + 10)
        assert not assistant_reply(conn, a["id"], group["id"] + 1, pid, NOW + 10)
        assert not assistant_reply(conn, a["id"], group["id"], pid, NOW + 4)
    deliver(
        s,
        m,
        2,
        peer=GROUP,
        received=NOW + 10,
        MsgType=49,
        Content={
            "string": f"another:\n<msg><appmsg><type>57</type><title>请解释</title><refermsg><svrid>{pid}</svrid></refermsg></appmsg></msg>"
        },
    )
    task = latest(s)
    assert task["state"] == "queued"
    deliver(
        s,
        m,
        3,
        peer=GROUP,
        received=NOW + 11,
        MsgType=10002,
        Content={
            "string": f'<sysmsg type="revokemsg"><revokemsg><session>{GROUP}</session><newmsgid>{pid}</newmsgid></revokemsg></sysmsg>'
        },
    )
    with s.connect() as conn:
        assert not assistant_reply(conn, a["id"], group["id"], pid, NOW + 11)
    assert m.context(task)[-1]["reference"]["unavailable"]
    gateway = Gateway()
    worker = Worker(m, gateway, clock=lambda: NOW + 12)
    try:
        await worker.once()
        result = s.query("SELECT * FROM tasks WHERE id=%s", (task["id"],))[0]
        assert result["state"] == "canceled"
        assert result["reason"] == "reference_unavailable" and not gateway.sends
    finally:
        worker.release()


async def test_group_image_collection_survives_restart_and_sending_never_replays(tmp_path):
    from wechat_manager.service import Manager
    from wechat_manager.store import Store

    s, m, _, _ = group_setup(tmp_path)
    deliver(
        s,
        m,
        1,
        peer=GROUP,
        received=NOW + 2,
        MsgType=3,
        source="<msgsource><atuserlist>wxid_me</atuserlist></msgsource>",
        Content={"string": "wxid_friend:\n<msg><img /></msg>"},
    )
    assert s.query("SELECT collect_until FROM group_reply_rounds")[0]["collect_until"] == NOW + 5
    reopened = Store(s.url)
    manager = Manager(reopened, clock=lambda: NOW + 4)
    gateway = Gateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 4)
    try:
        assert await worker.once() == 0 and not gateway.sends
    finally:
        worker.release()
    with reopened.connect() as conn:
        conn.execute("UPDATE tasks SET state='sending'")
    worker = Worker(manager, gateway, clock=lambda: NOW + 8)
    try:
        await worker.once()
        assert latest(reopened)["state"] == "unknown" and not gateway.sends
    finally:
        worker.release()
