"""窗口与拍一拍协调：服务器隔离库，群事件 274 / 私聊事件 277 脱敏结构。"""

import json

import pytest
from test_automation import NOW, deliver, setup
from test_group_wakeup import GROUP, group_setup, pat, text
from test_worker import Gateway
from wechat_manager.service import Manager
from wechat_manager.worker import Worker


def private_pat(s, m, mid=2, **extra):
    deliver(
        s,
        m,
        mid,
        MsgType=10002,
        Content={
            "string": '<sysmsg type="pat"><pat><fromusername>friend</fromusername>'
            "<chatusername>wxid_me</chatusername><pattedusername>wxid_me</pattedusername>"
            '<template><![CDATA["${friend}" 拍了拍 "${wxid_me}"]]></template></pat></sysmsg>'
        },
        **extra,
    )


def test_window_candidates_are_member_scoped_without_automatic_renewal(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m, mid=1)
    text(s, m, mid=2, body="接着问", created=NOW + 100)
    text(s, m, mid=3, body="继续", created=NOW + 200)
    deliver(s, m, 4, peer=GROUP, Content={"string": "another:\n不要接话"}, created=NOW + 201)
    tasks = {
        t["message_id"]: t
        for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    }
    assert tasks[2]["state"] == "merged" and tasks[3]["state"] == "skipped"
    assert tasks[4]["state"] == "skipped"
    assert (
        json.loads(s.query("SELECT metadata FROM messages WHERE id=2")[0]["metadata"])["wakeup_reason"]
        == "continuation"
    )


async def test_pat_waits_then_answers_previous_and_next_question_once(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m, mid=1, body="第一个问题")
    pat(s, m, mid=2)
    text(s, m, mid=3, body="补充信息")
    assert (
        next(
            t
            for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["message_id"] == 1
        )["state"]
        == "skipped"
    )
    contexts = []
    gateway = Gateway()
    original = gateway.model

    async def model(*args):
        contexts.append(args[-1])
        return await original(*args)

    gateway.model = model
    now = [NOW + 3]
    worker = Worker(m, gateway, clock=lambda: now[0])
    try:
        await worker.once()
        assert not gateway.sends
        now[0] = NOW + 6
        await worker.once()
        assert len(gateway.sends) == 1
        current = [x for x in contexts[0] if x.get("current")]
        assert [x["message_id"] for x in current] == [1, 2, 3]
        assert not any(x.get("content") == "第一个问题" for x in contexts[0] if not x.get("current"))
    finally:
        worker.release()


async def test_private_question_then_pat_does_not_duplicate(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    private_pat(s, m)
    g = Gateway()
    worker = Worker(m, g, clock=lambda: NOW + 6)
    try:
        await worker.once()
        await worker.once()
        assert len(g.sends) == 1
        assert (
            len(
                [
                    x
                    for x in m.context(
                        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[-1]
                    )
                    if x.get("current")
                ]
            )
            == 2
        )
    finally:
        worker.release()


def test_restart_preserves_window_and_idle_expiry(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    m = Manager(s, clock=lambda: NOW + 100)
    text(s, m, mid=2, body="窗口内", created=NOW + 100)
    text(s, m, mid=3, body="窗口外", created=NOW + 224)
    bymid = {
        t["message_id"]: t
        for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    }
    assert bymid[2]["state"] == "merged" and bymid[3]["state"] == "skipped"


def revoke(s, m, mid, target, peer=GROUP):
    deliver(
        s,
        m,
        mid,
        peer=peer,
        MsgType=10002,
        Content={
            "string": f'<sysmsg type="revokemsg"><revokemsg><session>{peer}</session><newmsgid>{target}</newmsgid>'
            "<replacemsg>撤回了一条消息</replacemsg></revokemsg></sysmsg>"
        },
    )


def test_hard_cap_and_other_mentions_do_not_renew(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    for i, at in enumerate(range(1, 602, 100), 1):
        text(s, m, mid=i, created=NOW + at)
    # 明确唤醒也只能开启新的轮次，不能改变上一轮的上限。
    row = s.query("SELECT * FROM dialogue_windows")[0]
    assert row["started_at"] == NOW + 603 and row["hard_deadline"] == NOW + 1203
    text(
        s,
        m,
        mid=8,
        body="给别人",
        source="<msgsource><atuserlist>other</atuserlist></msgsource>",
        created=NOW + 700,
    )
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "skipped"
    )
    text(s, m, mid=9, body="已过空闲窗口", created=NOW + 724)
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "skipped"
    )


def test_lookback_limit_and_no_history_replay(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    for i in range(1, 8):
        text(s, m, mid=i, body=f"问题{i}")
    # 发布前记录不能被新拍一拍重新触发。
    with s.transaction() as c:
        c.execute("UPDATE message_records SET metadata=metadata-'dialogue_version' WHERE id=7")
    pat(s, m, mid=8)
    ids = [
        x["message_id"]
        for x in m.context(s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0])
        if x.get("current")
    ]
    assert ids == [2, 3, 4, 5, 6, 8]
    pat(s, m, mid=9)
    assert len(s.query("SELECT * FROM reply_input_links WHERE kind='backfill'")) == 5


def test_wake_revoke_closes_window_and_cancels_dependents(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m, mid=1)
    text(s, m, mid=2, body="后续问题")
    revoke(s, m, 3, 1)
    assert {
        t["state"] for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    } == {"canceled"}
    text(s, m, mid=4, body="不能继续")
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "skipped"
    )


