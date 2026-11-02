import io
import json

import httpx
import pytest
from business_fixtures import allow_reply, phone_takeover
from PIL import Image
from qingda.config import Config
from qingda.providers import Gateway as RealGateway
from qingda.security import Vault, create_key
from qingda.worker import Worker
from test_automation import NOW, deliver, setup
from test_media import MediaGateway as ToolMediaGateway
from test_worker import Gateway


@pytest.mark.parametrize(
    "msg_type,body",
    [
        (3, "<msg><img /></msg>"),
        (34, '<msg><voicemsg voicelength="2000" /></msg>'),
        (
            49,
            "<msg><appmsg><type>57</type><title>引用后的问题</title><refermsg><svrid>123</svrid></refermsg></appmsg></msg>",
        ),
    ],
)
def test_supported_media_and_quoted_text_enter_auto_queue(tmp_path, msg_type, body):
    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1, MsgType=msg_type, Content={"string": body})
    assert len(store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")) == 1
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "queued"
    )
    deliver(store, manager, 1, MsgType=msg_type, Content={"string": body})
    assert len(store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")) == 1


def test_phone_media_takes_over_and_cancels_unsent_auto(tmp_path):
    store, manager, _, conv = setup(tmp_path)
    deliver(store, manager, 1)
    deliver(store, manager, 2, sender="wxid_me", ToUserName={"string": "friend"}, MsgType=3)
    assert manager.conversation(conv["id"])["pause_until"] == NOW + 1802
    assert (
        next(
            t
            for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["message_id"] == 1
        )["state"]
        == "canceled"
    )
    assert store.event(2)["Data"]["MsgType"] == 3


def test_excluded_group_emoji_retains_actual_sender_and_structured_mentions(tmp_path):
    store, manager, account, _ = setup(tmp_path)
    group = manager.ensure_conversation(account["id"], "group@chatroom")
    allow_reply(manager, group["id"], True)
    deliver(
        store,
        manager,
        1,
        peer="group@chatroom",
        MsgType=47,
        Content={
            "string": 'member-a:\n<msg><emoji md5="4cc7540a85b5b6cf4ba14e9f4ae08b7c" len="123" /></msg>'
        },
        source="<msgsource><atuserlist><![CDATA[wxid_me,member-b]]></atuserlist></msgsource>",
    )
    message = store.query("SELECT * FROM messages")[0]
    metadata = json.loads(message["metadata"])
    assert message["sender"] == "member-a" and message["text"] == ""
    assert metadata["mentions"] == ["wxid_me", "member-b"] and metadata["mentioned"]
    assert metadata["message_kind"] == "emoji" and metadata["emoji_size"] == 123
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "skipped"
    )

    assert store.query("SELECT reason FROM tasks")[0]["reason"] == "custom_emoji_disabled"


async def test_worker_downloads_image_and_reuses_persisted_media(tmp_path):
    from qingda.media import IncomingMedia

    store, manager, _, _ = setup(tmp_path)
    manager.media = IncomingMedia(manager, Config(tmp_path, tmp_path / "key"))
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "green").save(output, format="PNG")

    class MediaGateway(ToolMediaGateway):
        async def download_media(self, app_id, kind, xml, msg_id, md5, **kwargs):
            assert app_id == "device-a" and kind == "image" and "<img" in xml
            self.downloads += 1
            return output.getvalue()

        async def model(self, config, prompt, context):
            self.contexts.append(context)
            assert context[-1]["images"][0]["message_id"] == 1
            return await super().model(config, prompt, context)

    gateway = MediaGateway(output.getvalue())
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img /></msg>"})
    worker = Worker(manager, gateway, clock=lambda: NOW + 15)
    await worker.once()
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "accepted"
    )
    assert gateway.downloads == 1 and len(gateway.sends) == 1
    assert gateway.contexts[0][-1]["images"][0]["message_id"] == 1
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img /></msg>"})
    await worker.once()
    assert gateway.downloads == 1 and len(gateway.sends) == 1
    worker.release()
    restarted = IncomingMedia(manager, manager.media.config)
    await restarted.prepare(
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0], gateway
    )
    assert gateway.downloads == 1
    assert (
        "base64"
        not in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0][
            "context_json"
        ]
    )


