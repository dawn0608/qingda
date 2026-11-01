"""服务器隔离回归：固定型号、原生图片及视频全路径排除。"""

import io
import json
import wave

import httpx
import pytest
from auth_fixtures import login_response
from test_automation import NOW, deliver, setup
from test_emoji_policy import image_bytes
from test_group_wakeup import GROUP, group_setup, pat
from test_http import client
from test_media import MediaGateway, config_at
from wechat_manager.media import IncomingMedia
from wechat_manager.media_decode import decode
from wechat_manager.model_settings import DEFAULT_MODEL, effective_model
from wechat_manager.providers import Gateway, ProviderError
from wechat_manager.reply_media import VERSION
from wechat_manager.security import Vault
from wechat_manager.speech import inspect_voice
from wechat_manager.worker import Worker


def test_fixed_model_and_new_key_validation(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        if request.headers["Authorization"] == "Bearer invalid":
            return httpx.Response(401, json={"secret": "must not leak"})
        return httpx.Response(200, json={"object": "list", "data": [{"id": "deepseek-flash"}]})

    c, store = client(tmp_path, gateway_factory=lambda m: Gateway(m, httpx.MockTransport(handler)))
    headers = {"X-CSRF-Token": login_response(c).json()["csrf"]}
    store.set_setting(
        "model",
        {**DEFAULT_MODEL, "model": "retired-model", "thinking_enabled": True, "web_search_enabled": False},
    )
    assert c.get("/wechat/api/v1/settings").json()["model"] == DEFAULT_MODEL
    body = {"model_key": "new-key"}
    result = c.patch("/wechat/api/v1/settings", json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["model"] == DEFAULT_MODEL
    assert "media_models" not in result.json()
    assert (
        c.patch("/wechat/api/v1/settings", json={"media_key": "retired"}, headers=headers).status_code == 422
    )
    assert store.setting("model")["model"] == "deepseek-flash"
    assert len(seen) == 1 and str(seen[0].url) == "https://api.deepseek.com/models"
    assert (
        c.patch(
            "/wechat/api/v1/settings", json={"model": {"thinking_enabled": False}}, headers=headers
        ).status_code
        == 400
    )
    assert len(seen) == 1
    encrypted, revision = store.setting("model_key"), store.setting("model_revision")
    for invalid in (
        {"model_key": "invalid"},
        {"model": {"model": "other"}},
        {"model": {"thinking_enabled": 1}},
        {"model": {"thinking_enabled": True}},
        {"model": {"web_search_enabled": False}},
    ):
        response = c.patch("/wechat/api/v1/settings", json=invalid, headers=headers)
        assert response.status_code == 400
        assert "must not leak" not in response.text
        assert store.setting("model_key") == encrypted and store.setting("model_revision") == revision
    assert c.post("/wechat/api/v1/models", json={}, headers=headers).status_code == 404
    assert c.get("/wechat/api/v1/media-input/old/ticket").status_code in (401, 404)


async def test_native_pixels_work_without_asr_key_and_gateway_enforces_model(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    config = config_at(tmp_path)
    manager.vault = Vault(config.key_file)
    manager.configure_model({}, api_key="isolated-model-key")
    manager.media = IncomingMedia(manager, config)
    picture = image_bytes("PNG", animated=False)
    download = MediaGateway(picture)
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img/></msg>"})
    await manager.media.prepare(store.query("SELECT * FROM tasks")[0], download)
    context = manager.context(store.query("SELECT * FROM tasks")[0])

    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["messages"][0]["role"] == "system"
        parts = body["messages"][1]["content"]
        assert parts[-1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert all(tool["function"]["name"] != "inspect_media" for tool in body.get("tools", []))
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "红色"}}]}
        )

    assert not store.setting("media_key")
    gateway = Gateway(manager, httpx.MockTransport(handler))
    try:
        await gateway.model({**DEFAULT_MODEL, "model": "retired-model"}, "看图", context)
    finally:
        await gateway.close()
    assert effective_model({"model": "retired-model"})["model"] == "deepseek-flash"


