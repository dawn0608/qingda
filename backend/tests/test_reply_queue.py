import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from business_fixtures import allow_reply, phone_takeover
from test_automation import NOW, deliver, setup, verified_fixture
from test_worker import Gateway
from wechat_manager.providers import Gateway as RealGateway
from wechat_manager.worker import Worker


async def test_account_allows_generation_across_conversations_but_serializes_sends(tmp_path):
    store, manager, account, _ = setup(tmp_path)
    other = manager.ensure_conversation(account["id"], "friend-two")
    allow_reply(manager, other["id"], True)
    deliver(store, manager, 1)
    deliver(store, manager, 2, peer="friend-two")
    gateway = Gateway()
    original = gateway.model
    active = 0
    peak = 0
    both_started = asyncio.Event()

    async def model(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 5)
        result = await original(*args)
        active -= 1
        return result

    gateway.model = model
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    try:
        await worker.once()
        assert peak == 2
        assert len(gateway.sends) == 2
        assert (
            sum(
                t["state"] == "queued"
                for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            )
            == 0
        )
    finally:
        worker.release()


async def test_ready_order_and_send_end_gap_with_parallel_generation(tmp_path):
    store, manager, account, _ = setup(tmp_path)
    other = manager.ensure_conversation(account["id"], "friend-two")
    allow_reply(manager, other["id"], True)
    deliver(store, manager, 1)
    deliver(store, manager, 2, peer="friend-two")
    gateway = Gateway()
    original_model, original_send = gateway.model, gateway.send
    first_send = asyncio.Event()
    both_models = asyncio.Event()
    model_active = model_peak = send_active = send_peak = 0
    calls = []
    started = time.monotonic()

    def clock():
        return NOW + 5 + time.monotonic() - started

    async def model(config, prompt, context):
        nonlocal model_active, model_peak
        assert "concurrency" not in config
        model_active += 1
        model_peak = max(model_peak, model_active)
        if model_active == 2:
            both_models.set()
        try:
            await asyncio.wait_for(both_models.wait(), 5)
            if any(v.get("current") and v.get("sender") == "friend" for v in context):
                await asyncio.wait_for(first_send.wait(), 5)
            return await original_model(config, prompt, context)
        finally:
            model_active -= 1

    async def send(app_id, peer, content):
        nonlocal send_active, send_peak
        send_active += 1
        send_peak = max(send_peak, send_active)
        call = {"peer": peer, "start": time.monotonic()}
        calls.append(call)
        first_send.set()
        try:
            await asyncio.sleep(0.08)
            return await original_send(app_id, peer, content)
        finally:
            call["end"] = time.monotonic()
            send_active -= 1

    gateway.model, gateway.send = model, send
    worker = Worker(manager, gateway, clock=clock)
    try:
        await asyncio.wait_for(worker.once(), 10)
        assert model_peak == 2
        assert send_peak == 1
        assert [c["peer"] for c in calls] == ["friend-two", "friend"]
        assert calls[1]["start"] - calls[0]["end"] >= 1
        assert all(row["state"] == "accepted" for row in store.query("SELECT state FROM tasks"))
    finally:
        worker.release()


async def test_continuous_messages_generate_and_send_one_reply(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, Content={"string": "明天一起吃饭吗"})
    deliver(store, manager, 2, Content={"string": "中午十二点"})
    gateway = Gateway()
    contexts = []
    original = gateway.model

    async def model(config, prompt, context):
        contexts.append(context)
        return await original(config, prompt, context)

    gateway.model = model
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    try:
        await worker.once()
        assert [
            t["state"]
            for t in sorted(
                store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100"),
                key=lambda t: t["message_id"],
            )
        ] == [
            "accepted",
            "merged",
        ]
        assert [item["content"] for item in contexts[0] if item.get("current")] == [
            "明天一起吃饭吗",
            "中午十二点",
        ]
        await worker.once()
        assert len(gateway.sends) == len(contexts) == 1
    finally:
        worker.release()