async def test_deepseek_receives_image_bytes_and_explicit_history_current(tmp_path):
    from qingda.media import IncomingMedia

    store, manager, _, _ = setup(tmp_path)
    create_key(tmp_path / "key")
    manager.vault = Vault(tmp_path / "key")
    store.set_setting("model_key", manager.vault.encrypt("test-key"))
    manager.media = IncomingMedia(manager, Config(tmp_path, tmp_path / "key"))
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(image, "PNG")

    class Download:
        async def download_media(self, *args, **kwargs):
            return image.getvalue()

    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img /></msg>"})
    await manager.media.prepare(
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0], Download()
    )
    context = manager.context(
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]
    )

    def handler(request):
        body = json.loads(request.content)
        parts = body["messages"][-1]["content"]
        assert isinstance(parts, list)
        assert json.loads(parts[0]["text"])["current_messages"][0]["content"] == "[图片]"
        assert "current_message" in parts[1]["text"]
        assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "红色图片"}}]}
        )

    gateway = RealGateway(manager, transport=httpx.MockTransport(handler))
    await gateway.model(store.setting("model"), "测试", context)
    await gateway.close()


async def test_voice_transcript_is_persisted_and_only_text_is_replied(tmp_path):
    from qingda.media import IncomingMedia

    store, manager, _, _ = setup(tmp_path)
    audio = b"\x02#!SILK_V3" + b"\x01\x00x" * 100

    class Transcriber:
        calls = 0

        async def transcribe(self, body, voice_format):
            self.calls += 1
            assert body == audio and voice_format == "silk"
            return {"text": "明天下午三点开会", "request_id": "isolated", "duration_ms": 2000}

    class VoiceGateway(Gateway):
        async def download_media(self, *args, **kwargs):
            return audio

        async def model(self, config, prompt, context):
            assert context[-1]["content"] == "[语音转写] 明天下午三点开会"
            return await super().model(config, prompt, context)

    transcriber = Transcriber()
    config = Config(tmp_path, tmp_path / "key")
    manager.media = IncomingMedia(manager, config, transcriber)
    deliver(
        store,
        manager,
        1,
        MsgType=34,
        MsgId=123,
        Content={"string": '<msg><voicemsg voicelength="2000" /></msg>'},
    )
    gateway = VoiceGateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 15)
    await worker.once()
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "accepted"
        and len(gateway.sends) == 1
    )
    restarted = IncomingMedia(manager, config, transcriber)
    await restarted.prepare(
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0], gateway
    )
    assert transcriber.calls == 1
    assert restarted.record(1)["transcript"] == "明天下午三点开会"
    worker.release()


@pytest.mark.parametrize(
    "extra",
    [
        {
            "MsgType": 49,
            "Content": {"string": "<msg><appmsg><type>6</type><title>资料.pdf</title></appmsg></msg>"},
        },
        {"MsgType": 3, "IsHistory": True},
        {"MsgType": 34, "IsTest": True},
    ],
)
async def test_skipped_media_never_downloads_or_calls_a_model(tmp_path, extra):
    from qingda.media import IncomingMedia

    store, manager, _, _ = setup(tmp_path)
    manager.media = IncomingMedia(manager, Config(tmp_path, tmp_path / "key"))

    class Forbidden(Gateway):
        async def download_media(self, *args, **kwargs):
            raise AssertionError("不允许下载")

        async def model(self, *args):
            raise AssertionError("不允许调用模型")

    deliver(store, manager, 1, **extra)
    gateway = Forbidden()
    worker = Worker(manager, gateway, clock=lambda: NOW + 15)
    await worker.once()
    assert not gateway.sends
    assert not store.query("SELECT * FROM incoming_media")
    assert all(
        t["state"] == "skipped"
        for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    )
    worker.release()


async def test_custom_emoji_never_downloads_or_calls_models(tmp_path):
    from test_emoji_policy import emoji, fixture, image_bytes

    store, manager, gateway, worker, _ = fixture(tmp_path, image_bytes())
    emoji(store, manager)
    try:
        await worker.once()
        assert store.query("SELECT state,reason FROM tasks")[0] == {
            "state": "skipped",
            "reason": "custom_emoji_disabled",
        }
        assert not gateway.downloads and not gateway.sends
        assert not store.query("SELECT * FROM incoming_media")
    finally:
        worker.release()


