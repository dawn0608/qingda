"""服务器隔离验收：微信内置表情、静态图片准入及自定义表情全路径排除。"""

import hashlib
import io
import json
import zipfile

import pytest
from business_fixtures import allow_reply
from PIL import Image
from test_automation import NOW, deliver, setup
from test_media import MediaGateway
from wechat_manager.config import Config
from wechat_manager.media import IncomingMedia
from wechat_manager.worker import Worker


def image_bytes(fmt="GIF", animated=True, zipped=False):
    output = io.BytesIO()
    frames = [Image.new("RGB", (16, 16), c) for c in ("red", "green", "blue")]
    frames[0].save(
        output,
        fmt,
        save_all=animated,
        **({"append_images": frames[1:], "duration": 100, "loop": 0} if animated else {}),
    )
    body = output.getvalue()
    if zipped:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("sticker.bin", body)
        body = output.getvalue()
    return body


class EmojiGateway(MediaGateway):
    def __init__(self, body):
        super().__init__(body)

    async def download_media(self, *args, **kwargs):
        self.downloads += 1
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def fixture(tmp_path, body, group=False):
    store, manager, account, conv = setup(tmp_path)
    manager.media = IncomingMedia(manager, Config(tmp_path, tmp_path / "key"))
    peer = "room@chatroom" if group else "friend"
    if group:
        conv = manager.ensure_conversation(account["id"], peer)
        allow_reply(manager, conv["id"], True)
    gateway = EmojiGateway(body)
    worker = Worker(manager, gateway, clock=lambda: NOW + 35)
    return store, manager, gateway, worker, peer


def emoji(store, manager, mid=1, peer="friend", mentioned=True, created=None, digest="a" * 32):
    group = peer.endswith("@chatroom")
    deliver(
        store,
        manager,
        mid,
        peer=peer,
        MsgType=47,
        created=created,
        source="<msgsource><atuserlist>wxid_me</atuserlist></msgsource>" if group and mentioned else "",
        Content={"string": ("member:\n" if group else "") + '<msg><emoji md5="' + digest + '" /></msg>'},
    )


@pytest.mark.parametrize(
    "body", [image_bytes(), image_bytes(animated=False), b"broken", OSError("must not download")]
)
@pytest.mark.parametrize("group", [False, True])
async def test_all_custom_emoji_skip_before_download_or_models(tmp_path, body, group):
    store, manager, gateway, worker, peer = fixture(tmp_path, body, group)
    emoji(store, manager, peer=peer)
    try:
        await worker.once()
        assert store.query("SELECT state,reason FROM tasks")[0] == {
            "state": "skipped",
            "reason": "custom_emoji_disabled",
        }
        assert not gateway.downloads and not gateway.contexts and not gateway.sends
        assert (
            len(store.query("SELECT * FROM messages"))
            == len(store.query("SELECT * FROM media_resources"))
            == 1
        )
        assert not store.query("SELECT * FROM incoming_media")
    finally:
        worker.release()


@pytest.mark.parametrize("group", [False, True])
async def test_custom_emoji_does_not_delay_or_enter_text_batch(tmp_path, group):
    store, manager, gateway, worker, peer = fixture(tmp_path, image_bytes(), group)
    emoji(store, manager, peer=peer)
    deliver(
        store,
        manager,
        2,
        peer=peer,
        created=NOW + 3,
        source="<msgsource><atuserlist>wxid_me</atuserlist></msgsource>" if group else "",
        Content={"string": ("member:\n" if group else "") + "请回答文字问题"},
    )
    try:
        await worker.once()
        assert sorted(r["state"] for r in store.query("SELECT state FROM tasks")) == ["accepted", "skipped"]
        assert gateway.downloads == 0 and len(gateway.sends) == 1
        assert all(v.get("message_kind") != "emoji" for v in gateway.contexts[0])
        assert "请回答文字问题" in json.dumps(gateway.contexts, ensure_ascii=False)
    finally:
        worker.release()


@pytest.mark.parametrize("fmt", ["GIF", "PNG", "JPEG", "WEBP"])
async def test_static_picture_reaches_deepseek_and_replies(tmp_path, fmt):
    store, manager, gateway, worker, peer = fixture(tmp_path, image_bytes(fmt, animated=False))
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img/></msg>"})
    try:
        await worker.once()
        assert store.query("SELECT state FROM tasks")[0]["state"] == "accepted"
        assert len(gateway.sends) == len(gateway.contexts) == 1
        assert manager.media.record(1)["state"] == "ready"
        assert gateway.contexts[0][-1]["images"]
    finally:
        worker.release()


@pytest.mark.parametrize("fmt", ["GIF", "PNG", "WEBP"])
@pytest.mark.parametrize("group", [False, True])
async def test_animated_picture_is_excluded_without_any_model_call(tmp_path, fmt, group):
    store, manager, gateway, worker, peer = fixture(tmp_path, image_bytes(fmt), group)
    deliver(
        store,
        manager,
        1,
        peer=peer,
        MsgType=3,
        source="<msgsource><atuserlist>wxid_me</atuserlist></msgsource>" if group else "",
        Content={"string": ("member:\n" if group else "") + "<msg><img/></msg>"},
    )
    try:
        await worker.once()
        assert store.query("SELECT state,reason FROM tasks")[0] == {
            "state": "skipped",
            "reason": "animated_emoji_disabled",
        }
        assert not gateway.contexts and not gateway.sends
        assert manager.media.record(1)["state"] == "skipped"
        assert store.query("SELECT state FROM media_resources")[0]["state"] != "unavailable"
        assert not manager.media.frames(1)
    finally:
        worker.release()