def test_new_process_obeys_persisted_send_end_gap(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    actor = Path(__file__).with_name("serial_send_actor.py")
    env = {
        **os.environ,
        "ACTOR_DATABASE_URL": store.url,
        "ACTOR_REAL_ORIGIN": str(time.time()),
    }
    for mid in (1, 2):
        deliver(store, manager, mid)
        result = subprocess.run(
            [sys.executable, str(actor)], env=env, capture_output=True, text=True, timeout=20
        )
        assert result.returncode == 0, result.stderr
    starts = [
        json.loads(row["detail"])["time"]
        for row in store.query(
            "SELECT detail FROM audit WHERE action='isolated_serial_send_start' ORDER BY id"
        )
    ]
    ends = [
        json.loads(row["detail"])["time"]
        for row in store.query("SELECT detail FROM audit WHERE action='isolated_serial_send_end' ORDER BY id")
    ]
    assert len(starts) == len(ends) == 2
    assert starts[1] - ends[0] >= 1
    assert not store.query("SELECT * FROM worker_lock")


@pytest.mark.parametrize("change", ["takeover", "disable"])
async def test_prepared_answer_rechecks_permission_while_waiting_to_send(tmp_path, change):
    store, manager, account, _ = setup(tmp_path)
    other = manager.ensure_conversation(account["id"], "friend-two")
    allow_reply(manager, other["id"], True)
    deliver(store, manager, 1)
    deliver(store, manager, 2, peer="friend-two")
    gateway = Gateway()
    sending, finish = asyncio.Event(), asyncio.Event()
    original, original_model = gateway.send, gateway.model

    async def model(config, prompt, context):
        if any(v.get("current") and v.get("sender") == "friend-two" for v in context):
            await asyncio.wait_for(sending.wait(), 5)
        return await original_model(config, prompt, context)

    async def send(app_id, peer, content):
        if peer == "friend":
            sending.set()
            await finish.wait()
        return await original(app_id, peer, content)

    gateway.model, gateway.send = model, send
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    running = asyncio.create_task(worker.once())
    try:
        await asyncio.wait_for(sending.wait(), 5)
        for _ in range(100):
            rows = store.query("SELECT state FROM tasks WHERE conversation_id=%s", (other["id"],))
            if rows and rows[0]["state"] == "ready":
                break
            await asyncio.sleep(0.02)
        assert rows[0]["state"] == "ready"
        if change == "takeover":
            phone_takeover(manager, other["id"], "人工接管", "00000000-0000-4000-8000-000000000019")
        else:
            manager.set_auto_reply(other["id"], False)
        finish.set()
        await asyncio.wait_for(running, 10)
        assert [v[1] for v in gateway.sends] == ["friend"]
        assert (
            store.query("SELECT state FROM tasks WHERE conversation_id=%s", (other["id"],))[0]["state"]
            == "canceled"
        )
    finally:
        finish.set()
        await asyncio.gather(running, return_exceptions=True)
        worker.release()


async def test_model_wire_keeps_all_current_messages_separate_from_history(tmp_path):
    _, manager, _, _ = setup(tmp_path)
    gateway = RealGateway(manager)
    try:
        body = json.loads(
            await gateway.model_input(
                [
                    {"role": "assistant", "content": "历史回答"},
                    {
                        "role": "user",
                        "content": "第一条",
                        "current": True,
                        "message_id": 1,
                        "sender": "friend",
                    },
                    {
                        "role": "user",
                        "content": "第二条",
                        "current": True,
                        "message_id": 2,
                        "sender": "friend",
                    },
                ]
            )
        )
        assert [m["content"] for m in body["current_messages"]] == ["第一条", "第二条"]
        assert body["conversation_history"] == [{"role": "assistant", "content": "历史回答"}]
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "boundary", ["conversation", "sender", "unmentioned_sender", "unknown_sender", "manual"]
)
async def test_batch_never_crosses_a_real_boundary(tmp_path, boundary):
    store, manager, account, conv = setup(tmp_path)
    source = "<msgsource><atuserlist>wxid_me</atuserlist></msgsource>"
    if boundary in ("sender", "unmentioned_sender", "unknown_sender"):
        conv = manager.ensure_conversation(account["id"], "group@chatroom")
        allow_reply(manager, conv["id"], True)

        def message(mid, sender, mentioned=True):
            body = (sender + ":\n" if sender else "") + "群消息"
            deliver(
                store,
                manager,
                mid,
                peer="group@chatroom",
                Content={"string": body},
                source=source if mentioned else "",
            )

        message(1, "member-a" if boundary != "unknown_sender" else None)
        message(2, "member-b" if boundary != "unknown_sender" else None, boundary != "unmentioned_sender")
        message(3, "member-a")
    else:
        deliver(store, manager, 1)
        other = manager.ensure_conversation(account["id"], "friend-two")
        allow_reply(manager, other["id"], True)
        if boundary == "manual":
            phone_takeover(manager, other["id"], "人工消息", "00000000-0000-4000-8000-000000000003")
        else:
            deliver(store, manager, 2, peer="friend-two")
        deliver(store, manager, 3)
    gateway = Gateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    try:
        await worker.once()
        assert len(gateway.sends) == (2 if boundary == "conversation" else 1)
        if boundary in ("conversation", "manual", "sender", "unmentioned_sender"):
            # 私聊按会话、群聊按成员收集；其他成员交错发言仅作背景。
            merged = [
                t
                for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
                if t["state"] == "merged"
            ]
            assert len(merged) == 1 and merged[0]["conversation_id"] == conv["id"]
            if boundary in ("sender", "unmentioned_sender"):
                current = json.loads(
                    store.query("SELECT context_json FROM tasks WHERE state='accepted'")[0]["context_json"]
                )
                assert {x["sender"] for x in current if x.get("current")} == {"member-a"}
        else:
            assert all(
                t["state"] != "merged"
                for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            )
    finally:
        worker.release()


