"""服务器隔离回归：阿里录音协议、逐用户密钥、结果与共享容量。"""

import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
import pytest
from auth_fixtures import login_response
from business_fixtures import allow_reply
from qingda.media import IncomingMedia
from qingda.media_decode import prepare_voice, run_process
from qingda.providers import ProviderError
from qingda.reply_execution import AUDIT
from qingda.reply_media import VERSION as IMAGE_POLICY
from qingda.security import Vault
from qingda.service import Manager
from qingda.speech import CONCURRENCY, FINGERPRINT, MODEL, OPTIONS, RESOURCE, AliyunSpeech
from qingda.store import Store
from qingda.tenancy import initialize_user, user_scope
from qingda.worker import Worker
from test_automation import NOW, deliver, setup
from test_http import client
from test_media import MediaGateway, config_at
from test_worker import Gateway

KEY = "sk-isolated-private-asr-key-123456"


def wav_audio(seconds=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * round(seconds * 16000))
    return output.getvalue()


AUDIO = wav_audio()


def configured(tmp_path, manager=None):
    config = config_at(tmp_path)
    manager = manager or Manager(Store(config.database_url))
    manager.vault = Vault(config.key_file)
    manager.configure_asr(KEY)
    return config, manager


def success(**changes):
    return {
        "request_id": "isolated-request",
        "output": {"text": "嗯，明天15点开会。", "sentence": {"sentence_end": True}},
        "usage": {"duration": 2, "input_tokens": 30, "output_tokens": 10, "total_tokens": 40},
        **changes,
    }


async def test_recorded_payload_options_and_redacted_audit(tmp_path, caplog):
    config, manager = configured(tmp_path)
    calls, audit = [], []
    caplog.set_level(logging.INFO, logger="httpx")

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.host == "dashscope.aliyuncs.com"
        payload = json.loads(request.content)
        assert payload["model"] == MODEL and payload["parameters"] == OPTIONS
        content = payload["input"]["messages"]
        assert len(content) == 1 and len(content[0]["content"]) == 1
        encoded = content[0]["content"][0]["input_audio"]["data"]
        assert base64.b64decode(encoded.split(",", 1)[1]) == AUDIO
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert request.headers["X-DashScope-SSE"] == "disable"
        assert request.extensions["timeout"] == {"connect": 5, "read": 30, "write": 30, "pool": 30}
        return httpx.Response(200, json=success())

    async def record(value):
        audit.append(value)

    token = AUDIT.set(record)
    try:
        result = await AliyunSpeech(manager, httpx.MockTransport(handler)).transcribe(AUDIO, "wav")
    finally:
        AUDIT.reset(token)
    assert len(calls) == 1 and result["text"] == "嗯，明天15点开会。"
    assert result["engine"] == MODEL and result["service"] == "recorded_http"
    assert result["asr_fingerprint"] == FINGERPRINT and result["inference_options"] == OPTIONS
    assert audit[0]["usage"]["input_tokens"] == 30 and audit[0]["attempt"] == 1
    evidence = json.dumps(audit) + caplog.text + repr(config)
    assert KEY not in evidence and base64.b64encode(AUDIO).decode() not in evidence


@pytest.mark.parametrize(
    "status,code",
    [
        (401, None),
        (403, None),
        (429, None),
        (503, None),
        (200, "InvalidApiKey"),
        (200, "QuotaExceeded"),
        (400, "InsufficientBalance"),
        (200, KEY),
    ],
)
async def test_supplier_errors_never_retry_or_leak_response(tmp_path, status, code):
    _, manager = configured(tmp_path)
    calls, audit = [], []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json=success(code=code, message=KEY))

    async def record(value):
        audit.append(value)

    token = AUDIT.set(record)
    try:
        with pytest.raises(ProviderError) as error:
            await AliyunSpeech(manager, httpx.MockTransport(handler)).transcribe(AUDIO, "wav")
    finally:
        AUDIT.reset(token)
    assert len(calls) == 1 and KEY not in str(error.value)
    assert KEY not in json.dumps(audit) and audit[0]["request_id"] == "isolated-request"
    if code == "InsufficientBalance":
        assert error.value.reason == "asr_http_400_InsufficientBalance"


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": ""},
        {"request_id": KEY},
        {"output": []},
        {"output": {"text": ""}},
        {"usage": {"duration": True}},
        {"usage": {"duration": 61}},
        {"usage": {"duration": 2, "input_tokens": 30, "output_tokens": -1, "total_tokens": 29}},
        {"usage": {"duration": 2, "input_tokens": 30, "output_tokens": 10, "total_tokens": 41}},
    ],
)
async def test_incomplete_success_is_rejected(tmp_path, changes):
    _, manager = configured(tmp_path)
    speech = AliyunSpeech(
        manager, httpx.MockTransport(lambda _: httpx.Response(200, json=success(**changes)))
    )
    with pytest.raises(ProviderError, match="invalid_asr_result"):
        await speech.transcribe(AUDIO, "wav")


