import asyncio
import json

import pytest
from business_fixtures import allow_reply
from qingda.worker import Worker
from test_automation import NOW, deliver, setup
from test_worker import Gateway


async def test_private_image_waits_for_followup_without_claiming_worker_slot(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img/></msg>"})
    worker = Worker(manager, Gateway(), clock=lambda: NOW + 3)
    try:
        assert await worker.once() == 0
        assert (
            store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
            == "queued"
        )
    finally:
        worker.release()


async def test_followup_replaces_unsent_generation_and_uses_one_reply(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, Content={"string": "原编号A"})
    now = [NOW + 5]
    gateway = Gateway()
    started = asyncio.Event()
    contexts = []

    async def model(config, prompt, context):
        contexts.append(context)
        if len(contexts) == 1:
            started.set()
            await asyncio.Event().wait()
        return {"choices": [{"finish_reason": "stop", "message": {"content": "B"}}]}

    gateway.model = model
    worker = Worker(manager, gateway, clock=lambda: now[0])
    try:
        running = asyncio.create_task(worker.once())
        await asyncio.wait_for(started.wait(), 3)
        now[0] = NOW + 6
        deliver(store, manager, 2, received=now[0], Content={"string": "更正为B，只输出编号"})
        await asyncio.wait_for(running, 3)
        assert gateway.sends == []
        now[0] = NOW + 9
        await worker.once()
        assert len(gateway.sends) == 1
        assert [v["content"] for v in contexts[-1] if v.get("current")] == ["原编号A", "更正为B，只输出编号"]
        assert gateway.sends[0][-1] == "B"
    finally:
        worker.release()


async def test_collection_has_thirty_second_cap_and_duplicate_does_not_extend_it(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    for mid, at in enumerate((2, 10, 20, 31), 1):
        deliver(store, manager, mid, received=NOW + at)
    deliver(store, manager, 4, received=NOW + 31)
    gateway = Gateway()
    now = [NOW + 31]
    worker = Worker(manager, gateway, clock=lambda: now[0])
    try:
        assert await worker.once() == 0
        now[0] = NOW + 32
        assert await worker.once() == 1
        assert len(gateway.sends) == 1
        assert (
            len(
                [
                    t
                    for t in store.query(
                        "SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100"
                    )
                    if t["state"] == "merged"
                ]
            )
            == 3
        )
    finally:
        worker.release()


async def test_waiting_image_does_not_block_other_private_chat_and_restart_keeps_deadline(tmp_path):
    store, manager, account, _ = setup(tmp_path)
    other = manager.ensure_conversation(account["id"], "other")
    allow_reply(manager, other["id"], True)
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img/></msg>"})
    deliver(store, manager, 2, peer="other")
    gateway = Gateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    try:
        assert await worker.once() == 1
        assert gateway.sends[0][1] == "other"
    finally:
        worker.release()
    restarted = Worker(manager, gateway, clock=lambda: NOW + 6)
    try:
        assert await restarted.once() == 0
    finally:
        restarted.release()


async def test_private_context_normalizes_builtin_emotions_and_preserves_unknown_codes(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, Content={"string": "[Smile] /:strong [未知123]"})
    context = manager.context(
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]
    )
    assert context[-1]["content"] == "[微笑] [强] [未知123]"
    assert context[-1]["original_content"] == "[Smile] /:strong [未知123]"


async def test_missing_media_keeps_text_and_marks_failure_in_model_input(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img/></msg>"})
    deliver(store, manager, 2, received=NOW + 6, Content={"string": "帮我看这张图片的编号"})

    class BrokenMedia:
        def image_allowed(self, *args):
            return True

        async def prepare(self, *args):
            from qingda.providers import ProviderError

            raise ProviderError("platform_download_rejected")

        def record(self, mid, conn=None):
            return (
                {"state": "failed", "reason": "platform_download_rejected", "trace_json": "{}"}
                if mid == 1
                else None
            )

        def context_item(self, row, conn=None):
            return {"role": "user", "content": row["text"] or "[图片]"}

    manager.media = BrokenMedia()
    manager.media.manager = manager
    manager.media.store = store
    gateway = Gateway()
    contexts = []

    async def model(config, prompt, context):
        contexts.append(context)
        return {
            "choices": [
                {"finish_reason": "stop", "message": {"content": "图片没能读取，可以补充一下编号吗？"}}
            ]
        }

    gateway.model = model
    worker = Worker(manager, gateway, clock=lambda: NOW + 10)
    try:
        await worker.once()
        current = [v for v in contexts[0] if v.get("current")]
        assert current[0]["media_status"] == "download_failed"
        assert current[0]["media_reason"] == "platform_download_rejected"
        assert current[1]["content"] == "帮我看这张图片的编号"
        assert len(gateway.sends) == 1
    finally:
        worker.release()


async def test_group_uses_its_own_short_collection_window(tmp_path):
    store, manager, account, _ = setup(tmp_path)
    group = manager.ensure_conversation(account["id"], "g@chatroom")
    allow_reply(manager, group["id"], True)
    deliver(
        store,
        manager,
        1,
        peer="g@chatroom",
        Content={"string": "member:\n你好"},
        source="<msgsource><atuserlist>wxid_me</atuserlist></msgsource>",
    )
    gateway = Gateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 2)
    try:
        assert await worker.once() == 0
        worker.clock = lambda: NOW + 4
        assert await worker.once() == 1
        assert len(gateway.sends) == 1
    finally:
        worker.release()


async def test_history_retains_more_than_ten_complete_rounds_with_real_send_times(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    gateway = Gateway()
    contexts = []
    original = gateway.model

    async def model(config, prompt, context):
        contexts.append(context)
        return await original(config, prompt, context)

    gateway.model = model
    for number in range(13):
        at = NOW + 2 + number * 10
        deliver(store, manager, number + 1, received=at, Content={"string": f"问题{number}"})
        worker = Worker(manager, gateway, clock=lambda: at + 3)
        try:
            await worker.once()
        finally:
            worker.release()
    history = [value for value in contexts[-1] if not value.get("current")]
    assert len(history) == 24
    assert [v["content"] for v in history if v["role"] == "user"] == [f"问题{n}" for n in range(12)]
    assert [v["role"] for v in history] == ["user", "assistant"] * 12
    assert history[1]["timestamp"] == NOW + 5


async def test_history_budget_removes_whole_old_round_but_not_current_input(tmp_path, monkeypatch):
    monkeypatch.setattr("qingda.context_budget.PRIVATE", (100, 400, 1000))
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, Content={"string": "甲" * 12001})
    worker = Worker(manager, Gateway(), clock=lambda: NOW + 5)
    try:
        await worker.once()
    finally:
        worker.release()
    deliver(store, manager, 2, received=NOW + 10, Content={"string": "当前问题"})
    task = next(
        t
        for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
        if t["state"] == "queued"
    )
    context = manager.context(task)
    assert len(context) == 1 and context[0]["content"] == "当前问题" and context[0]["current"]


async def test_explicit_reference_is_scoped_and_recovers_original_text(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, Content={"string": "原始编号Q123"})
    worker = Worker(manager, Gateway(), clock=lambda: NOW + 5)
    try:
        await worker.once()
    finally:
        worker.release()
    deliver(
        store,
        manager,
        2,
        received=NOW + 10,
        MsgType=49,
        Content={
            "string": "<msg><appmsg><type>57</type><title>这个编号是什么</title><refermsg><svrid>1</svrid></refermsg></appmsg></msg>"
        },
    )
    task = next(
        t
        for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
        if t["state"] == "queued"
    )
    assert manager.context(task)[-1]["reference"]["content"] == "原始编号Q123"


async def test_followup_during_online_check_invalidates_ready_reply(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    gateway = Gateway()
    checked = False

    async def online(app_id):
        nonlocal checked
        if not checked:
            checked = True
            deliver(store, manager, 2, received=NOW + 6, Content={"string": "这是发送前的更正"})
        return True

    gateway.online = online
    now = [NOW + 5]
    worker = Worker(manager, gateway, clock=lambda: now[0])
    try:
        await worker.once()
        assert not gateway.sends
        now[0] = NOW + 9
        await worker.once()
        assert len(gateway.sends) == 1
        assert (
            len(
                [
                    t
                    for t in store.query(
                        "SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100"
                    )
                    if t["state"] == "accepted"
                ]
            )
            == 1
        )
    finally:
        worker.release()


async def test_arrival_after_dispatch_boundary_is_a_new_round(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    gateway = Gateway()
    original = gateway.send

    async def send(*args):
        if not gateway.sends:
            deliver(store, manager, 2, received=NOW + 6)
        return await original(*args)

    gateway.send = send
    now = [NOW + 5]
    worker = Worker(manager, gateway, clock=lambda: now[0])
    try:
        await worker.once()
        now[0] = NOW + 9
        await worker.once()
        assert len(gateway.sends) == 2
        assert all(
            t["state"] == "accepted"
            for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
        )
    finally:
        worker.release()


def test_released_emotions_load_backend_resource_without_frontend_source(tmp_path, monkeypatch):
    from qingda import private_context

    runtime = tmp_path / "backend/qingda/native_emotions.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(json.dumps({"[Strong]": "[强]"}), encoding="utf-8")
    (tmp_path / "release.json").write_text("{}")
    monkeypatch.setattr(private_context, "ROOT", tmp_path)
    private_context.emotions.cache_clear()
    try:
        assert private_context.normalize_emotions("收到[Strong]") == "收到[强]"
    finally:
        private_context.emotions.cache_clear()


def test_released_emotions_require_packaged_resource(tmp_path, monkeypatch):
    from qingda import private_context
    from qingda.providers import ProviderError

    (tmp_path / "release.json").write_text("{}")
    monkeypatch.setattr(private_context, "ROOT", tmp_path)
    private_context.emotions.cache_clear()
    try:
        with pytest.raises(ProviderError, match="missing_native_emotions"):
            private_context.emotions()
    finally:
        private_context.emotions.cache_clear()