async def test_revoked_backfill_is_excluded(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m, mid=1, body="不要回答")
    pat(s, m, mid=2)
    revoke(s, m, 3, 1)
    g = Gateway()
    w = Worker(m, g, clock=lambda: NOW + 6)
    try:
        await w.once()
        task = next(
            t
            for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["message_id"] == 2
        )
        assert len(g.sends) == 1
        assert [x["message_id"] for x in m.context(task) if x.get("current")] == [2]
    finally:
        w.release()


@pytest.mark.parametrize("stage", ["generating", "sending", "accepted"])
async def test_private_pat_during_active_or_completed_reply(tmp_path, stage):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    g = Gateway()
    original_model = g.model
    original_send = g.send

    async def model(*args):
        if stage == "generating":
            private_pat(s, m, 2)
            private_pat(s, m, 3)
        return await original_model(*args)

    async def send(*args):
        if stage == "sending":
            private_pat(s, m, 2)
            private_pat(s, m, 3)
        return await original_send(*args)

    g.model = model
    g.send = send
    w = Worker(m, g, clock=lambda: NOW + 6)
    try:
        await w.once()
        if stage == "accepted":
            private_pat(s, m, 2)
        await w.once()
        assert len(g.sends) == 1
        if stage != "accepted":
            assert all(
                t["state"] == "merged"
                for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
                if t["message_id"] != 1
            )
    finally:
        w.release()


