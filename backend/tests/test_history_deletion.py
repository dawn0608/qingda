"""仅服务器隔离库：全链路永久删除、范围、续清及跨进程租约。"""

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import wave

import pytest
from contact_fixtures import setup_directory
from qingda import history_deletion, history_voice
from qingda.database_async import database_call
from qingda.media_access import history_media_access
from qingda.tenancy import current_user, user_scope
from test_authentication import code_login
from test_chat_records import endpoint, incoming


def populate(tmp_path, peer="alice"):
    http, store, manager, aid, headers = setup_directory(tmp_path)
    http.headers.update(headers)
    marker = "PRIVATE_CLEAR_" + uuid.uuid4().hex
    text = incoming(store, manager, 88001, content=marker, peer=peer)
    photo = incoming(store, manager, 88002, "3", '<msg><img aeskey="secret-key"/></msg>', peer=peer)
    voice = incoming(store, manager, 88003, "34", '<msg><voicemsg voicelength="3740"/></msg>', peer=peer)
    for platform, kind in ((88011, "47"), (88012, "43"), (88013, "10002")):
        excluded = incoming(store, manager, platform, kind, "<msg>" + marker + "</msg>", peer=peer)
        manager.media.raw_path(excluded["id"]).write_bytes(marker.encode())
    other = incoming(store, manager, 88004, content="其他会话必须保留", peer="bob")
    cid = text["conversation_id"]
    with store.transaction() as conn:
        account = manager.account(aid, conn)
        conv = manager.conversation(cid, conn)
        task = conn.execute("SELECT * FROM tasks WHERE message_id=%s", (text["id"],)).fetchone()
        if not task:
            task = manager.create_task(
                conn, "auto", account, conv, text["id"], state="accepted", content=marker
            )
        tid = task["id"] if isinstance(task, dict) else task
        conn.execute(
            "UPDATE tasks SET state='accepted',content=%s,context_json=%s,trace_json=%s WHERE id=%s",
            (marker, json.dumps([dict(content=marker)]), json.dumps(dict(hidden=marker)), tid),
        )
        conn.execute(
            "INSERT INTO reply_context_snapshots(task_id,fingerprint,manifest,recall,updated_at) VALUES(%s,'test',%s,%s,0)",
            (tid, json.dumps(dict(body=marker)), json.dumps([marker])),
        )
        conn.execute(
            "INSERT INTO reply_generation_checkpoints(task_id,fingerprint,messages_json,usage_json,trace_json,updated_at) VALUES(%s,'test',%s,'{}','{}',0)",
            (tid, json.dumps([marker])),
        )
        conn.execute(
            "INSERT INTO reply_input_links(message_id,task_id,kind,in_context,created_at,source_mid) VALUES(%s,%s,'direct',1,0,%s)",
            (text["id"], tid, text["id"]),
        )
        for row, kind in ((photo, "image"), (voice, "voice")):
            body = (marker + kind).encode()
            conn.execute(
                "INSERT INTO incoming_media(message_id,state,kind,sha256,transcript,updated_at) VALUES(%s,'failed',%s,%s,%s,0)",
                (row["id"], kind, hashlib.sha256(body).hexdigest(), marker if kind == "voice" else ""),
            )
            manager.media.raw_path(row["id"]).write_bytes(body)
        conn.execute(
            "INSERT INTO callback_versions(event_id,payload,raw_sha256,received_at) VALUES(%s,%s,%s,0)",
            (
                text["event_id"],
                json.dumps(dict(Data=dict(Content=marker))),
                hashlib.sha256(marker.encode()).hexdigest(),
            ),
        )
    for suffix in (".bin", "-" + "a" * 64 + ".png", "." + "b" * 32 + ".tmp"):
        (manager.media.directory / f"{photo['id']}{suffix}").write_bytes(marker.encode())
    transient = manager.media.directory / f"playback-{voice['id']}-example"
    transient.mkdir()
    (transient / "audio.wav").write_bytes(marker.encode())
    outside = tmp_path / "outside-protected"
    outside.write_text("不允许跟随链接删除")
    (manager.media.directory / f"{photo['id']}-{'c' * 64}.png").symlink_to(outside)
    manager.media.raw_path(other["id"]).write_bytes(b"other-cache")
    event = dict(
        Appid="wx_current_terminal",
        Wxid="me",
        TypeName="AddMsg",
        Data=dict(
            NewMsgId=88005,
            FromUserName=peer,
            ToUserName="me",
            MsgType=1,
            Content=marker,
            CreateTime=manager.clock(),
        ),
    )
    pending = store.receive(json.dumps(event).encode())
    with store.connect() as conn:
        for imported in (0, 1):
            raw = json.dumps({**event, "Data": {**event["Data"], "NewMsgId": 88007 + imported}})
            conn.execute(
                "INSERT INTO migration_inbox(received_at,body,raw_sha256,imported,user_id) VALUES(0,%s,%s,%s,%s)",
                (raw, hashlib.sha256(raw.encode()).hexdigest(), imported, current_user.get()),
            )
    return http, store, manager, aid, cid, marker, text, other, tid, pending, outside


