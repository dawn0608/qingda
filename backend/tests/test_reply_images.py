"""真实引用漏下载案例的服务器隔离回归。"""

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image
from test_automation import NOW, deliver, setup
from test_group_wakeup import GROUP, group_setup, text
from test_media import MediaGateway
from wechat_manager import reply_queue
from wechat_manager.config import Config
from wechat_manager.media import IncomingMedia
from wechat_manager.providers import Gateway as RealGateway
from wechat_manager.providers import ProviderError
from wechat_manager.worker import Worker


def png():
    out = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(out, format="PNG")
    return out.getvalue()


class Images(MediaGateway):
    def __init__(self):
        super().__init__(png())
        self.downloads, self.contexts = [], []

    async def download_media(self, app_id, kind, xml, *args, **kwargs):
        self.downloads.append(kwargs["message_id"])
        assert "<img" in xml
        return png()

    async def model(self, config, prompt, context):
        self.contexts.append(context)
        return await super().model(config, prompt, context)


def media(manager, tmp_path):
    manager.media = IncomingMedia(manager, Config(tmp_path, tmp_path / "key"))


def picture(s, m, mid, *, group=True, actor="wxid_friend", created=NOW + 1, received=None):
    deliver(
        s,
        m,
        mid,
        peer=GROUP if group else "friend",
        MsgType=3,
        created=created,
        received=received,
        Content={"string": (actor + ":\n" if group else "") + '<msg><img cdnmidimgurl="opaque" /></msg>'},
    )


def quote(s, m, mid, ref, *, group=True, created=NOW + 5, received=None):
    deliver(
        s,
        m,
        mid,
        peer=GROUP if group else "friend",
        MsgType=49,
        created=created,
        received=received,
        source="<msgsource><atuserlist>wxid_me</atuserlist></msgsource>" if group else "",
        Content={
            "string": ("wxid_friend:\n" if group else "")
            + f"<msg><appmsg><type>57</type><title>WeMind，分析这张图片</title><refermsg><svrid>{ref}</svrid><content>伪造正文不可采信</content></refermsg></appmsg></msg>"
        },
    )


@pytest.mark.parametrize("group,other", [(True, False), (True, True), (False, False)])
async def test_unprepared_quote_is_downloaded_and_attributed(tmp_path, group, other):
    s, m, _, _ = group_setup(tmp_path) if group else setup(tmp_path)
    media(m, tmp_path)
    picture(s, m, 100, group=group, actor="other-member" if other else "wxid_friend")
    if not group:
        with s.connect() as c:
            c.execute("UPDATE tasks SET state='skipped',reason='test_original_not_processed'")
    quote(s, m, 101, 100, group=group)
    g = Images()
    w = Worker(m, g, clock=lambda: NOW + 20)
    try:
        await w.once()
        assert g.downloads == [1] and len(g.sends) == 1
        current = [v for v in g.contexts[-1] if v.get("current")][-1]
        assert m.media.record(1)["state"] == "ready"
        assert g.contexts[0][-1]["images"][0]["message_id"] == 1
        assert current["images"][0]["source_type"] == "quote"
        assert current["reference"]["sender"] == (
            "other-member" if other else "wxid_friend" if group else "friend"
        )
        assert current["reply_target"] == ("wxid_friend" if group else "friend")
        assert json.loads(
            s.query("SELECT handled_message_ids FROM tasks WHERE handled_message_ids != '[]'::jsonb")[0][
                "handled_message_ids"
            ]
        ) == [2]
        assert len(s.query("SELECT * FROM reply_media_dependencies")) == 1
        await m.media.prepare(
            {
                "message_id": 1,
                "account_id": 1,
                "conversation_id": s.query("SELECT conversation_id FROM messages WHERE id=1")[0][
                    "conversation_id"
                ],
            },
            g,
        )
        assert g.downloads == [1]
        real = RealGateway(m)
        try:
            parts = await real.model_input(g.contexts[-1])
            assert isinstance(parts, list)
            assert parts[-1]["image_url"]["url"].startswith("data:image/png;base64,")
        finally:
            await real.client.aclose()
    finally:
        w.release()