async def test_partial_output_is_rejected(tmp_path):
    _, manager = configured(tmp_path)
    speech = AliyunSpeech(
        manager,
        httpx.MockTransport(
            lambda _: httpx.Response(
                200, json=success(output={"text": "partial", "sentence": {"sentence_end": False}})
            )
        ),
    )
    with pytest.raises(ProviderError, match="asr_output_truncated"):
        await speech.transcribe(AUDIO, "wav")


async def test_missing_credentials_do_not_use_legacy_or_environment(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    manager = Manager(Store(config.database_url), Vault(config.key_file))
    manager.store.set_setting("media_key", manager.vault.encrypt(KEY))
    monkeypatch.setenv("DASHSCOPE_API_KEY", KEY)
    speech = AliyunSpeech(manager, httpx.MockTransport(lambda _: pytest.fail("配置缺失不应调用")))
    with pytest.raises(ProviderError, match="missing_asr_key"):
        await speech.transcribe(AUDIO, "wav")


@pytest.mark.parametrize("body", [b"", b"x" * (3 * 1024 * 1024 + 1), b"#!SILK_V3\x00\x00", wav_audio(60.1)])
async def test_invalid_audio_never_calls_supplier(tmp_path, body):
    _, manager = configured(tmp_path)
    speech = AliyunSpeech(manager, httpx.MockTransport(lambda _: pytest.fail("无效音频不应调用")))
    with pytest.raises((ProviderError, ValueError)):
        await speech.transcribe(body, "wav")


async def test_ready_cache_is_reused_and_old_policy_is_replaced_on_demand(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    config, manager = configured(tmp_path / "speech", manager)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=success())

    media = IncomingMedia(manager, config, AliyunSpeech(manager, httpx.MockTransport(handler)))
    deliver(store, manager, 1, MsgType=34, Content={"string": '<msg><voicemsg voicelength="2000"/></msg>'})
    task, gateway = store.query("SELECT * FROM tasks")[0], MediaGateway(AUDIO)
    await media.prepare(task, gateway)
    await media.prepare(task, gateway)
    assert len(calls) == 1 and gateway.downloads == 1
    store.query(
        "UPDATE incoming_media SET trace_json='{}',transcript='历史结果' WHERE message_id=1 RETURNING message_id"
    )
    await media.prepare(task, gateway)
    assert len(calls) == 2 and gateway.downloads == 1
    assert media.record(1)["transcript"] == "嗯，明天15点开会。"
    assert not list(media.directory.glob("voice-*"))


@pytest.mark.parametrize("ending", ["timeout", "cancel", "crash", "legacy_crash"])
async def test_persisted_attempt_blocks_reissue_after_interruption(tmp_path, ending):
    store, manager, _, _ = setup(tmp_path)
    config, manager = configured(tmp_path / "speech", manager)
    entered, calls = asyncio.Event(), []

    async def handler(request):
        calls.append(1)
        entered.set()
        if ending == "timeout":
            raise httpx.ReadTimeout("private payload", request=request)
        await asyncio.Event().wait()

    media = IncomingMedia(manager, config, AliyunSpeech(manager, httpx.MockTransport(handler)))
    deliver(store, manager, 1, MsgType=34, Content={"string": '<msg><voicemsg voicelength="2000"/></msg>'})
    task, gateway = store.query("SELECT * FROM tasks")[0], MediaGateway(AUDIO)
    if ending in ("crash", "legacy_crash"):
        store.query(
            "INSERT INTO incoming_media(message_id,state,kind,updated_at,trace_json) VALUES(1,'transcribing','voice',%s,%s) RETURNING message_id",
            (
                NOW,
                json.dumps(
                    {
                        "asr_fingerprint": FINGERPRINT if ending == "crash" else "retired-policy",
                        "asr_attempt": "crashed-owner",
                    }
                ),
            ),
        )
    elif ending == "cancel":
        running = asyncio.create_task(media.prepare(task, gateway))
        await asyncio.wait_for(entered.wait(), 10)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    else:
        with pytest.raises(ProviderError, match="asr_outcome_unknown"):
            await media.prepare(task, gateway)
    before = len(calls)
    restarted = IncomingMedia(manager, config, media.transcriber)
    with pytest.raises(ProviderError, match="asr_outcome_unknown"):
        await restarted.prepare(task, gateway)
    assert len(calls) == before and json.loads(media.record(1)["trace_json"])["asr_attempt"]


def test_asr_settings_are_independent_encrypted_and_fixed(tmp_path):
    c, store = client(tmp_path)
    headers = {"X-CSRF-Token": login_response(c).json()["csrf"]}
    assert not c.get("/wechat/api/v1/settings").json()["asr"]["key_configured"]
    store.set_setting("model_key", c.app.state.manager.vault.encrypt("deepseek-private"))
    result = c.patch("/wechat/api/v1/settings", json={"asr_key": KEY}, headers=headers)
    assert result.status_code == 200 and result.json()["asr"] == {
        "provider": "aliyun",
        "model": MODEL,
        "key_configured": True,
        "key_preview": KEY[:7] + "********" + KEY[-4:],
    }
    saved = store.setting("asr_key")
    assert saved != KEY and c.app.state.manager.vault.decrypt(saved) == KEY
    assert KEY not in result.text and "deepseek-private" not in result.text
    assert c.app.state.manager.vault.decrypt(store.setting("model_key")) == "deepseek-private"
    for body in ({"asr_key": ""}, {"asr_key": KEY}):
        assert c.patch("/wechat/api/v1/settings", json=body, headers=headers).status_code == 200
        assert store.setting("asr_key") == saved
    for body in ({"asr_model": "wrong"}, {"asr": {"model": "wrong"}}, {"media_key": KEY}):
        assert c.patch("/wechat/api/v1/settings", json=body, headers=headers).status_code == 422
    result = c.patch("/wechat/api/v1/settings", json={"asr_key": "invalid-key"}, headers=headers)
    assert result.status_code == 400 and store.setting("asr_key") == saved


async def test_same_client_reads_current_users_key_each_call(tmp_path):
    config, manager = configured(tmp_path)
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=success())

    speech = AliyunSpeech(manager, httpx.MockTransport(handler))
    manager.store.query(
        "INSERT INTO users(id,channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url) VALUES(2,'email','asr-other@example.com','用户二',0,'asr-other-hash','asr-other-key','https://example.com/callback') RETURNING id"
    )
    with user_scope(2):
        with manager.store.transaction() as conn:
            initialize_user(conn, 2)
        with pytest.raises(ProviderError, match="missing_asr_key"):
            await speech.transcribe(AUDIO, "wav")
        manager.configure_asr("sk-second-user-asr-key-123456")
        await speech.transcribe(AUDIO, "wav")
    await speech.transcribe(AUDIO, "wav")
    assert seen == ["Bearer sk-second-user-asr-key-123456", "Bearer " + KEY]