async def test_native_text_emoji_keeps_existing_semantics(tmp_path):
    from wechat_manager.private_context import emotions

    token, meaning = next(iter(emotions().items()))
    store, manager, gateway, worker, peer = fixture(tmp_path, OSError("must not download"))
    deliver(store, manager, 1, Content={"string": "今天开心 " + token})
    try:
        await worker.once()
        assert len(gateway.sends) == 1 and not gateway.downloads
        current = gateway.contexts[0][-1]
        assert meaning in current["content"] and token in current.get("original_content", current["content"])
    finally:
        worker.release()


async def test_old_custom_emoji_answer_and_quote_do_not_reenter_context(tmp_path):
    body = image_bytes(animated=False)
    store, manager, gateway, worker, peer = fixture(tmp_path, body)
    emoji(store, manager)
    manager.media.raw_path(1).write_bytes(body)
    store.query(
        "INSERT INTO incoming_media(message_id,state,kind,sha256,updated_at) VALUES(1,'ready','emoji',%s,%s) RETURNING message_id",
        (hashlib.sha256(body).hexdigest(), NOW),
    )
    store.query(
        "UPDATE tasks SET state='accepted',handled_message_ids='[1]',content='OLD_CUSTOM_ONLY',response_json=%s,completed_at=%s RETURNING id",
        (json.dumps({"data": {"newMsgId": "99"}}), NOW + 4),
    )
    deliver(
        store,
        manager,
        2,
        created=NOW + 10,
        MsgType=49,
        Content={
            "string": "<msg><appmsg><type>57</type><title>继续聊文字</title><refermsg><svrid>99</svrid></refermsg></appmsg></msg>"
        },
    )
    try:
        await worker.once()
        assert len(gateway.sends) == 1 and gateway.downloads == 0
        assert "OLD_CUSTOM_ONLY" not in json.dumps(gateway.contexts)
        assert not manager.media.frames(1)
        assert all(v.get("message_kind") != "emoji" and "reference" not in v for v in gateway.contexts[0])
    finally:
        worker.release()


async def test_recent_custom_and_quoted_custom_never_become_media_dependencies(tmp_path):
    store, manager, gateway, worker, peer = fixture(tmp_path, image_bytes(animated=False), True)
    emoji(store, manager, peer=peer, mentioned=False)
    deliver(
        store,
        manager,
        2,
        peer=peer,
        MsgType=3,
        created=NOW + 1,
        Content={"string": "member:\n<msg><img/></msg>"},
    )
    deliver(
        store,
        manager,
        3,
        peer=peer,
        created=NOW + 2,
        source="<msgsource><atuserlist>wxid_me</atuserlist></msgsource>",
        Content={"string": "member:\n@WeMind 说明图片"},
    )
    try:
        await worker.once()
        assert len(gateway.sends) == gateway.downloads == 1
        assert {
            r["source_message_id"]
            for r in store.query("SELECT source_message_id FROM reply_media_dependencies")
        } == {2}
        assert "emoji" not in json.dumps(gateway.contexts)
    finally:
        worker.release()


@pytest.mark.parametrize("with_trace", [False, True])
async def test_legacy_animated_picture_cache_cannot_enter_context(tmp_path, with_trace):
    from wechat_manager.private_context import input_allowed

    body = image_bytes()
    store, manager, gateway, worker, peer = fixture(tmp_path, body)
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img/></msg>"})
    manager.media.raw_path(1).write_bytes(body)
    store.query(
        "INSERT INTO incoming_media(message_id,state,kind,sha256,trace_json,updated_at) VALUES(1,'ready','image',%s,%s,%s) RETURNING message_id",
        (
            hashlib.sha256(body).hexdigest(),
            json.dumps({"frame_count": 3, "animated": True} if with_trace else {}),
            NOW,
        ),
    )
    try:
        row = store.query("SELECT * FROM messages WHERE id=1")[0]
        assert not input_allowed(manager, row) and not manager.media.frames(1)
        assert not gateway.downloads
    finally:
        worker.release()


async def test_quoting_custom_emoji_cannot_trigger_old_static_picture_understanding(tmp_path):
    store, manager, gateway, worker, _ = fixture(tmp_path, image_bytes(animated=False))
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img/></msg>"})
    try:
        await worker.once()
        assert len(gateway.contexts) == 1
        emoji(store, manager, 2, created=NOW + 45)
        deliver(
            store,
            manager,
            3,
            created=NOW + 46,
            MsgType=49,
            Content={
                "string": "<msg><appmsg><type>57</type><title>这是什么</title><refermsg><svrid>2</svrid></refermsg></appmsg></msg>"
            },
        )
        worker.clock = lambda: NOW + 75
        await worker.once()
        assert len(gateway.contexts) == 2 and gateway.downloads == 1
        current = next(v for v in reversed(gateway.contexts[-1]) if v.get("current"))
        assert not current.get("reference")
        assert not any(value.get("images") for value in gateway.contexts[-1])
    finally:
        worker.release()


async def test_dynamic_picture_is_removed_while_mixed_text_continues(tmp_path):
    store, manager, gateway, worker, _ = fixture(tmp_path, image_bytes())
    deliver(store, manager, 1, MsgType=3, Content={"string": "<msg><img/></msg>"})
    deliver(store, manager, 2, created=NOW + 3, Content={"string": "只回答这条文字"})
    try:
        await worker.once()
        await worker.once()
        assert sorted(r["state"] for r in store.query("SELECT state FROM tasks")) == ["accepted", "skipped"]
        assert len(gateway.sends) == 1
        assert all(v.get("message_kind") != "image" for v in gateway.contexts[-1])
        assert "只回答这条文字" in json.dumps(gateway.contexts, ensure_ascii=False)
    finally:
        worker.release()