async def test_pat_wait_does_not_block_other_conversation(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    pat(s, m)
    deliver(s, m, 3, created=NOW, received=NOW)
    g = Gateway()
    w = Worker(m, g, clock=lambda: NOW + 3)
    try:
        await w.once()
        assert len(g.sends) == 1 and g.sends[0][1] == "friend"
    finally:
        w.release()


async def test_pat_interleaved_member_and_repeat_has_one_fixed_deadline(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    pat(s, m, mid=1)
    deliver(s, m, 2, peer=GROUP, Content={"string": "another:\n别人的背景"})
    text(s, m, mid=3, body="我的问题")
    pat(s, m, mid=4, created=NOW + 3)
    deadline = s.query("SELECT ready_at FROM reply_not_before")[0]["ready_at"]
    assert deadline == NOW + 5
    g = Gateway()
    w = Worker(m, g, clock=lambda: NOW + 6)
    try:
        await w.once()
        await w.once()
        assert len(g.sends) == 1
        leader = next(
            t
            for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["message_id"] == 1
        )
        assert [x["message_id"] for x in m.context(leader) if x.get("current")] == [1, 3]
    finally:
        w.release()


async def test_window_expiry_does_not_cancel_admitted_task(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    g = Gateway()
    w = Worker(m, g, clock=lambda: NOW + 130)
    try:
        await w.once()
        assert len(g.sends) == 1
    finally:
        w.release()


@pytest.mark.parametrize("change", ["takeover", "conversation", "account"])
def test_rule_changes_close_window_and_cancel_unsent(tmp_path, change):
    s, m, a, c = group_setup(tmp_path)
    text(s, m)
    text(s, m, mid=2, body="续聊")
    if change == "takeover":
        with s.transaction() as db:
            m.takeover(c["id"], db, NOW + 3)
    elif change == "conversation":
        m.set_auto_reply(c["id"], False)
    else:
        m.configure_automation(a["id"], ai_enabled=False)
    assert all(
        t["state"] in ("canceled", "merged")
        for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    )
    assert not s.query("SELECT id FROM tasks WHERE state IN ('queued','generating','ready','sending')")
    assert s.query("SELECT closed_reason FROM dialogue_windows")[0]["closed_reason"]
    pat(s, m, mid=3)
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "skipped"
    )


async def test_private_revoked_question_retains_pat_without_original(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    private_pat(s, m, 2)
    revoke(s, m, 3, 1, "friend")
    g = Gateway()
    w = Worker(m, g, clock=lambda: NOW + 6)
    try:
        await w.once()
        assert len(g.sends) == 1
        task = next(
            t
            for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["message_id"] == 2
        )
        assert task["state"] == "accepted"
        assert [x["message_id"] for x in m.context(task) if x.get("current")] == [2]
    finally:
        w.release()


async def test_frozen_pat_does_not_swallow_new_question(tmp_path):
    s, m, _, _ = setup(tmp_path)
    private_pat(s, m, 1)
    g = Gateway()
    original = g.model
    contexts = []

    async def model(*args):
        contexts.append(args[-1])
        if len(contexts) == 1:
            deliver(s, m, 2, Content={"string": "新问题"})
        return await original(*args)

    g.model = model
    w = Worker(m, g, clock=lambda: NOW + 6)
    try:
        await w.once()
        await w.once()
        assert len(g.sends) == 1
        assert [[x["message_id"] for x in ctx if x.get("current")] for ctx in contexts] == [[1], [1, 2]]
    finally:
        w.release()


async def test_backfilled_image_prepares_actual_media(tmp_path):
    import io

    from PIL import Image
    from wechat_manager.config import Config
    from wechat_manager.media import IncomingMedia

    s, m, _, _ = group_setup(tmp_path)
    m.media = IncomingMedia(m, Config(tmp_path, tmp_path / "key"))
    from test_reply_images import Images

    deliver(s, m, 1, peer=GROUP, MsgType=3, Content={"string": "wxid_friend:\n<msg><img /></msg>"})
    pat(s, m, mid=2)
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "green").save(output, format="PNG")
    g = Images()
    original = g.model
    seen = []

    async def download(*args, **kwargs):
        return output.getvalue()

    async def model(*args):
        current = [x for x in args[-1] if x.get("current")]
        assert [x["message_id"] for x in current] == [1, 2]
        assert current[0]["images"][0]["message_id"] == 1
        seen.append(current)
        return await original(*args)

    g.download_media = download
    g.model = model
    w = Worker(m, g, clock=lambda: NOW + 6)
    try:
        await w.once()
        assert len(seen) == len(g.sends) == 1
        assert m.media.record(1)["state"] == "ready"
        assert any(frame["message_id"] == 1 for value in g.contexts[0] for frame in value.get("images", []))
    finally:
        w.release()


async def test_group_pat_just_after_answer_avoids_redundant_greeting(tmp_path):
    """生产事件 283/284：问题已回复，随后拍一拍不得再补一句寒暄。"""
    s, m, _, _ = group_setup(tmp_path)
    text(s, m, mid=1, body="@WeMind 21+22是多少")
    now = [NOW + 4]
    g = Gateway()
    w = Worker(m, g, clock=lambda: now[0])
    try:
        await w.once()
        assert len(g.sends) == 1
        pat(s, m, mid=2, created=NOW + 4)
        now[0] = NOW + 10
        await w.once()
        await w.once()
        assert len(g.sends) == 1
        assert (
            next(
                t
                for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
                if t["message_id"] == s.query("SELECT id FROM messages WHERE platform_id='2'")[0]["id"]
            )["state"]
            == "skipped"
        )
        text(s, m, mid=3, body="再加10呢", created=NOW + 11)
        now[0] = NOW + 15
        await w.once()
        assert len(g.sends) == 2
    finally:
        w.release()


async def test_group_pat_after_answer_still_collects_new_question(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    now = [NOW + 4]
    g = Gateway()
    w = Worker(m, g, clock=lambda: now[0])
    try:
        await w.once()
        pat(s, m, mid=2, created=NOW + 4)
        text(s, m, mid=3, body="新的问题", created=NOW + 5)
        now[0] = NOW + 10
        await w.once()
        assert len(g.sends) == 2
        assert (
            next(
                t
                for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
                if t["message_id"] == s.query("SELECT id FROM messages WHERE platform_id='2'")[0]["id"]
            )["state"]
            == "accepted"
        )
    finally:
        w.release()
