"""服务器隔离 PostgreSQL：分条等待、成员隔离、释放恢复与原文授权。"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_automation import NOW, deliver, setup
from test_group_wakeup import group_setup, text
from test_reply_strategy import decision, latest
from test_worker import Gateway
from wechat_manager import input_waits, reply_decisions
from wechat_manager.providers import ProviderError
from wechat_manager.worker import Worker


class WaitingGateway(Gateway):
    def __init__(self):
        super().__init__()
        self.control = "continue"
        self.contexts = []
        self.control_calls = []

    async def model(self, config, prompt, context):
        self.contexts.append(context)
        actual = [v for v in context if v.get("current") and v.get("input_scope") != "deferred"]
        wait = next((v for v in actual if "等我发完" in v["content"]), None)
        if wait:
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": ""}}],
                "reply_decision": {
                    **decision("wait", "", "awaiting_input"),
                    "wait_evidence": {"message_id": wait["message_id"], "quote": "等我发完"},
                },
            }
        return await super().model(config, prompt, context)

    async def chat(self, config, messages, tools):
        assert [t["function"]["name"] for t in tools] == ["finish_input_control"]
        body = json.loads(messages[1]["content"])
        self.control_calls.append(body)
        last = body["current_messages"][-1]
        from test_reply_tools import call, response

        return response(
            calls=[
                call(
                    "finish_input_control",
                    {
                        "action": self.control,
                        "message_id": last["message_id"],
                        "quote": last["content"],
                    },
                )
            ]
        )


def message(store, manager, mid, content, at, *, group=False):
    if group:
        text(store, manager, mid=mid, body=content, received=NOW + at, created=NOW + at)
    else:
        deliver(store, manager, mid, Content={"string": content}, received=NOW + at, created=NOW + at)


async def begin(tmp_path, *, group=False):
    store, manager, account, conv = (group_setup if group else setup)(tmp_path)
    gateway = WaitingGateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    message(store, manager, 1, ("@WeMind " if group else "") + "等我发完，再总结", 2, group=group)
    await worker.once()
    assert latest(store)["reason"] == "reply_waiting_input", latest(store)
    assert not gateway.sends
    return store, manager, account, conv, gateway, worker


@pytest.mark.parametrize("group", [False, True])
async def test_wait_collect_release_once_after_five_minutes(tmp_path, group):
    s, m, _, _, gateway, worker = await begin(tmp_path, group=group)
    try:
        deadline = s.query("SELECT expires_at FROM reply_input_waits")[0]["expires_at"]
        message(s, m, 2, "第一项预算是 120 元", 20, group=group)
        worker.clock = lambda: NOW + 23
        await worker.once()
        assert latest(s)["reason"] == "reply_wait_collecting"
        assert s.query("SELECT expires_at FROM reply_input_waits")[0]["expires_at"] == deadline
        assert not s.query("SELECT * FROM reply_resource_slots")
        gateway.control = "resume"
        message(s, m, 3, "发完了，请开始", 320, group=group)
        worker.clock = lambda: NOW + 323
        await worker.once()
        assert latest(s)["state"] == "accepted", latest(s)
        actual = [v for v in gateway.contexts[-1] if v.get("current")]
        assert len(actual) == 3 and actual[0]["input_scope"] == "deferred"
        assert "120" in actual[1]["content"]
        assert len(gateway.sends) == 1
        assert json.loads(latest(s)["usage_json"])["total_tokens"] == 22
        worker.release()
        restarted = Worker(m, gateway, clock=lambda: NOW + 324)
        await restarted.once()
        assert len(gateway.sends) == 1
        restarted.release()
    finally:
        worker.release()


@pytest.mark.parametrize("group", [False, True])
async def test_text_only_wait_does_not_reuse_unrelated_history_pixels(tmp_path, group):
    """生产消息906-914：合计数字触发了历史图片，回复虽正确却增加无关输入。"""
    from test_reply_images import Images, media, picture

    s, m, _, _ = (group_setup if group else setup)(tmp_path)
    media(m, tmp_path)
    gateway = WaitingGateway()
    gateway.download_media = Images().download_media
    worker = Worker(m, gateway, clock=lambda: NOW + 20)
    picture(s, m, 100, group=group)
    if group:
        text(s, m, 101, "WeMind，分析这张图片", created=NOW + 5)
    try:
        await worker.once()
        assert any(v.get("images") for v in gateway.contexts[-1])
        assert len(gateway.sends) == 1
        for mid, content, at, control in [
            (102, "等我发完再算合计", 30, "continue"),
            (103, "第一项120元，第二项80元", 40, "continue"),
            (104, "发完了，只输出合计数字", 50, "resume"),
        ]:
            gateway.control = control
            message(s, m, mid, content, at, group=group)
            worker.clock = lambda: NOW + at + 3
            await worker.once()
            if control == "continue":
                assert len(gateway.sends) == 1
        assert len(gateway.sends) == 2
        assert not any(v.get("images") for v in gateway.contexts[-1])
        assert len([v for v in gateway.contexts[-1] if v.get("input_scope") == "deferred"]) == 2
        assert not s.query(
            "SELECT * FROM reply_media_dependencies WHERE task_id="
            "(SELECT release_task_id FROM reply_input_waits)"
        )
    finally:
        worker.release()


@pytest.mark.parametrize("group", [False, True])
async def test_correction_during_released_generation_keeps_material_bound_once(tmp_path, group):
    s, m, _, _, gateway, worker = await begin(tmp_path, group=group)
    started = asyncio.Event()
    contexts = []

    async def model(config, prompt, context):
        contexts.append(context)
        if len(contexts) == 1:
            started.set()
            await asyncio.Event().wait()
        return {"choices": [{"finish_reason": "stop", "message": {"content": "150"}}]}

    try:
        message(s, m, 2, "预算120元", 20, group=group)
        worker.clock = lambda: NOW + 23
        await worker.once()
        gateway.control = "resume"
        gateway.model = model
        message(s, m, 3, "发完了，开始回答", 30, group=group)
        worker.clock = lambda: NOW + 33
        running = asyncio.create_task(worker.once())
        await asyncio.wait_for(started.wait(), 5)
        message(s, m, 4, "更正，预算150元，只输出数字", 34, group=group)
        await asyncio.wait_for(running, 5)
        assert not gateway.sends
        controls = len(gateway.control_calls)
        worker.clock = lambda: NOW + 37
        await worker.once()
        released = s.query("SELECT t.state FROM tasks t JOIN reply_input_waits w ON w.release_task_id=t.id")
        assert released[0]["state"] == "accepted", released
        assert len(gateway.sends) == 1 and gateway.sends[0][-1] == "150"
        assert len(gateway.control_calls) == controls
        current = [v for v in contexts[-1] if v.get("current")]
        assert len(current) == 4
        assert len([v for v in current if v.get("input_scope") == "deferred"]) == 2
    finally:
        worker.release()


@pytest.mark.parametrize("mode", ["cancel", "replace"])
async def test_cancel_or_replace_does_not_consume_previous_material(tmp_path, mode):
    s, m, _, _, gateway, worker = await begin(tmp_path)
    try:
        message(s, m, 2, "旧材料秘密数字 1234", 10)
        worker.clock = lambda: NOW + 13
        await worker.once()
        gateway.control = mode
        message(s, m, 3, "取消刚才的任务" if mode == "cancel" else "不总结了，改为解释光合作用", 20)
        worker.clock = lambda: NOW + 23
        await worker.once()
        assert len(gateway.sends) == (0 if mode == "cancel" else 1)
        if mode == "replace":
            assert not any("1234" in v["content"] for v in gateway.contexts[-1])
        else:
            assert latest(s)["reason"] == "reply_wait_canceled"
    finally:
        worker.release()


async def test_wait_expiry_and_takeover_are_silent_and_never_reset_deadline(tmp_path):
    s, m, a, c, gateway, worker = await begin(tmp_path)
    try:
        row = s.query("SELECT * FROM reply_input_waits")[0]
        with s.transaction() as conn:
            assert (
                input_waits.active(
                    m,
                    conn,
                    m.account(a["id"], conn),
                    m.conversation(c["id"], conn),
                    "friend",
                    row["expires_at"],
                )
                is None
            )
        assert s.query("SELECT closed_reason FROM reply_input_waits")[0]["closed_reason"] == "wait_expired"
        assert not gateway.sends
        with s.transaction() as conn:
            conn.execute("UPDATE reply_input_waits SET state='waiting',closed_reason=''")
            m.takeover(c["id"], conn, NOW + 40)
        assert s.query("SELECT closed_reason FROM reply_input_waits")[0]["closed_reason"] == "human_takeover"
    finally:
        worker.release()


async def test_group_wait_does_not_authorize_another_member(tmp_path):
    s, m, _, _, gateway, worker = await begin(tmp_path, group=True)
    try:
        deliver(
            s,
            m,
            2,
            peer="group@chatroom",
            Content={"string": "other:\n发完了"},
            received=NOW + 20,
            created=NOW + 20,
        )
        assert latest(s)["reason"] == "not_explicitly_mentioned"
        worker.clock = lambda: NOW + 23
        await worker.once()
        assert not gateway.control_calls and not gateway.sends
        assert s.query("SELECT state FROM reply_input_waits")[0]["state"] == "waiting"
    finally:
        worker.release()


@pytest.mark.parametrize("scope,kind", [("deferred", "text"), ("pending", "text"), ("current", "image")])
def test_wait_evidence_cannot_come_from_material_or_old_instructions(scope, kind):
    value = {
        **decision("wait", "", "awaiting_input", ids=[1]),
        "wait_evidence": {"message_id": 1, "quote": "等我发完"},
    }
    context = [
        {
            "current": True,
            "message_id": 1,
            "sender": "friend",
            "reply_target": "friend",
            "input_scope": scope,
            "message_kind": kind,
            "content": "等我发完",
        }
    ]
    with pytest.raises(ProviderError, match="invalid_reply_decision"):
        reply_decisions.validate(value, context)


def test_wait_control_requires_literal_current_text():
    assert input_waits.validate_control({"action": "continue"}, []) == {
        "action": "continue",
        "message_id": 0,
        "quote": "",
    }
    with pytest.raises(ProviderError, match="invalid_wait_control"):
        input_waits.validate_control({"action": "resume"}, [])
    with pytest.raises(ProviderError, match="invalid_wait_control"):
        input_waits.validate_control(
            {"action": "resume", "message_id": 1, "quote": "发完了"},
            [{"message_id": 1, "message_kind": "image", "content": "发完了"}],
        )


def test_voice_wait_evidence_uses_transcript_without_generated_prefix():
    current = {
        "current": True,
        "message_id": 1,
        "sender": "friend",
        "reply_target": "friend",
        "message_kind": "voice",
        "content": "[语音转写] 等我发完",
    }
    value = {
        **decision("wait", "", "awaiting_input", ids=[1]),
        "wait_evidence": {"message_id": 1, "quote": "等我发完"},
    }
    assert reply_decisions.validate(value, [current]) == value
    value["wait_evidence"]["quote"] = "[语音转写]"
    with pytest.raises(ProviderError, match="invalid_reply_decision"):
        reply_decisions.validate(value, [current])


@pytest.mark.parametrize("target", [1, 2])
async def test_withdraw_wait_source_or_material(tmp_path, target):
    from test_dialogue_windows import revoke

    s, m, _, _, gateway, worker = await begin(tmp_path)
    try:
        message(s, m, 2, "材料：预算 120 元", 20)
        worker.clock = lambda: NOW + 23
        await worker.once()
        revoke(s, m, 30, target, peer="friend")
        gateway.control = "resume"
        message(s, m, 3, "发完了，请开始", 40)
        worker.clock = lambda: NOW + 43
        await worker.once()
        actual = [v for v in gateway.contexts[-1] if v.get("current")]
        assert not any("120" in v["content"] for v in actual)
        if target == 1:
            assert not any(v.get("input_scope") == "deferred" for v in actual)
            assert (
                s.query("SELECT closed_reason FROM reply_input_waits")[0]["closed_reason"]
                == "wait_source_revoked"
            )
    finally:
        worker.release()


@pytest.mark.parametrize("phase,expected", [("hold_generation", "accepted"), ("hold_send", "unknown")])
async def test_two_process_wait_release_crash_takeover_once(tmp_path, phase, expected):
    s, m, _, _, gateway, worker = await begin(tmp_path)
    worker.release()
    message(s, m, 2, "发完了，现在总结", 20)
    actor = Path(__file__).with_name("wait_process_actor.py")
    env = {**os.environ, "ACTOR_DATABASE_URL": s.url}
    first = subprocess.Popen(
        [sys.executable, str(actor), phase], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        line = await asyncio.wait_for(asyncio.to_thread(first.stdout.readline), 20)
        assert line, (await asyncio.to_thread(first.communicate, timeout=10))[1].decode(errors="replace")
        signal = json.loads(line)
        assert signal["phase"] == ("bound" if phase == "hold_generation" else "dispatch")
        if phase == "hold_generation":
            assert len(signal["ids"]) == 2
        first.kill()
        await asyncio.to_thread(first.communicate, timeout=10)
        s.query("UPDATE worker_lock SET expires_at=%s RETURNING id", (NOW,))
        recovered = await asyncio.to_thread(
            subprocess.run, [sys.executable, str(actor), "resume"], env=env, capture_output=True, timeout=30
        )
        assert recovered.returncode == 0, recovered.stderr
        assert latest(s)["state"] == expected, latest(s)
        assert len(s.query("SELECT * FROM audit WHERE action='wait_actor_send'")) == 1
        assert not s.query("SELECT * FROM reply_resource_slots")
        row = s.query("SELECT * FROM reply_input_waits")[0]
        assert row["release_task_id"] == latest(s)["id"]
        assert json.loads(latest(s)["usage_json"])["total_tokens"] == 22
        assert "quote" not in json.loads(row["control"]) and "quote_sha256" in json.loads(row["control"])
        with s.transaction() as conn:
            assert (
                input_waits.active(
                    m,
                    conn,
                    m.account(row["account_id"], conn),
                    m.conversation(row["conversation_id"], conn),
                    "friend",
                    NOW + 31,
                )
                is None
            )
    finally:
        if first.poll() is None:
            first.kill()
            await asyncio.to_thread(first.communicate)


async def test_pure_image_collects_without_model_or_download(tmp_path):
    s, m, _, _, gateway, worker = await begin(tmp_path)
    try:
        deliver(
            s, m, 2, MsgType=3, Content={"string": "<msg><img/></msg>"}, created=NOW + 20, received=NOW + 20
        )
        worker.clock = lambda: NOW + 26
        await worker.once()
        assert latest(s)["reason"] == "reply_wait_collecting"
        assert not gateway.control_calls and not gateway.sends
        assert not s.query("SELECT * FROM incoming_media")
    finally:
        worker.release()


async def test_wait_records_are_tenant_scoped(tmp_path):
    from wechat_manager.tenancy import user_scope

    s, _, _, _, _, worker = await begin(tmp_path)
    try:
        with user_scope(2):
            assert not s.query("SELECT * FROM reply_input_waits")
        assert len(s.query("SELECT * FROM reply_input_waits")) == 1
    finally:
        worker.release()
