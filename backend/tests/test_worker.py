import asyncio
import hashlib
import json

import pytest
from business_fixtures import allow_reply
from test_automation import NOW, deliver, setup
from wechat_manager.worker import Worker


class Gateway:
    def __init__(self):
        self.sends = []
        self.uncertain = False
        self.finish = "stop"

    async def model(self, config, prompt, context):
        return {
            "choices": [{"finish_reason": self.finish, "message": {"content": "收到"}}],
            "usage": {"total_tokens": 12},
        }

    async def model_with_tools(self, config, prompt, context, **runtime):
        # 队列/发送用例的模型替身输出新决策协议；原生协议另由test_reply_strategy验收。
        result = await self.model(config, prompt, context)
        actual = [
            v["message_id"]
            for v in context
            if v.get("current") and v.get("message_kind") != "pat" and v.get("input_scope") != "pending"
        ]
        selected = actual or [
            v["message_id"] for v in context if v.get("current") and v.get("input_scope") == "pending"
        ]
        if "reply_decision" not in result:
            choice = (result.get("choices") or [{}])[0]
            result["reply_decision"] = {
                "action": "reply",
                "content": choice.get("message", {}).get("content", ""),
                "reason": "answer" if selected else "presence",
                "close_dialogue": False,
            }
        result["reply_decision"]["handled_message_ids"] = selected
        return result

    async def online(self, app_id):
        return True

    async def close(self):
        pass

    async def send(self, app_id, peer, content):
        self.sends.append((app_id, peer, content))
        if self.uncertain:
            raise TimeoutError("response lost")
        return {"ret": 200, "data": {"newMsgId": 8895067626106766584}}


async def test_prompt_save_during_generation_cancels_old_reply_without_prompt_versions(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    deliver(store, manager, 1)
    gateway = Gateway()
    original = gateway.model

    async def change_prompt(*args):
        manager.configure_automation(account["id"], "仅使用新的提示词", True)
        return await original(*args)

    gateway.model = change_prompt
    worker = Worker(manager, gateway, clock=lambda: NOW + 10)
    await worker.once()
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "canceled"
    )
    assert not gateway.sends
    with store.transaction() as conn:
        conn.execute(
            "UPDATE contact_monitor SET epoch=%s WHERE account_id=%s",
            (manager.account(account["id"], conn)["epoch"], account["id"]),
        )
    deliver(store, manager, 2, created=NOW + 6)

    async def current_prompt(config, prompt, context):
        assert "仅使用新的提示词" in prompt
        return await original(config, prompt, context)

    gateway.model = current_prompt
    worker.clock = lambda: NOW + 12
    await worker.once()
    latest = store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]
    assert latest["state"] == "accepted" and len(gateway.sends) == 1
    trace = json.loads(latest["trace_json"])
    assert "prompt_version" not in trace
    assert trace["account_prompt_sha256"] == hashlib.sha256("仅使用新的提示词".encode()).hexdigest()
    worker.release()


async def test_response_loss_is_unknown_and_restart_does_not_resend(tmp_path):
    store, manager, a, c = setup(tmp_path)
    deliver(store, manager, 1)
    gateway = Gateway()
    gateway.uncertain = True
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    await worker.once()
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "unknown"
    )
    assert manager.account(a["id"])["last_successful_refresh_at"] == NOW + 5
    worker.release()
    restarted = Worker(manager, gateway, clock=lambda: NOW + 10)
    await restarted.once()
    assert len(gateway.sends) == 1
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "unknown"
    )
    restarted.release()


@pytest.mark.parametrize("content,finish", [("", "stop"), ("   ", "stop"), ("拒绝", "content_filter")])
async def test_empty_or_abnormal_model_completion_never_sends(tmp_path, content, finish):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    g = Gateway()

    async def invalid(*args):
        return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}

    g.model = invalid
    w = Worker(m, g, clock=lambda: NOW + 5)
    await w.once()
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"] == "failed"
        and not g.sends
    )
    w.release()


