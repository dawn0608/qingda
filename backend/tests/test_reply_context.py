"""实际模型上下文的只读查看、隔离与缺失状态；仅服务器测试库。"""

import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from contact_fixtures import setup_directory
from PIL import Image
from test_chat_records import endpoint, incoming


def test_legacy_auto_reply_context_is_explicitly_unavailable(tmp_path):
    http, store, manager, aid, _ = setup_directory(tmp_path)
    message = incoming(store, manager, 98001)
    cid = message["conversation_id"]
    with store.transaction() as conn:
        conn.execute(
            "UPDATE message_records SET direction='outbound',source='auto' WHERE id=%s",
            (message["id"],),
        )
    result = http.get(endpoint(aid, cid) + f"/{message['id']}/context")
    assert result.status_code == 200
    assert result.json()["status"] == "unavailable"
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["missing"]
    assert http.get(endpoint(aid, cid + 1) + f"/{message['id']}/context").status_code == 404


def capture_fixture(tmp_path):
    from qingda.context_capture import Capture

    http, store, manager, aid, _ = setup_directory(tmp_path)
    message = incoming(store, manager, 98002)
    cid = message["conversation_id"]
    with store.transaction() as conn:
        tid = conn.execute("SELECT id FROM tasks WHERE message_id=%s", (message["id"],)).fetchone()["id"]
        conn.execute(
            "UPDATE tasks SET kind='auto',state='generating',content='实际答复',lease_owner='capture-test' WHERE id=%s",
            (tid,),
        )
    task = dict(store.query("SELECT * FROM tasks WHERE id=%s", (tid,))[0])
    task["_owner"] = "capture-test"
    return http, store, manager, aid, cid, task, Capture(manager, task)


@pytest.mark.asyncio
async def test_actual_request_is_frozen_deduplicated_and_read_only(tmp_path):
    from qingda.context_capture import CURRENT
    from qingda.media_references import record_outbound
    from qingda.model_settings import DEFAULT_MODEL
    from qingda.providers import Gateway

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "实际答复", "reasoning_content": "不应保存的思考"},
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )

    gateway = Gateway(manager, httpx.MockTransport(handler))
    # 仅替换密钥读取，实际请求组装、HTTP与快照记录均使用生产实现。
    gateway.secret = lambda name: "never-store-this-key"
    messages = [
        {"role": "system", "content": "当时的提示词"},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "current_messages": [{"content": "当时的问题"}],
                    "conversation_history": [{"content": "历史原文"}],
                    "history_coverage": {"has_more": True, "scope": "current_conversation"},
                },
                ensure_ascii=False,
            ),
        },
    ]
    token = CURRENT.set(capture)
    try:
        await gateway.chat(DEFAULT_MODEL, messages, [])
        await gateway.chat(DEFAULT_MODEL, messages, [])
    finally:
        CURRENT.reset(token)
    messages[0]["content"] = "后来修改的提示词"
    capture.tool(
        {
            "call_id": "finish-1",
            "name": "finish_reply",
            "arguments": {"content": "实际答复"},
            "result": {"ok": True},
        }
    )
    with store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state='accepted',trace_json=%s WHERE id=%s",
            (json.dumps({"context_capture": capture.manifest()}), task["id"]),
        )
        record_outbound(store, conn, task, {"data": {"newMsgId": "98003"}}, "accepted", manager.clock())
    await gateway.context_writer.drain()
    mid = store.query("SELECT id FROM message_records WHERE outbound_task_id=%s", (task["id"],))[0]["id"]
    path = endpoint(aid, cid) + f"/{mid}/context"
    response = http.get(path)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "complete", body
    assert body["input"]["system"] == ["当时的提示词"]
    assert body["input"]["data"]["current_messages"][0]["content"] == "当时的问题"
    calls = body["calls"]
    assert len(calls) == 3
    detail = http.get(path, params={"call_id": calls[0]["call_id"]}).json()
    assert detail["request"] == payloads[0]
    assert "never-store-this-key" not in response.text + json.dumps(detail)
    assert "不应保存的思考" not in json.dumps(detail, ensure_ascii=False)
    assert (
        len(
            store.query(
                "SELECT id FROM reply_execution_events WHERE phase='context_blob' AND task_id=%s",
                (task["id"],),
            )
        )
        == 3
    )
    assert http.get(path, params={"call_id": "other-task"}).status_code == 404
    assert http.get(endpoint(aid, cid) + f"/{mid - 1}/context").status_code == 404
    await gateway.close()