@pytest.mark.parametrize("peer", ["alice", "room@chatroom"])
def test_purge_all_content_files_and_pending_callbacks_preserves_new_and_other(tmp_path, peer):
    http, store, manager, aid, cid, marker, text, other, tid, pending, outside = populate(tmp_path, peer)
    old = store.query("SELECT * FROM message_records WHERE conversation_id=%s", (other["conversation_id"],))
    conv = manager.conversation(cid)
    calls = list(http.app.state.gateway.calls)
    request_id = str(uuid.uuid4())
    history_deletion.start(manager, aid, cid, request_id)
    assert store.query("SELECT processed FROM events WHERE id=%s", (pending,))[0]["processed"] == 1
    fresh = incoming(store, manager, 88006, content="清空期间的新消息", peer=peer)
    manager.media.raw_path(fresh["id"]).write_bytes(b"new-cache")
    history_deletion.advance(manager, request_id)
    status = history_deletion.status(manager, aid, cid, request_id)
    assert status["state"] == "complete", store.query("SELECT * FROM chat_history_deletions")
    assert (
        store.query("SELECT * FROM message_records WHERE conversation_id=%s", (other["conversation_id"],))
        == old
    )
    assert [
        r["id"] for r in store.query("SELECT id FROM message_records WHERE conversation_id=%s", (cid,))
    ] == [fresh["id"]]
    assert manager.conversation(cid)["allowed"] == conv["allowed"]
    assert manager.conversation(cid)["pause_until"] == conv["pause_until"]
    assert outside.read_text() == "不允许跟随链接删除"
    assert manager.media.raw_path(other["id"]).read_bytes() == b"other-cache"
    assert manager.media.raw_path(fresh["id"]).read_bytes() == b"new-cache"
    assert sorted(
        p.name for p in manager.media.directory.iterdir() if not p.name.startswith(".history-media-")
    ) == sorted([f"{other['id']}.bin", f"{fresh['id']}.bin"])
    for table in (
        "events",
        "callback_versions",
        "message_contents",
        "message_views",
        "incoming_media",
        "tasks",
        "reply_context_snapshots",
        "reply_generation_checkpoints",
        "migration_inbox",
    ):
        assert not store.query(
            f"SELECT 1 FROM {table} r WHERE to_jsonb(r)::text LIKE %s", ("%" + marker + "%",)
        ), table
    # 重复的同一请求不能清掉确认之后的新消息；平台重投也不能还原已删除记录。
    response = http.request("DELETE", endpoint(aid, cid), json=dict(request_id=request_id, confirmed=True))
    assert response.status_code == 200 and response.json()["state"] == "complete"
    assert (
        store.receive(
            json.dumps(
                dict(
                    Appid="wx_current_terminal",
                    Wxid="me",
                    TypeName="AddMsg",
                    Data=dict(NewMsgId=88001, FromUserName=peer, ToUserName="me", MsgType=1, Content=marker),
                )
            ).encode()
        )
        is None
    )
    manager.process_events()
    assert len(http.get(endpoint(aid, cid)).json()["items"]) == 1
    assert http.app.state.gateway.calls == calls