def test_key_change_cancels_only_voice_and_ignores_sending(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    _, manager = configured(tmp_path / "speech", manager)
    deliver(store, manager, 1, MsgType=34, Content={"string": "<msg><voicemsg/></msg>"})
    voice = store.query("SELECT id FROM tasks WHERE message_id=1")[0]["id"]
    allow_reply(manager, manager.ensure_conversation(1, "other")["id"], True)
    deliver(store, manager, 2, peer="other", Content={"string": "text"})
    text = store.query("SELECT id FROM tasks WHERE message_id=2")[0]["id"]
    allow_reply(manager, manager.ensure_conversation(1, "sending")["id"], True)
    deliver(store, manager, 3, peer="sending", MsgType=34, Content={"string": "<msg><voicemsg/></msg>"})
    store.query("UPDATE tasks SET state='sending' WHERE message_id=3 RETURNING id")
    manager.configure_asr("sk-new-user-asr-key-123456")
    states = {row["message_id"]: row["state"] for row in store.query("SELECT message_id,state FROM tasks")}
    assert states[1] == "canceled" and states[2] == "queued" and states[3] == "sending"
    assert voice != text


async def test_voice_decode_keeps_original_and_cleans_temporary_files(tmp_path):
    config = config_at(tmp_path)
    path = tmp_path / "original.bin"
    path.write_bytes(AUDIO)
    body, kind, duration = await prepare_voice(path, tmp_path, config)
    assert kind == "wav" and duration == 2000 and path.read_bytes() == AUDIO
    assert body.startswith(b"RIFF") and not list(tmp_path.glob("voice-*"))


async def test_decode_cancellation_reaps_process_group(tmp_path):
    marker = tmp_path / "pid"
    code = (
        "import os,pathlib,time;pathlib.Path("
        + repr(str(marker))
        + ").write_text(str(os.getpid()));time.sleep(30)"
    )
    task = asyncio.create_task(run_process(sys.executable, "-c", code))
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.02)
    assert marker.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