@pytest.mark.asyncio
async def test_background_failure_does_not_wait_or_claim_complete(tmp_path):
    from qingda.context_capture import CURRENT, Writer
    from qingda.model_settings import DEFAULT_MODEL
    from qingda.providers import Gateway

    _, store, manager, _, _, task, capture = capture_fixture(tmp_path)
    writer = Writer(manager.store)
    original = writer.write

    def broken(envelope):
        raise RuntimeError("private-database-detail")

    writer.write = broken
    gateway = Gateway(
        manager,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": "仍然回复"}, "finish_reason": "stop"}]}
            )
        ),
    )
    gateway.secret = lambda name: "key"
    gateway.context_writer = writer
    token = CURRENT.set(capture)
    try:
        result = await asyncio.wait_for(gateway.chat(DEFAULT_MODEL, [{"role": "user", "content": "你好"}]), 2)
    finally:
        CURRENT.reset(token)
    assert result["choices"][0]["message"]["content"] == "仍然回复"
    await writer.drain()
    assert capture.manifest()["missing"]
    assert not store.query("SELECT id FROM reply_execution_events WHERE task_id=%s", (task["id"],))
    writer.write = original
    await gateway.close()


def accepted_path(store, manager, aid, cid, task, capture):
    from qingda.media_references import record_outbound

    with store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state='accepted',trace_json=%s WHERE id=%s",
            (json.dumps({"context_capture": capture.manifest()}), task["id"]),
        )
        record_outbound(store, conn, task, {"data": {"newMsgId": "98004"}}, "accepted", manager.clock())
    mid = store.query("SELECT id FROM message_records WHERE outbound_task_id=%s", (task["id"],))[0]["id"]
    return endpoint(aid, cid) + f"/{mid}/context"


async def test_slow_writer_never_holds_up_model_result_and_overflow_is_partial(tmp_path):
    from qingda.context_capture import CURRENT, Writer
    from qingda.model_settings import DEFAULT_MODEL
    from qingda.providers import Gateway

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    release = threading.Event()
    writer = Writer(store)
    original = writer.write

    def slow(envelope):
        release.wait(5)
        original(envelope)

    writer.write = slow
    gateway = Gateway(
        manager,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": "按时返回"}, "finish_reason": "stop"}]}
            )
        ),
    )
    gateway.secret = lambda name: "key"
    gateway.context_writer = writer
    token = CURRENT.set(capture)
    try:
        response = await asyncio.wait_for(
            gateway.chat(
                DEFAULT_MODEL,
                [{"role": "user", "content": json.dumps({"current_messages": [{"content": "你好"}]})}],
            ),
            1,
        )
        assert response["choices"][0]["message"]["content"] == "按时返回"
        writer.bytes = 32 * 1024 * 1024
        capture.tool({"call_id": "dropped-tool", "name": "finish_reply", "result": {"ok": True}})
        assert capture.manifest()["missing"]
    finally:
        CURRENT.reset(token)
        release.set()
        await writer.drain()
    path = accepted_path(store, manager, aid, cid, task, capture)
    assert http.get(path).json()["status"] == "partial"
    await gateway.close()