async def test_private_arrivals_during_generation_replace_unsent_answer(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    deliver(store, manager, 2)
    gateway = Gateway()
    original = gateway.model
    calls = []

    async def model(config, prompt, context):
        calls.append([m["message_id"] for m in context if m.get("current")])
        if len(calls) == 1:
            deliver(store, manager, 3)
        return await original(config, prompt, context)

    gateway.model = model
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    try:
        await worker.once()
        await worker.once()
        assert calls == [[1, 2], [1, 2, 3]]
        assert len(gateway.sends) == 1
    finally:
        worker.release()


async def test_private_restart_resumes_round_with_new_unsent_inputs(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    deliver(store, manager, 2)
    gateway = Gateway()
    entered = asyncio.Event()

    async def model(*args):
        entered.set()
        await asyncio.Event().wait()

    gateway.model = model
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    running = asyncio.create_task(worker.once())
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    worker.release()
    deliver(store, manager, 3)
    gateway = Gateway()
    contexts = []
    original = gateway.model

    async def resumed(config, prompt, context):
        contexts.append([m["message_id"] for m in context if m.get("current")])
        return await original(config, prompt, context)

    gateway.model = resumed
    worker = Worker(manager, gateway, clock=lambda: NOW + 6)
    try:
        await worker.once()
        await worker.once()
        assert contexts == [[1, 2, 3]]
        assert len(gateway.sends) == 1
    finally:
        worker.release()


@pytest.mark.parametrize("change", ["takeover", "disable", "expire", "uncertain", "generation_failed"])
async def test_batch_outcomes_never_report_unsent_members_as_success(tmp_path, change):
    store, manager, account, conv = setup(tmp_path)
    deliver(store, manager, 1)
    deliver(store, manager, 2)
    gateway = Gateway()
    original = gateway.model
    now = [NOW + 5]

    async def model(*args):
        if change == "takeover":
            phone_takeover(manager, conv["id"], "接管", "00000000-0000-4000-8000-000000000004")
        elif change == "disable":
            manager.configure_automation(account["id"], ai_enabled=False)
        elif change == "expire":
            now[0] += 301
        elif change == "generation_failed":
            raise TimeoutError("model unavailable")
        return await original(*args)

    gateway.model = model
    gateway.uncertain = change == "uncertain"
    worker = Worker(manager, gateway, clock=lambda: now[0])
    try:
        await worker.once()
        records = [
            t
            for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["kind"] == "auto"
        ]
        leader = next(t for t in records if t["state"] != "merged")
        follower = next(t for t in records if t["state"] == "merged")
        assert leader["state"] == (
            "unknown" if change == "uncertain" else "failed" if change == "generation_failed" else "canceled"
        )
        assert (
            store.query("SELECT batch_id FROM reply_batch_items WHERE task_id=%s", (follower["id"],))[0][
                "batch_id"
            ]
            == leader["id"]
        )
        assert len(gateway.sends) == (1 if change == "uncertain" else 0)
    finally:
        worker.release()


async def test_capacity_splits_current_messages_without_dropping_or_merging_later_arrivals(
    tmp_path, monkeypatch
):
    # 人为缩小字节预算验证拆批，生产预算已独立扩容。
    monkeypatch.setattr("wechat_manager.reply_queue.TEXT_LIMIT", 100000)
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, Content={"string": "a" * 60000})
    deliver(store, manager, 2, Content={"string": "b" * 60000})
    gateway = Gateway()
    original = gateway.model
    calls = []

    async def model(config, prompt, context):
        calls.append([m["message_id"] for m in context if m.get("current")])
        if len(calls) == 1:
            deliver(store, manager, 3)
        return await original(config, prompt, context)

    gateway.model = model
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    try:
        for _ in range(3):
            await worker.once()
        assert calls == [[1], [2], [3]]
        assert len(gateway.sends) == 3
        assert all(
            t["state"] == "accepted"
            for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
        )
    finally:
        worker.release()


async def test_slow_account_does_not_hold_other_account_next_task(tmp_path):
    store, manager, _, conv = setup(tmp_path)
    second = manager.add_account("device-b")
    manager.verify_identity("device-b", {"wxid": "wxid_second"})
    other = manager.ensure_conversation(second["id"], "friend-other")
    verified_fixture(manager, other)
    manager.configure_automation(second["id"], prompt="简洁回复", ai_enabled=True)
    third = manager.ensure_conversation(second["id"], "friend-third")
    for target in [other, third]:
        allow_reply(manager, target["id"], True)
        verified_fixture(manager, target)
    for index, target in enumerate([conv, other, third]):
        account = manager.account(target["account_id"])
        value = {
            "TypeName": "AddMsg",
            "Appid": account["current_app_id"],
            "Wxid": account["wxid"],
            "Data": {
                "NewMsgId": str(100 + index),
                "MsgType": 1,
                "FromUserName": {"string": target["peer_id"]},
                "ToUserName": {"string": account["wxid"]},
                "Content": {"string": "测试问题"},
                "CreateTime": NOW + 1,
            },
        }
        store.receive(json.dumps(value).encode(), now=NOW + 2)
    manager.process_events(now=NOW + 2)
    gateway = Gateway()
    blocked = asyncio.Event()
    sends = []

    async def send(app_id, peer, content):
        if app_id == "device-a":
            await blocked.wait()
        sends.append(app_id)
        return {"ret": 200}

    gateway.send = send
    worker = Worker(manager, gateway, clock=lambda: NOW + 10)
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and len(sends) < 2:
            await worker.once(background=True)
            await asyncio.sleep(0.03)
        assert sends == ["device-b", "device-b"]
    finally:
        blocked.set()
        await asyncio.gather(*worker.running)
        worker.release()