def test_confirmation_csrf_auth_and_scope_are_required(tmp_path):
    http, store, manager, aid, cid, *_ = populate(tmp_path)
    path = endpoint(aid, cid)
    request_id = str(uuid.uuid4())
    body = dict(request_id=request_id, confirmed=True)
    assert http.request("DELETE", path, json=dict(request_id=request_id, confirmed=False)).status_code == 422
    assert http.request("DELETE", path, json={}).status_code == 422
    token = http.headers.pop("X-CSRF-Token", None)
    assert http.request("DELETE", path, json=body).status_code == 403
    if token:
        http.headers["X-CSRF-Token"] = token
    second = manager.add_account("another-clear-account")
    assert http.request("DELETE", endpoint(second["id"], cid), json=body).status_code in (404, 409)
    assert not store.query("SELECT * FROM chat_history_deletions")
    http.cookies.clear()
    assert http.request("DELETE", path, json=body).status_code == 401
    login = code_login(http, target="13900000001")
    assert login.status_code == 200
    http.headers["X-CSRF-Token"] = login.json()["csrf"]
    assert http.request("DELETE", path, json=body).status_code in (404, 409)


def test_active_execution_drains_and_file_failure_retries_without_false_success(tmp_path, monkeypatch):
    _, store, manager, aid, cid, _, _, _, tid, *_ = populate(tmp_path)
    token = uuid.uuid4().hex
    with store.connect() as conn:
        conn.execute("UPDATE tasks SET state='generating',lease_owner='old-worker' WHERE id=%s", (tid,))
        conn.execute(
            "INSERT INTO reply_resource_slots(resource,token,task_id,owner,expires_at) VALUES('active',%s,%s,'old-worker',%s)",
            (token, tid, time.time() + 120),
        )
    request_id = str(uuid.uuid4())
    history_deletion.start(manager, aid, cid, request_id)
    assert store.query("SELECT state FROM tasks WHERE id=%s", (tid,))[0]["state"] == "canceled"
    history_deletion.advance(manager, request_id)
    assert history_deletion.status(manager, aid, cid, request_id)["state"] == "waiting"
    assert store.query("SELECT * FROM message_records WHERE conversation_id=%s", (cid,))
    with store.connect() as conn:
        conn.execute("DELETE FROM reply_resource_slots WHERE token=%s", (token,))
        conn.execute("UPDATE chat_history_deletions SET next_at=0 WHERE id=%s", (request_id,))
    original = history_deletion.clean_files
    monkeypatch.setattr(history_deletion, "clean_files", lambda *_: (_ for _ in ()).throw(PermissionError()))
    history_deletion.advance(manager, request_id)
    result = history_deletion.status(manager, aid, cid, request_id)
    assert result["state"] == "files" and result["error"] == "cleanup_failed"
    assert not store.query("SELECT * FROM message_records WHERE conversation_id=%s", (cid,))
    monkeypatch.setattr(history_deletion, "clean_files", original)
    with store.connect() as conn:
        conn.execute("UPDATE chat_history_deletions SET next_at=0 WHERE id=%s", (request_id,))
    history_deletion.advance(manager, request_id)
    assert history_deletion.status(manager, aid, cid, request_id)["state"] == "complete"