@pytest.mark.parametrize("old_state", ["ready", "queued"])
async def test_old_generated_voice_is_canceled_without_replaying(tmp_path, old_state):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, MsgType=34, Content={"string": "<msg><voicemsg/></msg>"})
    store.query(
        "UPDATE tasks SET state=%s,content='old voice result',trace_json=%s RETURNING id",
        (old_state, json.dumps({"image_policy": IMAGE_POLICY})),
    )
    gateway = Gateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 35)
    try:
        await worker.once()
        assert store.query("SELECT state,reason FROM tasks")[0] == {
            "state": "canceled",
            "reason": "reply_policy_changed",
        }
        assert not gateway.sends
    finally:
        worker.release()


@pytest.mark.parametrize("ending", ["cancel", "crash"])
async def test_two_process_two_user_shared_asr_capacity_and_recovery(tmp_path, ending):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    first = store.query("UPDATE tasks SET state='generating',lease_owner='asr-owner-1' RETURNING id")[0]["id"]
    store.query(
        "INSERT INTO users(id,channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url) VALUES(2,'email','asr-two@example.com','用户二',0,'asr-two-hash','asr-two-key','https://example.com/callback') RETURNING id"
    )
    with user_scope(2):
        with store.transaction() as conn:
            initialize_user(conn, 2)
        other = Manager(store, clock=lambda: NOW)
        account = other.add_account("device-asr-2", "第二用户")
        conv = other.ensure_conversation(account["id"], "friend-two")
        store.query(
            "INSERT INTO tasks(id,kind,account_id,conversation_id,state,created_at,updated_at,lease_owner) VALUES('asr-second','auto',%s,%s,'generating',%s,%s,'asr-owner-2') RETURNING id",
            (account["id"], conv["id"], NOW, NOW),
        )
    actor = Path(__file__).with_name("reply_capacity_actor.py")

    def environment(uid, task):
        return {
            **os.environ,
            "ACTOR_DATABASE_URL": store.url,
            "ACTOR_TASK_ID": task,
            "ACTOR_USER_ID": str(uid),
            "ACTOR_RESOURCE": RESOURCE,
            "ACTOR_LIMIT": str(CONCURRENCY),
        }

    processes = []

    async def probe():
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(actor), "probe"],
            env=environment(1, first),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    try:
        for uid, tid in ((1, first), (2, "asr-second")):
            process = subprocess.Popen(
                [sys.executable, str(actor), "hold"],
                env=environment(uid, tid),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            processes.append(process)
            assert (
                await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10)
            ).strip() == b"occupied"
        assert await probe() == "busy"
        victim = processes[0]
        victim.terminate() if ending == "cancel" else victim.kill()
        await asyncio.to_thread(victim.communicate, timeout=10)
        if ending == "crash":
            assert await probe() == "busy"
            store.query(
                "UPDATE reply_resource_slots SET expires_at=%s WHERE resource=%s AND owner='asr-owner-1' RETURNING token",
                (time.time() - 1, RESOURCE),
            )
        assert await probe() == "available"
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.communicate, timeout=10)
    assert not store.query("SELECT * FROM reply_resource_slots WHERE resource=%s", (RESOURCE,))