async def test_recent_group_images_select_only_current_member_up_to_five(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    media(m, tmp_path)
    picture(s, m, 1, created=NOW - 200)
    picture(s, m, 2, actor="other-member")
    for mid in range(3, 10):
        picture(s, m, mid, created=NOW + mid, received=NOW + mid + 1)
    text(s, m, 10, "WeMind，比较这些图片", created=NOW + 12)
    g = Images()
    w = Worker(m, g, clock=lambda: NOW + 25)
    try:
        await w.once()
        assert g.downloads == [5, 6, 7, 8, 9]
        current = [v for v in g.contexts[-1] if v.get("current")][-1]
        assert len(current["recent_images"]) == 5 and len(g.sends) == 1
    finally:
        w.release()


@pytest.mark.parametrize(
    "question,expected",
    [
        ("你好", False),
        ("发完了，只输出合计数字", False),
        ("这道题的金额再解释一下", False),
        ("刚才的文字换个说法", False),
        ("电脑如何截图？", False),
        ("只输出图片中的数字", True),
        ("这张图是什么颜色？", True),
        ("我刚发的图片里人物头发是什么颜色？", True),
    ],
)
async def test_private_history_pixels_require_explicit_material_reference(tmp_path, question, expected):
    s, m, _, _ = setup(tmp_path)
    media(m, tmp_path)
    picture(s, m, 100, group=False)
    g = Images()
    w = Worker(m, g, clock=lambda: NOW + 20)
    try:
        await w.once()
        assert g.contexts[-1][-1].get("images")
        deliver(s, m, 101, Content={"string": question}, created=NOW + 25)
        w.clock = lambda: NOW + 30
        await w.once()
        assert len(g.sends) == 2
        selected = [v for v in g.contexts[-1] if v.get("images")]
        assert bool(selected) is expected
        if expected:
            assert [v["message_id"] for v in selected] == [1]
        else:
            assert any(v.get("media_status") == "historical_image_omitted" for v in g.contexts[-1])
    finally:
        w.release()


@pytest.mark.parametrize("mode", ["direct", "quote", "foreign", "revoked", "old_quote", "late"])
async def test_source_priority_isolation_and_callback_order(tmp_path, mode):
    s, m, _, conv = group_setup(tmp_path)
    media(m, tmp_path)
    picture(s, m, 100)
    if mode == "foreign":
        quote(s, m, 101, 100, group=False)
    elif mode == "direct":
        text(s, m, 101, "WeMind，分析接下来的图片", created=NOW + 5)
        picture(s, m, 102, created=NOW + 6)
    elif mode == "late":
        quote(s, m, 101, 102, created=NOW + 5, received=NOW + 7)
        picture(s, m, 102, created=NOW + 4, received=NOW + 8)
    else:
        quote(s, m, 101, 100, created=NOW + (150 if mode == "old_quote" else 5))
    if mode == "revoked":
        from wechat_manager.callbacks import message_key, put

        with s.connect() as c:
            put(c, message_key(1), 1, conv["id"], revoked=True, event_id=1)
    g = Images()
    w = Worker(m, g, clock=lambda: NOW + (170 if mode == "old_quote" else 20))
    try:
        await w.once()
        expected = [] if mode in ("foreign", "revoked") else [3] if mode in ("direct", "late") else [1]
        assert g.downloads == expected
        if mode == "late":
            assert (
                next(v for v in g.contexts[-1] if v.get("current") and v.get("reference"))["reference"][
                    "message_id"
                ]
                == 3
            )
        if mode in ("foreign", "revoked"):
            assert not any(v.get("images") for v in g.contexts[-1] if v.get("current"))
    finally:
        w.release()


async def test_revoke_during_download_discards_result_and_rebuilds(tmp_path):
    s, m, _, conv = group_setup(tmp_path)
    media(m, tmp_path)
    picture(s, m, 100)
    quote(s, m, 101, 100)
    g = Images()
    original = g.download_media

    async def revoke(*args, **kwargs):
        from wechat_manager.callbacks import message_key, put

        with s.connect() as c:
            put(c, message_key(1), 1, conv["id"], revoked=True, event_id=1)
        return await original(*args, **kwargs)

    g.download_media = revoke
    w = Worker(m, g, clock=lambda: NOW + 20)
    try:
        await w.once()
        assert not g.sends and not g.contexts
        assert m.media.record(1)["state"] != "ready"
        await w.once()
        assert len(g.sends) == 1
        assert not any(v.get("images") for v in g.contexts[-1])
    finally:
        w.release()


@pytest.mark.parametrize("failure", ["rejected", "invalid", "transient", "no_other_spec"])
async def test_image_spec_fallback_and_bounded_retry(failure):
    g = RealGateway(None)
    calls = []

    async def call(path, payload, **kw):
        calls.append(payload["type"])
        if failure == "transient" and len(calls) == 1:
            raise ProviderError("platform_http_5xx")
        if payload["type"] == 2 and failure in ("rejected", "no_other_spec"):
            return {"ret": 500}
        return {"ret": 200, "data": {"fileUrl": str(payload["type"])}}

    async def fetch(url, *args, **kwargs):
        return b"not an image" if failure == "invalid" and url == "2" else png()

    g.call, g.fetch_media = call, fetch
    try:
        xml = '<msg><img cdnmidimgurl="x" cdnbigimgurl="y" cdnthumburl="z" /></msg>'
        if failure == "no_other_spec":
            with pytest.raises(ProviderError, match="platform_download_rejected"):
                await g.download_image("test", "<msg><img /></msg>")
            assert calls == [2]
        else:
            body = await g.download_image("test", xml)
            assert calls == ([2, 2] if failure == "transient" else [2, 1])
            assert body.image_type == (2 if failure == "transient" else 1)
    finally:
        await g.client.aclose()


def test_capacity_marks_omissions_and_preserves_question(tmp_path, monkeypatch):
    class Media:
        def path(self, ref):
            p = tmp_path / str(ref["message_id"])
            p.write_bytes(b"x" * 100)
            return p

    monkeypatch.setattr(reply_queue, "IMAGE_LIMIT", 1500)
    context = [
        {
            "role": "user",
            "content": "分析当前图片",
            "current": True,
            "message_id": 2,
            "recent_images": [{"message_id": 1}],
            "images": [
                {"message_id": 1, "source_type": "recent"},
                {"message_id": 2, "source_type": "direct"},
            ],
        }
    ]
    fitted = reply_queue.fit_context(context, Media())
    assert fitted[0]["content"] == "分析当前图片" and len(fitted[0]["images"]) == 1
    assert fitted[0]["recent_images"][0]["media_status"] == "omitted_capacity"
    monkeypatch.setattr(reply_queue, "IMAGE_LIMIT", 1000)
    fitted = reply_queue.fit_context(context, Media())
    assert fitted[0]["media_status"] == "omitted_capacity" and not fitted[0]["images"]


@pytest.mark.parametrize("mode", ["revision", "crash", "crash_revoke", "crash_send"])
def test_two_process_image_recovery_and_unknown_send(tmp_path, mode):
    s, m, _, conv = group_setup(tmp_path)
    picture(s, m, 100)
    quote(s, m, 101, 100)
    actor = Path(__file__).with_name("image_process_actor.py")
    env = {
        **os.environ,
        "ACTOR_DATABASE_URL": s.url,
        "ACTOR_MEDIA_DIR": str(tmp_path),
        "PGAPPNAME": "wechat-manager-image-actor",
    }
    first = subprocess.Popen(
        [sys.executable, str(actor), "crash_send" if mode == "crash_send" else "hold"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 12
        while not s.query("SELECT * FROM audit WHERE action='image_download_started'"):
            assert first.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        excess = subprocess.run(
            [sys.executable, str(actor), "lease"], env=env, capture_output=True, text=True, timeout=10
        )
        assert excess.returncode == 0 and "lease_busy" in excess.stdout, excess.stderr
        if mode == "revision":
            picture(s, m, 102, created=NOW + 21, received=NOW + 21)
            text(s, m, 103, "更正，分析新发的图片", created=NOW + 22)
        elif mode in ("crash", "crash_revoke"):
            first.kill()
            if mode == "crash_revoke":
                from wechat_manager.callbacks import message_key, put

                with s.connect() as c:
                    put(c, message_key(1), 1, conv["id"], revoked=True, event_id=1)
        first.communicate(timeout=12)
        before = s.query("SELECT * FROM audit WHERE action='image_send_boundary'")
        assert len(before) == (1 if mode == "crash_send" else 0)
        if mode == "revision":
            assert s.query("SELECT state FROM incoming_media")[0]["state"] == "failed"
        resumed = subprocess.run(
            [sys.executable, str(actor), "resume"],
            env={**env, "ACTOR_CLOCK": str(NOW + 150)},
            capture_output=True,
            text=True,
            timeout=25,
        )
        assert resumed.returncode == 0, resumed.stderr
        assert len(s.query("SELECT * FROM audit WHERE action='image_send_boundary'")) == 1
        assert not s.query("SELECT * FROM worker_lock")
        assert not s.query("SELECT * FROM incoming_media WHERE state='downloading'")
        assert (
            s.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name='wechat-manager-image-actor'"
            )[0]["n"]
            == 0
        )
        if mode == "crash_send":
            assert (
                s.query("SELECT state FROM tasks WHERE kind='auto' AND message_id=2")[0]["state"] == "unknown"
            )
        else:
            assert bool(s.query("SELECT * FROM incoming_media WHERE state='ready'")) == (
                mode != "crash_revoke"
            )
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)