def test_two_processes_lease_exclusion_crash_and_expired_takeover(tmp_path):
    _, store, manager, aid, cid, *_ = populate(tmp_path)
    request_id = str(uuid.uuid4())
    history_deletion.start(manager, aid, cid, request_id)
    env = {**os.environ, "CLEAR_TEST_DATABASE_URL": store.url}
    code = """import os,sys,time,json
from pathlib import Path
from types import SimpleNamespace
from qingda.store import Store
from qingda.tenancy import user_scope
from qingda.history_deletion import claim,advance,status
from qingda.media_access import history_media_access
with user_scope(int(sys.argv[1])):
 m=SimpleNamespace(store=Store(os.environ['CLEAR_TEST_DATABASE_URL']),media=SimpleNamespace(directory=Path(sys.argv[2])))
 if sys.argv[4]=='first':
  with history_media_access(m.media.directory,int(sys.argv[6])):
   job=claim(m,sys.argv[3]);print('claimed',flush=True);input()
 else:
  print('excluded' if claim(m,sys.argv[3]) is None else 'duplicate',flush=True);input();advance(m,sys.argv[3]);print(json.dumps(status(m,int(sys.argv[5]),int(sys.argv[6]),sys.argv[3])),flush=True)
"""
    children = []
    try:
        args = [str(current_user.get()), str(manager.media.directory), request_id]
        first = subprocess.Popen(
            [sys.executable, "-c", code, *args, "first", str(aid), str(cid)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        children.append(first)
        assert first.stdout.readline().strip() == "claimed"
        with history_media_access(manager.media.directory, cid, exclusive=True) as acquired:
            assert not acquired
        second = subprocess.Popen(
            [sys.executable, "-c", code, *args, "second", str(aid), str(cid)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        children.append(second)
        assert second.stdout.readline().strip() == "excluded"
        first.kill()
        first.wait(timeout=5)
        with history_media_access(manager.media.directory, cid, exclusive=True) as acquired:
            assert acquired
        with store.connect() as conn:
            conn.execute(
                "UPDATE chat_history_deletions SET lease_until=%s WHERE id=%s",
                (time.time() + 0.2, request_id),
            )
        time.sleep(0.3)
        second.stdin.write("\n")
        second.stdin.flush()
        result = json.loads(second.stdout.readline())
        second.wait(timeout=10)
        assert result["state"] == "complete"
        assert history_deletion.status(manager, aid, cid, request_id)["state"] == "complete"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_request_cancellation_does_not_abandon_committed_cleanup(tmp_path, monkeypatch):
    _, store, manager, aid, cid, *_ = populate(tmp_path)
    request_id = str(uuid.uuid4())
    history_deletion.start(manager, aid, cid, request_id)
    entered, release = threading.Event(), threading.Event()
    original = history_deletion.clean_files

    def delayed(*args):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(history_deletion, "clean_files", delayed)

    async def run():
        task = asyncio.create_task(database_call(history_deletion.advance, manager, request_id))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(0.03)
            assert not task.done()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert history_deletion.status(manager, aid, cid, request_id)["state"] == "complete"


def test_api_purges_all_search_results_and_resets_old_watermark(tmp_path):
    http, store, manager, aid, cid, _, text, _, tid, *_ = populate(tmp_path)
    path = endpoint(aid, cid)
    watermark = http.get(path).json()["watermark"]
    media_ids = [
        r["id"]
        for r in store.query(
            "SELECT id FROM messages WHERE conversation_id=%s AND msg_type IN ('3','34')", (cid,)
        )
    ]
    with store.connect() as conn:
        conn.execute("UPDATE tasks SET state='sending' WHERE id=%s", (tid,))
    request_id = str(uuid.uuid4())
    body = dict(request_id=request_id, confirmed=True)
    assert http.request("DELETE", path, json=body).status_code == 409
    assert not store.query("SELECT * FROM chat_history_deletions")
    with store.connect() as conn:
        conn.execute("UPDATE tasks SET state='accepted' WHERE id=%s", (tid,))
    response = http.request("DELETE", path, json=body)
    assert response.status_code == 200 and response.json()["state"] == "complete", response.text
    assert response.headers["cache-control"] == "no-store"
    result = http.get(path, params=dict(after_id=watermark)).json()
    assert result["items"] == [] and result["history_revision"] == request_id
    for mid in media_ids:
        for suffix in ("image", "voice"):
            assert http.get(f"{path}/{mid}/{suffix}").status_code == 404
    assert (
        http.get(path + "/deletion", params=dict(request_id=request_id)).json()["operation"]["state"]
        == "complete"
    )


def test_active_audio_read_blocks_completion_then_resumes(tmp_path):
    _, store, manager, aid, cid, *_ = populate(tmp_path)
    request_id = str(uuid.uuid4())
    history_deletion.start(manager, aid, cid, request_id)
    with history_media_access(manager.media.directory, cid) as acquired:
        assert acquired
        history_deletion.advance(manager, request_id)
        operation = history_deletion.status(manager, aid, cid, request_id)
        assert operation["state"] == "files" and not operation["error"]
        assert not store.query("SELECT * FROM message_records WHERE conversation_id=%s", (cid,))
    with store.connect() as conn:
        conn.execute("UPDATE chat_history_deletions SET next_at=0 WHERE id=%s", (request_id,))
    history_deletion.advance(manager, request_id)
    assert history_deletion.status(manager, aid, cid, request_id)["state"] == "complete"


@pytest.mark.parametrize("disconnect", [False, True])
def test_audio_conversion_cannot_write_or_return_old_data_after_purge(tmp_path, monkeypatch, disconnect):
    http, store, manager, aid, cid, *_ = populate(tmp_path)
    mid = store.query("SELECT id FROM messages WHERE conversation_id=%s AND msg_type='34'", (cid,))[0]["id"]
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000)
    body = output.getvalue()
    manager.media.raw_path(mid).write_bytes(body)
    with store.connect() as conn:
        conn.execute(
            "UPDATE incoming_media SET sha256=%s WHERE message_id=%s", (hashlib.sha256(body).hexdigest(), mid)
        )
    entered, finish = asyncio.Event(), asyncio.Event()

    class Request:
        async def is_disconnected(self):
            return disconnect and finish.is_set()

    async def convert(*args, **_):
        entered.set()
        if disconnect:
            await asyncio.Future()
        else:
            await finish.wait()
            args[4].write_bytes(body)
            return b'{"duration_ms":1000}'

    monkeypatch.setattr(history_voice, "run_process", convert)
    request_id = str(uuid.uuid4())

    async def run():
        task = asyncio.create_task(
            history_voice.voice_content(manager, http.app.state.config, aid, cid, mid, Request())
        )
        await asyncio.wait_for(entered.wait(), 5)
        await database_call(history_deletion.start, manager, aid, cid, request_id)
        await database_call(history_deletion.advance, manager, request_id)
        assert history_deletion.status(manager, aid, cid, request_id)["state"] == "files"
        finish.set()
        assert await asyncio.wait_for(task, 5) is None

    asyncio.run(run())
    assert all(path.name.endswith("-example") for path in manager.media.directory.glob("playback-*"))
    with store.connect() as conn:
        conn.execute("UPDATE chat_history_deletions SET next_at=0 WHERE id=%s", (request_id,))
    history_deletion.advance(manager, request_id)
    assert history_deletion.status(manager, aid, cid, request_id)["state"] == "complete"
    assert not list(manager.media.directory.glob("playback-*"))


def test_other_user_same_platform_id_and_shared_content_are_preserved(tmp_path):
    http, store, manager, aid, cid, marker, text, other, *_ = populate(tmp_path)
    owner = current_user.get()
    with store.connect() as conn:
        conn.execute(
            "UPDATE message_records SET content_id=%s WHERE id=%s", (text["content_id"], other["id"])
        )
    login = code_login(http, target="13900000001")
    assert login.status_code == 200
    foreign_user = login.json()["user_id"]
    with user_scope(foreign_user):
        manager.add_account("wx_current_terminal")
        manager.verify_identity("wx_current_terminal", {"wxid": "me"})
        foreign = incoming(store, manager, 88001, content="其他用户的原文", peer="alice")
        manager.media.raw_path(foreign["id"]).write_bytes(b"foreign-cache")
        snapshot = {
            table: store.query(f"SELECT * FROM {table}")
            for table in ("message_records", "message_contents", "events", "tasks")
        }
    with user_scope(owner):
        request_id = str(uuid.uuid4())
        history_deletion.start(manager, aid, cid, request_id)
        history_deletion.advance(manager, request_id)
        assert history_deletion.status(manager, aid, cid, request_id)["state"] == "complete"
        assert store.query("SELECT text FROM messages WHERE id=%s", (other["id"],))[0]["text"] == marker
    with user_scope(foreign_user):
        assert snapshot == {table: store.query(f"SELECT * FROM {table}") for table in snapshot}
        assert manager.media.raw_path(foreign["id"]).read_bytes() == b"foreign-cache"
        assert (
            store.receive(
                json.dumps(
                    dict(
                        Appid="wx_current_terminal",
                        Wxid="me",
                        TypeName="AddMsg",
                        Data=dict(
                            NewMsgId=88001,
                            FromUserName="alice",
                            ToUserName="me",
                            MsgType=1,
                            Content="其他用户的原文",
                        ),
                    )
                ).encode()
            )
            == foreign["event_id"]
        )