async def test_same_account_merges_continuous_messages_and_advances_next_conversation(tmp_path):
    s, m, a, conv = setup(tmp_path)
    other = m.ensure_conversation(a["id"], "friend-two")
    allow_reply(m, other["id"], True)
    deliver(s, m, 1)
    deliver(s, m, 2)
    deliver(s, m, 3, peer="friend-two")
    g = Gateway()
    contexts = []
    original = g.model

    async def capture(config, prompt, context):
        contexts.append(context)
        return await original(config, prompt, context)

    g.model = capture
    w = Worker(m, g, clock=lambda: NOW + 5)
    await w.once()
    assert len(g.sends) == len(contexts) == 2
    assert sorted(sum(bool(item.get("current")) for item in ctx) for ctx in contexts) == [1, 2]
    await w.once()
    assert len(g.sends) == 2
    assert len(contexts) == 2
    w.release()


async def test_truncated_answer_never_sends(tmp_path):
    store, manager, a, c = setup(tmp_path)
    deliver(store, manager, 1)
    gateway = Gateway()
    gateway.finish = "length"
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    await worker.once()
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "failed"
    )
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["reason"]
        == "invalid_model_output"
    )
    assert gateway.sends == []
    worker.release()


async def test_worker_shutdown_releases_persisted_lease(tmp_path):
    s, m, _, _ = setup(tmp_path)
    w = Worker(m, Gateway(), clock=lambda: NOW + 5)
    task = asyncio.create_task(w.run())
    await asyncio.sleep(0.05)
    assert s.query("SELECT * FROM worker_lock")
    task.cancel()
    await task
    assert s.query("SELECT * FROM worker_lock") == []


async def test_phone_takeover_arriving_during_generation_cancels_auto_before_send(tmp_path):
    store, manager, a, c = setup(tmp_path)
    deliver(store, manager, 1)
    gateway = Gateway()
    original = gateway.model

    async def with_phone(config, prompt, context):
        raw = {
            "TypeName": "AddMsg",
            "Appid": "device-a",
            "Wxid": "wxid_me",
            "Data": {
                "NewMsgId": 2,
                "MsgType": 1,
                "FromUserName": {"string": "wxid_me"},
                "ToUserName": {"string": "friend"},
                "Content": {"string": "我来处理"},
                "CreateTime": NOW + 4,
            },
        }
        store.receive(json.dumps(raw).encode(), now=NOW + 5)
        return await original(config, prompt, context)

    gateway.model = with_phone
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    await worker.once()
    assert gateway.sends == []
    assert (
        next(
            t
            for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["message_id"] == 1
        )["state"]
        == "canceled"
    )
    assert manager.conversation(c["id"])["pause_until"] > NOW + 1700
    worker.release()


async def test_message_expiring_during_generation_is_not_sent(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    now = [NOW + 5]
    g = Gateway()
    original = g.model

    async def slow(*args):
        now[0] = NOW + 305
        return await original(*args)

    g.model = slow
    w = Worker(m, g, clock=lambda: now[0])
    await w.once()
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["reason"]
        == "expired"
        and not g.sends
    )
    w.release()


@pytest.mark.parametrize("elapsed,expected", [(5, "accepted"), (305, "canceled")])
async def test_restart_recovers_only_still_valid_generation(tmp_path, elapsed, expected):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    with s.connect() as c:
        c.execute("UPDATE tasks SET state='generating',lease_owner='dead-worker'")
    g = Gateway()
    w = Worker(m, g, clock=lambda: NOW + elapsed)
    await w.once()
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"] == expected
    )
    assert len(g.sends) == (1 if expected == "accepted" else 0)
    w.release()


async def test_retired_task_kind_never_calls_model_or_sends(tmp_path):
    s, m, account, conv = setup(tmp_path)
    baseline = m.conversation(conv["id"])
    with s.connect() as conn:
        task = m.create_task(conn, "preview", account, conv, content="旧测试")
    g = Gateway()

    async def forbidden(*args):
        raise AssertionError("旧测试不得调用模型")

    g.generate = forbidden
    w = Worker(m, g, clock=lambda: NOW + 5)
    await w.once()
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["id"] == task["id"]
        and s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "canceled"
    )
    assert not g.sends and m.conversation(conv["id"]) == baseline
    w.release()


@pytest.mark.parametrize("mode", ["exception", "tool_call"])
async def test_model_exception_or_tool_request_has_no_platform_side_effect(tmp_path, mode):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    g = Gateway()

    async def invalid(*args):
        if mode == "exception":
            raise TimeoutError("isolated model timeout")
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": "调用工具", "tool_calls": [{}]}}]
        }

    g.model = invalid
    w = Worker(m, g, clock=lambda: NOW + 5)
    await w.once()
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"] == "failed"
        and not g.sends
    )
    w.release()