async def test_image_snapshot_is_hash_verified_and_scoped(tmp_path):
    from qingda.context_capture import Writer
    from test_authentication import code_login

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    image_message = incoming(store, manager, 98005, "3", "<msg><img/></msg>")
    with store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state='generating',lease_owner=%s WHERE id=%s", (task["_owner"], task["id"])
        )
    picture = io.BytesIO()
    Image.new("RGB", (32, 16), "green").save(picture, format="PNG")
    body = picture.getvalue()
    sha = hashlib.sha256(body).hexdigest()
    frame = {"message_id": image_message["id"], "sha256": sha, "width": 32, "height": 16}
    manager.media.path(frame).write_bytes(body)
    writer = Writer(store)
    payload = {
        "model": "deepseek-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps({"current_messages": [{"content": "看图"}]})},
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "image_location": "current_messages[0]",
                                "message_id": frame["message_id"],
                                "source_type": "direct",
                            }
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + base64.b64encode(body).decode()},
                    },
                ],
            }
        ],
    }
    call_id = capture.request(payload, {"tokens": 1200}, writer)
    capture.response(call_id, {"choices": []}, 200, 1)
    await writer.drain()
    path = accepted_path(store, manager, aid, cid, task, capture)
    result = http.get(path).json()
    assert result["input"]["images"][0]["sha256"] == sha
    assert "base64" not in json.dumps(
        store.query("SELECT details FROM reply_execution_events WHERE task_id=%s", (task["id"],))
    )
    assert http.get(path + "/images/" + sha).content == body
    from qingda.media_access import history_media_access

    with history_media_access(manager.media.directory, cid, exclusive=True) as acquired:
        assert acquired
        assert http.get(path + "/images/" + sha).status_code == 404
    assert http.get(path + "/images/" + "0" * 64).status_code == 404
    manager.media.path(frame).write_bytes(b"corrupt")
    assert http.get(path + "/images/" + sha).status_code == 404
    assert code_login(http, target="13900000001").status_code == 200
    assert http.get(path).status_code == 404
    assert http.get(path + "/images/" + sha).status_code == 404
    http.cookies.clear()
    assert http.get(path).status_code == 401


async def test_checkpoint_restores_exact_lineage_and_cursor_binds_message(tmp_path):
    from qingda.context_capture import Capture, Writer
    from qingda.tool_generation import Checkpoint

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    writer = Writer(store)
    for text in ("第一次实际输入", "第二次实际输入"):
        call_id = capture.request(
            {
                "messages": [
                    {"role": "user", "content": json.dumps({"current_messages": [{"content": text}]})}
                ]
            },
            {},
            writer,
        )
        capture.response(call_id, {"choices": []}, 200, 1)
    await writer.drain()
    checkpoint = Checkpoint(store, task["id"], "same-input", capture)
    checkpoint.save([], {"prompt_tokens": 20}, [], 2)
    restored = Capture(manager, task)
    loaded = Checkpoint(store, task["id"], "same-input", restored).load()
    assert loaded == ([], {"prompt_tokens": 20}, [], 2)
    assert [item["call_id"] for item in restored.expected] == [item["call_id"] for item in capture.expected]
    path = accepted_path(store, manager, aid, cid, task, restored)
    first = http.get(path, params={"limit": 1}).json()
    assert first["has_more"] and len(first["calls"]) == 1
    second = http.get(path, params={"cursor": first["cursor"], "limit": 1}).json()
    assert second["calls"][0]["call_id"] != first["calls"][0]["call_id"]
    assert http.get(path, params={"cursor": "wrong"}).status_code == 404
    revised = Capture(manager, {**task, "_private_revision": 2})
    Checkpoint(store, task["id"], "same-input", revised).load()
    assert revised.expected == [] and revised.missing