@pytest.mark.parametrize(
    "msg_type,body",
    [
        (3, b"broken image"),
        (34, b"\x02#!SILK_V3\x10\x00x"),
        (34, b"x" * (3 * 1024 * 1024 + 1)),
        (34, b"\x02#!SILK_V3" + b"\x01\x00x" * 3001),
    ],
    ids=["broken-image", "incomplete-silk", "voice-over-3mb", "voice-over-60s"],
)
async def test_damaged_or_oversized_media_is_marked_missing_without_asr(tmp_path, msg_type, body):
    from qingda.media import IncomingMedia

    store, manager, _, _ = setup(tmp_path)

    class NeverTranscribe:
        async def transcribe(self, *args):
            pytest.fail("损坏或超限素材不应转写")

    class InvalidGateway(Gateway):
        model_calls = 0

        async def download_media(self, *args, **kwargs):
            return body

        async def model(self, *args):
            self.model_calls += 1
            return await super().model(*args)

    manager.media = IncomingMedia(manager, Config(tmp_path, tmp_path / "key"), NeverTranscribe())
    deliver(store, manager, 1, MsgType=msg_type, Content={"string": "<msg />"})
    gateway = InvalidGateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 15)
    await worker.once()
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "accepted"
    )
    assert manager.media.record(1)["state"] == "failed"
    context = manager.context(
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]
    )
    assert context[-1]["media_status"] == ("decode_failed" if msg_type == 3 else "unavailable")
    assert len(gateway.sends) == gateway.model_calls == 1
    worker.release()


async def test_missing_asr_credentials_marks_voice_missing_and_other_tasks_continue(tmp_path):
    from qingda.media import IncomingMedia

    store, manager, _, _ = setup(tmp_path)
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(image, "PNG")

    class ImageGateway(Gateway):
        downloads = []

        async def download_media(self, app_id, kind, *args, **kwargs):
            self.downloads.append(kind)
            return image.getvalue()

    manager.media = IncomingMedia(manager, Config(tmp_path, tmp_path / "key"))
    other = manager.ensure_conversation(1, "friend-two")
    allow_reply(manager, other["id"], True)
    deliver(store, manager, 1, MsgType=34)
    deliver(store, manager, 2, peer="friend-two", MsgType=3, created=NOW + 2)
    deliver(store, manager, 3, peer="friend-two", created=NOW + 3)
    gateway = ImageGateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + 15)
    for _ in range(3):
        await worker.once()
    tasks = {
        t["message_id"]: t
        for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    }
    assert tasks[1]["state"] == "accepted"
    assert manager.context(tasks[1])[-1]["media_status"] == "unavailable"
    assert tasks[2]["state"] == "accepted" and tasks[3]["state"] == "merged"
    assert gateway.downloads == ["image"] and len(gateway.sends) == 2
    worker.release()


@pytest.mark.parametrize("blocked", ["account", "whitelist", "group", "expired"])
async def test_media_guard_rejection_prevents_download_and_generation(tmp_path, blocked):
    from qingda.media import IncomingMedia

    store, manager, account, conv = setup(tmp_path)
    peer = "friend"
    if blocked == "account":
        manager.configure_automation(account["id"], prompt="测试", ai_enabled=False)
    elif blocked == "whitelist":
        allow_reply(manager, conv["id"], False)
    elif blocked == "group":
        peer = "group@chatroom"
        group = manager.ensure_conversation(account["id"], peer)
        allow_reply(manager, group["id"], True)

    class GuardedGateway(Gateway):
        calls = []

        async def download_media(self, *args, **kwargs):
            self.calls.append("download")
            return b""

        async def model(self, *args):
            self.calls.append("model")
            return await super().model(*args)

    manager.media = IncomingMedia(manager, Config(tmp_path, tmp_path / "key"))
    deliver(store, manager, 1, peer=peer, MsgType=3)
    gateway = GuardedGateway()
    worker = Worker(manager, gateway, clock=lambda: NOW + (400 if blocked == "expired" else 5))
    await worker.once()
    assert not gateway.calls and not gateway.sends
    assert not store.query("SELECT * FROM incoming_media")
    assert store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"] in (
        "skipped",
        "canceled",
    )
    worker.release()


async def test_takeover_during_voice_download_cancels_before_asr_and_model(tmp_path):
    from qingda.media import IncomingMedia

    store, manager, _, conv = setup(tmp_path)

    class NeverTranscribe:
        async def transcribe(self, *args):
            pytest.fail("接管后不应转写")

    class TakingOver(Gateway):
        model_calls = 0

        async def download_media(self, *args, **kwargs):
            phone_takeover(manager, conv["id"], "由人工处理", "0f1d41a0-c801-4ce7-9e8f-8b0b63d84cb7")
            return b"\x02#!SILK_V3" + b"\x01\x00x" * 100

        async def model(self, *args):
            self.model_calls += 1
            return await super().model(*args)

    manager.media = IncomingMedia(manager, Config(tmp_path, tmp_path / "key"), NeverTranscribe())
    deliver(store, manager, 1, MsgType=34)
    gateway = TakingOver()
    worker = Worker(manager, gateway, clock=lambda: NOW + 15)
    await worker.once()
    assert (
        next(
            t
            for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["kind"] == "auto"
        )["state"]
        == "canceled"
    )
    assert not gateway.sends and gateway.model_calls == 0
    worker.release()