@pytest.mark.parametrize("msg_type", [43, 62])
@pytest.mark.parametrize("quote", [False, True])
async def test_video_is_stored_but_never_downloaded_or_added_to_context(tmp_path, msg_type, quote):
    store, manager, _, _ = setup(tmp_path)
    manager.media = IncomingMedia(manager, config_at(tmp_path))
    gateway = MediaGateway(OSError("video must not download"))
    deliver(store, manager, 1, MsgType=msg_type, Content={"string": "<msg><videomsg/></msg>"})
    assert store.query("SELECT state,reason FROM tasks")[0] == {
        "state": "skipped",
        "reason": "video_disabled",
    }
    assert len(store.query("SELECT * FROM media_resources")) == 1
    text = "只回答文字问题"
    if quote:
        text = "<msg><appmsg><type>57</type><title>只回答文字问题</title><refermsg><svrid>1</svrid></refermsg></appmsg></msg>"
    deliver(store, manager, 2, created=NOW + 3, MsgType=49 if quote else 1, Content={"string": text})
    worker = Worker(manager, gateway, clock=lambda: NOW + 35)
    try:
        await worker.once()
        assert len(gateway.sends) == 1 and gateway.downloads == 0
        assert all(v.get("message_kind") != "video" for v in gateway.contexts[0])
        assert all(not v.get("reference") for v in gateway.contexts[0])
        assert not store.query("SELECT * FROM incoming_media")
        assert not store.query("SELECT * FROM reply_media_dependencies")
        with pytest.raises(ProviderError, match="video_disabled"):
            await manager.media.prepare({"message_id": 1, "account_id": 1, "conversation_id": 1}, gateway)
        with pytest.raises(ProviderError, match="video_disabled"):
            await decode(tmp_path / "no-video", tmp_path, 1, "video", manager.media.config)
        raw = Gateway(manager)
        try:
            with pytest.raises(ProviderError, match="video_disabled"):
                await raw.download_media("device", "video", "<msg/>")
        finally:
            await raw.close()
    finally:
        worker.release()


@pytest.mark.parametrize("old_state", ["ready", "queued"])
async def test_legacy_ready_reply_canceled_and_queued_text_kept(tmp_path, old_state):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    store.query(
        "UPDATE tasks SET state=%s,content='old vision output',trace_json=%s RETURNING id",
        (old_state, json.dumps({"image_policy": "retired-policy"})),
    )
    deliver(store, manager, 2, created=NOW + 4)
    gateway = MediaGateway(b"never download")
    worker = Worker(manager, gateway, clock=lambda: NOW + 35)
    try:
        await worker.once()
        rows = store.query("SELECT state,reason,trace_json FROM tasks ORDER BY ordinal")
        assert rows[0]["state"] == "canceled" and rows[0]["reason"] == "reply_policy_changed"
        assert rows[1]["state"] == "accepted", rows
        assert json.loads(rows[1]["trace_json"])["image_policy"] == VERSION
        assert len(gateway.sends) == 1 and gateway.sends[0][2] != "old vision output"
    finally:
        worker.release()


async def test_voice_inspection_uses_ffmpeg_without_ffprobe(tmp_path):
    config = config_at(tmp_path)
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000)
    raw = output.getvalue()
    path = tmp_path / "voice.wav"
    path.write_bytes(raw)
    assert await inspect_voice(raw, path, config) == ("wav", 1000)
    assert not hasattr(config, "ffprobe_binary")


@pytest.mark.parametrize("msg_type", [43, 62])
async def test_group_pat_cannot_read_even_a_legacy_cached_video(tmp_path, msg_type):
    store, manager, _, _ = group_setup(tmp_path)
    manager.media = IncomingMedia(manager, config_at(tmp_path))
    deliver(
        store,
        manager,
        1,
        peer=GROUP,
        MsgType=msg_type,
        Content={"string": "wxid_friend:\n<msg><videomsg/></msg>"},
    )
    store.query(
        "INSERT INTO incoming_media(message_id,state,kind,frames_json,updated_at) VALUES(1,'ready','video','[{\"message_id\":1}]',%s) RETURNING message_id",
        (NOW,),
    )
    assert not manager.media.frames(1)
    pat(store, manager, mid=2, created=NOW + 4)
    gateway = MediaGateway(b"must not download video")
    worker = Worker(manager, gateway, clock=lambda: NOW + 35)
    try:
        await worker.once()
        assert gateway.downloads == 0 and len(gateway.sends) == 1
        assert all(value.get("message_kind") != "video" for value in gateway.contexts[0])
        assert not store.query("SELECT * FROM reply_media_dependencies")
        assert store.query("SELECT kind,state FROM incoming_media WHERE message_id=1")[0] == {
            "kind": "video",
            "state": "ready",
        }
    finally:
        worker.release()