def test_two_process_writers_deduplicate_and_cannot_revive_cleared_history(tmp_path):
    from qingda.context_capture import Writer

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    actor = Path(__file__).with_name("context_capture_actor.py")
    environment = {
        **os.environ,
        "DATABASE_URL": store.url,
        "CAPTURE_TASK": task["id"],
        "CAPTURE_OWNER": task["_owner"],
        "CAPTURE_RUN": capture.run_id,
    }
    children = [
        subprocess.Popen(
            [sys.executable, str(actor), str(index)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    for child in children:
        output, error = child.communicate(timeout=20)
        assert child.returncode == 0, error
        assert output.strip() == "recorded"
    assert (
        len(
            store.query(
                "SELECT id FROM reply_execution_events WHERE task_id=%s AND phase='context_blob'",
                (task["id"],),
            )
        )
        == 1
    )
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET lease_owner='new-owner' WHERE id=%s", (task["id"],))
    Writer(store).write((capture, "tool", json.dumps({"call_id": "stale", "run_id": capture.run_id})))
    assert not store.query("SELECT id FROM reply_execution_events WHERE details->>'call_id'='stale'")
    csrf = http.get("/wechat/api/v1/auth/session").json()["csrf"]
    response = http.request(
        "DELETE",
        endpoint(aid, cid),
        json={"request_id": str(uuid.uuid4()), "confirmed": True},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    Writer(store).write((capture, "tool", json.dumps({"call_id": "late", "run_id": capture.run_id})))
    assert not store.query("SELECT id FROM reply_execution_events WHERE task_id=%s", (task["id"],))


async def test_worker_binds_real_request_repairs_and_terminal_tools(tmp_path):
    from qingda.providers import Gateway
    from qingda.reply_context import history_context
    from qingda.worker import Worker
    from test_automation import NOW, deliver, setup

    store, manager, _, conv = setup(tmp_path)
    deliver(store, manager, 1)
    payloads = []

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        current = json.loads(payload["messages"][1]["content"])["current_messages"]
        decision = {
            "action": "reply",
            "content": "" if len(payloads) == 1 else "修复后的实际答复",
            "reason": "answer",
            "close_dialogue": False,
            "handled_message_ids": [value["message_id"] for value in current],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": f"finish-{len(payloads)}",
                                    "type": "function",
                                    "function": {"name": "finish_reply", "arguments": json.dumps(decision)},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 15},
            },
        )

    class Native(Gateway):
        async def online(self, app_id):
            return True

        async def send(self, app_id, peer, content):
            assert content == "修复后的实际答复"
            return {"ret": 200, "data": {"newMsgId": "98999"}}

    gateway = Native(manager, httpx.MockTransport(handler))
    gateway.secret = lambda name: "isolated-key"
    worker = Worker(manager, gateway, clock=lambda: NOW + 10)
    try:
        await worker.once()
        task = store.query("SELECT * FROM tasks ORDER BY ordinal DESC LIMIT 1")[0]
        assert task["state"] == "accepted", task["reason"]
        await gateway.context_writer.drain()
        mid = store.query("SELECT id FROM message_records WHERE outbound_task_id=%s", (task["id"],))[0]["id"]
        body = history_context(manager, conv["account_id"], conv["id"], mid)
        assert body["status"] == "complete", body
        assert [call["kind"] for call in body["calls"]] == ["model", "tool", "model", "tool"]
        for call, payload in zip([call for call in body["calls"] if call["kind"] == "model"], payloads):
            assert (
                history_context(manager, conv["account_id"], conv["id"], mid, call["call_id"])["request"]
                == payload
            )
    finally:
        worker.release()
        await gateway.close()


async def test_shared_blocks_remain_readable_after_input_revision(tmp_path):
    from qingda.context_capture import Capture, Writer

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    writer = Writer(store)
    payload = {
        "messages": [
            {"role": "system", "content": "相同系统提示词"},
            {"role": "user", "content": json.dumps({"current_messages": [{"content": "同一原问题"}]})},
        ],
        "tools": [],
    }
    first = capture.request(payload, {}, writer)
    capture.response(first, {"choices": []}, 200, 1)
    await writer.drain()
    revised = Capture(manager, {**task, "_private_revision": 1})
    second = revised.request(payload, {}, writer)
    revised.response(second, {"choices": []}, 200, 1)
    await writer.drain()
    path = accepted_path(store, manager, aid, cid, task, revised)
    result = http.get(path).json()
    assert result["status"] == "complete", result
    assert result["calls"][0]["call_id"] == second
    assert http.get(path, params={"call_id": first}).status_code == 404


async def test_recovered_terminal_tool_is_recorded_without_new_model_call(tmp_path):
    from qingda.context_capture import Capture, Writer
    from qingda.model_settings import DEFAULT_MODEL
    from qingda.providers import Gateway
    from qingda.tool_generation import Checkpoint, generate

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    writer = Writer(store)
    initial = capture.request(
        {
            "messages": [
                {"role": "user", "content": json.dumps({"current_messages": [{"content": "原问题"}]})}
            ]
        },
        {},
        writer,
    )
    capture.response(initial, {"choices": []}, 200, 1)
    await writer.drain()
    decision = {
        "action": "reply",
        "content": "恢复后的结果",
        "reason": "answer",
        "close_dialogue": False,
        "handled_message_ids": [task["message_id"]],
    }
    pending = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "pending-finish",
                    "type": "function",
                    "function": {"name": "finish_reply", "arguments": json.dumps(decision)},
                }
            ],
        }
    ]
    Checkpoint(store, task["id"], "resume", capture).save(pending, {}, [], 1)
    restored = Capture(manager, task)

    def forbidden(request):
        raise AssertionError("恢复终止工具不应再次调用模型")

    gateway = Gateway(manager, httpx.MockTransport(forbidden))
    gateway.context_writer = writer
    context = [
        {
            "current": True,
            "role": "user",
            "sender": "alice",
            "message_id": task["message_id"],
            "content": "原问题",
        }
    ]
    result = await generate(
        gateway,
        None,
        DEFAULT_MODEL,
        "原提示词",
        context,
        checkpoint=Checkpoint(store, task["id"], "resume", restored),
        decision_mode=True,
        execution=SimpleNamespace(capture=restored),
    )
    assert result["reply_decision"] == decision
    await writer.drain()
    path = accepted_path(store, manager, aid, cid, task, restored)
    body = http.get(path).json()
    assert body["status"] == "complete", body
    assert body["calls"][-1]["name"] == "finish_reply"
    await gateway.close()


async def test_writer_holding_block_transaction_does_not_block_task_updates(tmp_path):
    from qingda.context_capture import Writer

    _, store, _, _, _, task, capture = capture_fixture(tmp_path)
    entered, release = threading.Event(), threading.Event()

    class SlowBlocks(Writer):
        def block(self, conn, capture, value):
            entered.set()
            release.wait(5)
            return super().block(conn, capture, value)

    writer = SlowBlocks(store)
    capture.request({"messages": [{"role": "user", "content": "原文"}]}, {}, writer)
    assert await asyncio.to_thread(entered.wait, 3)
    update = asyncio.create_task(
        asyncio.to_thread(
            store.query,
            "UPDATE tasks SET content='正常业务更新' WHERE id=%s RETURNING content",
            (task["id"],),
        )
    )
    try:
        rows = await asyncio.wait_for(asyncio.shield(update), 0.5)
        assert rows[0]["content"] == "正常业务更新"
    finally:
        release.set()
        await update
        await writer.drain()


async def test_capture_keeps_all_fixed_context_case_requests_unchanged(tmp_path):
    from qingda.config import ROOT
    from qingda.context_capture import CURRENT
    from qingda.model_settings import DEFAULT_MODEL
    from qingda.providers import Gateway

    _, _, manager, _, _, _, capture = capture_fixture(tmp_path)
    cases = json.loads((ROOT / "evals/acceptance/reply-context.json").read_text())["cases"]
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "固定协议替身"}}]}
        )

    gateway = Gateway(manager, httpx.MockTransport(handler))
    gateway.secret = lambda name: "fixture-key"
    try:
        for case in cases:
            messages = [
                {"role": "system", "content": "固定上下文契约"},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "conversation_kind": case["kind"],
                            "current_messages": [{"content": case["question"]}],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
            await gateway.chat(DEFAULT_MODEL, messages, [])
            baseline = sent[-1]
            token = CURRENT.set(capture)
            try:
                await gateway.chat(DEFAULT_MODEL, messages, [])
            finally:
                CURRENT.reset(token)
            assert sent[-1] == baseline, case["id"]
    finally:
        await gateway.close()


async def test_clear_committed_during_snapshot_read_cannot_return_deleted_context(tmp_path, monkeypatch):
    from qingda import reply_context
    from qingda.context_capture import Writer
    from qingda.history_deletion import advance, start

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    writer = Writer(store)
    call_id = capture.request(
        {
            "messages": [
                {"role": "user", "content": json.dumps({"current_messages": [{"content": "清空前的原文"}]})}
            ]
        },
        {},
        writer,
    )
    capture.response(call_id, {"choices": []}, 200, 1)
    await writer.drain()
    path = accepted_path(store, manager, aid, cid, task, capture)
    original, cleared = reply_context.sections, []

    def clear_during_read(request):
        if not cleared:
            cleared.append(True)
            job = start(manager, aid, cid, str(uuid.uuid4()))
            advance(manager, job["id"])
        return original(request)

    monkeypatch.setattr(reply_context, "sections", clear_during_read)
    assert http.get(path).status_code == 404


async def test_reused_native_ids_in_different_stages_keep_both_tool_executions(tmp_path):
    from qingda.context_capture import Writer

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    writer = Writer(store)
    call_id = capture.request(
        {"messages": [{"role": "user", "content": json.dumps({"current_messages": [{"content": "问题"}]})}]},
        {},
        writer,
    )
    capture.response(call_id, {"choices": []}, 200, 1)
    capture.tool(
        {"call_id": "call_0", "name": "finish_input_control", "result": {"ok": True}}, "input_control"
    )
    capture.tool({"call_id": "call_0", "name": "finish_reply", "result": {"ok": True}}, "reply")
    await writer.drain()
    path = accepted_path(store, manager, aid, cid, task, capture)
    assert [call.get("name") for call in http.get(path).json()["calls"] if call["kind"] == "tool"] == [
        "finish_input_control",
        "finish_reply",
    ]


async def test_orphaned_attempt_and_missing_response_are_not_reported_complete(tmp_path):
    from qingda.context_capture import Capture, Writer

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    writer = Writer(store)
    payload = {
        "messages": [{"role": "user", "content": json.dumps({"current_messages": [{"content": "问题"}]})}]
    }
    capture.request(payload, {}, writer)
    await writer.drain()
    restored = Capture(manager, task)
    call_id = restored.request(payload, {}, writer)
    restored.response(call_id, {"choices": []}, 200, 1)
    await writer.drain()
    path = accepted_path(store, manager, aid, cid, task, restored)
    assert http.get(path).json()["status"] == "partial"
    with store.transaction() as conn:
        conn.execute(
            "DELETE FROM reply_execution_events WHERE task_id=%s AND phase='context_response'", (task["id"],)
        )
    body = http.get(path).json()
    assert body["calls"][0]["request_available"] is True
    assert body["calls"][0]["response_available"] is False
    assert body["calls"][0]["available"] is False


def test_legacy_summary_uses_frozen_coverage_instead_of_current_configuration(tmp_path):
    from qingda.context_snapshot import freeze

    http, store, manager, aid, cid, task, capture = capture_fixture(tmp_path)
    freeze(
        manager,
        task,
        [
            {
                "current": True,
                "role": "user",
                "sender": "alice",
                "message_id": task["message_id"],
                "content": "正文",
                "history_selection": {
                    "scope": "current_conversation",
                    "selected_messages": 24,
                    "has_more": True,
                    "trim_reasons": {"round_limit": 6},
                },
            }
        ],
    )
    path = accepted_path(store, manager, aid, cid, task, capture)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET trace_json=%s WHERE id=%s",
            (json.dumps({"tool_calls": [{"name": "finish_reply", "result": {"ok": True}}]}), task["id"]),
        )
    body = http.get(path).json()
    assert body["status"] == "partial"
    assert body["input"]["data"]["history_coverage"]["selected_messages"] == 24
    assert body["input"]["data"]["history_coverage"]["trim_reasons"] == {"round_limit": 6}
    assert body["calls"][0]["name"] == "finish_reply"
