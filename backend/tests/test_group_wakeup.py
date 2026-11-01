"""事件 201/215/229/234 的脱敏触发回归；仅在服务器隔离库运行。"""

import json

import pytest
from business_fixtures import allow_reply
from test_automation import NOW, deliver, setup
from test_worker import Gateway
from wechat_manager.service import Manager
from wechat_manager.worker import Worker

GROUP = "group@chatroom"


def group_setup(tmp_path):
    store, manager, account, _ = setup(tmp_path)
    with store.transaction() as conn:
        conn.execute("UPDATE accounts SET nickname='WeMind' WHERE id=%s", (account["id"],))
    group = manager.ensure_conversation(account["id"], GROUP)
    allow_reply(manager, group["id"], True)
    return store, manager, account, group


def text(store, manager, mid=1, body="@WeMind\u2005你好", source="", **extra):
    deliver(
        store, manager, mid, peer=GROUP, source=source, Content={"string": "wxid_friend:\n" + body}, **extra
    )


def pat(
    store, manager, mid=2, actor="wxid_friend", target="wxid_me", session=GROUP, prefix=GROUP + ":\n", **extra
):
    # 真实样本 201 的正文前缀和 FromUserName 均为群，操作者只在 pat 内。
    body = (
        f'{prefix}<sysmsg type="pat"><pat><fromusername>{actor}</fromusername>'
        f"<chatusername>{session}</chatusername><pattedusername>{target}</pattedusername>"
        '<template><![CDATA["${wxid_friend}" 拍了拍 "${wxid_me}"]]></template>'
        "</pat></sysmsg>"
    )
    deliver(store, manager, mid, peer=GROUP, MsgType=10002, Content={"string": body}, **extra)


@pytest.mark.parametrize(
    "body,source,expected",
    [
        ("@WeMind\u2005爱出者爱返 福往者福来什么意思", "", "text_call"),
        (" \n@WeMind", "", "text_call"),
        ("@WeMind：你好", "", "text_call"),
        ("@WeMind123 你好", "", None),
        ("有人说 @WeMind 你好", "", None),
        ("“@WeMind 你好”", "", None),
        ("@wemind 你好", "", None),
        ("@其他人 你好", "", None),
        ("@WeMind 你好", "<msgsource><atuserlist>other</atuserlist></msgsource>", None),
        ("@WeMind 你好", "<msgsource><atuserlist>notify@all</atuserlist></msgsource>", None),
        ("@WeMind 你好", "<msgsource><atuserlist><![CDATA[wxid_me]]></atuserlist></msgsource>", "mention"),
    ],
)
def test_text_calls_and_protocol_mentions(tmp_path, body, source, expected):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m, body=body, source=source)
    task = s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]
    metadata = json.loads(s.query("SELECT metadata FROM messages")[0]["metadata"])
    assert (task["state"] == "queued") == bool(expected)
    assert metadata.get("wakeup_reason") == expected
    assert metadata["mentioned"] == (expected == "mention")


def test_real_pat_has_actor_context_and_deduplication(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    pat(s, m)
    pat(s, m)
    assert (
        len(s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")) == 1
        and s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "queued"
    )
    row = s.query("SELECT * FROM messages")[0]
    assert row["sender"] == "wxid_friend"
    assert json.loads(row["metadata"])["wakeup_reason"] == "pat"
    context = m.context(s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0])[-1]
    assert context["sender"] == "wxid_friend"
    assert "拍了拍你" in context["content"] and "<sysmsg" not in context["content"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target": "other"},
        {"actor": "wxid_me"},
        {"actor": ""},
        {"session": "other@chatroom"},
        {"prefix": "other:\n"},
        {"actor": "other@chatroom"},
    ],
)
def test_pat_requires_unambiguous_other_actor_and_self_target(tmp_path, kwargs):
    s, m, _, _ = group_setup(tmp_path)
    pat(s, m, **kwargs)
    assert not any(
        t["state"] == "queued"
        for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    )


@pytest.mark.parametrize("trigger", ["text", "mention", "pat"])
@pytest.mark.parametrize("block", ["paused", "not_allowed", "ai_disabled", "historical_sync", "expired"])
def test_all_triggers_respect_existing_guards(tmp_path, trigger, block):
    s, m, a, g = group_setup(tmp_path)
    extra = {}
    with s.transaction() as c:
        if block == "paused":
            c.execute("UPDATE conversations SET pause_until=%s WHERE id=%s", (NOW + 1800, g["id"]))
        if block == "not_allowed":
            c.execute("UPDATE conversations SET allowed=0 WHERE id=%s", (g["id"],))
        if block == "ai_disabled":
            c.execute("UPDATE accounts SET ai_enabled=0 WHERE id=%s", (a["id"],))
    if block == "historical_sync":
        extra["IsHistory"] = True
    if block == "expired":
        extra.update(created=NOW + 1, received=NOW + 400)
    if trigger == "pat":
        pat(s, m, **extra)
    else:
        text(
            s,
            m,
            source="<msgsource><atuserlist>wxid_me</atuserlist></msgsource>" if trigger == "mention" else "",
            **extra,
        )
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["reason"] == block
    )
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "skipped"
    )


async def test_pat_and_question_merge_after_restart_and_send_once(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    pat(s, m, mid=1)
    text(s, m, mid=2)
    manager = Manager(s, clock=lambda: NOW + 5)
    gateway = Gateway()
    contexts = []
    original = gateway.model

    async def model(*args):
        contexts.append(args[-1])
        return await original(*args)

    gateway.model = model
    worker = Worker(manager, gateway, clock=lambda: NOW + 5)
    try:
        await worker.once()
        await worker.once()
        assert len(gateway.sends) == 1
        current = [r for r in contexts[0] if r.get("current")]
        assert len(current) == 2 and all(r["sender"] == "wxid_friend" for r in current)
        assert "拍了拍你" in current[0]["content"]
    finally:
        worker.release()


def test_nicknames_are_scoped_and_do_not_use_remarks(tmp_path):
    s, m, a, g = group_setup(tmp_path)
    with s.transaction() as c:
        c.execute(
            "INSERT INTO chat_member_profiles VALUES(%s,%s,%s,%s,%s)",
            (a["id"], GROUP, "wxid_me", json.dumps({"displayName": "小维", "remark": "私人备注"}), NOW),
        )
        c.execute(
            "INSERT INTO chat_member_profiles VALUES(%s,%s,%s,%s,%s)",
            (a["id"], "other@chatroom", "wxid_me", json.dumps({"displayName": "别群名字"}), NOW),
        )
        c.execute(
            "INSERT INTO chat_member_profiles VALUES(%s,%s,%s,%s,%s)",
            (999, GROUP, "wxid_me", json.dumps({"displayName": "别账号名字"}), NOW),
        )
    for i, name in enumerate(["小维", "私人备注", "别群名字", "别账号名字"], 1):
        text(s, m, mid=i, body="@" + name + " 你好")
    assert (
        sum(
            t["state"] == "queued"
            for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
        )
        == 1
    )


@pytest.mark.parametrize("subtype", [19, 57])
def test_nested_and_quoted_mentions_do_not_wake(tmp_path, subtype):
    s, m, _, _ = group_setup(tmp_path)
    deliver(
        s,
        m,
        1,
        peer=GROUP,
        MsgType=49,
        Content={
            "string": f"wxid_friend:\n<msg><appmsg><type>{subtype}</type><title>@WeMind 你好</title><refermsg><svrid>22</svrid><content>@WeMind 你好</content></refermsg></appmsg></msg>"
        },
    )
    assert not any(
        t["state"] == "queued"
        for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    )


def test_counts_include_wakeups_and_revoke_cancels_without_replay(tmp_path):
    s, m, a, g = group_setup(tmp_path)
    pat(s, m, mid=1)
    text(s, m, mid=2)
    deliver(
        s,
        m,
        3,
        peer=GROUP,
        MsgType=10002,
        Content={
            "string": f'<sysmsg type="revokemsg"><revokemsg><session>{GROUP}</session><newmsgid>2</newmsgid><replacemsg>撤回了一条消息</replacemsg></revokemsg></sysmsg>'
        },
    )
    assert (
        next(
            t
            for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["message_id"] == 2
        )["state"]
        == "canceled"
    )
    m.process_events(now=NOW + 5)
    assert len(s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")) == 2


@pytest.mark.parametrize("pause_during_model", [False, True])
async def test_single_pat_sends_unless_manually_paused_before_send(tmp_path, pause_during_model):
    s, m, _, g = group_setup(tmp_path)
    pat(s, m)
    gateway = Gateway()
    original = gateway.model

    async def model(*args):
        assert "拍了拍你" in args[-1][-1]["content"]
        if pause_during_model:
            with s.transaction() as c:
                m.takeover(g["id"], c, NOW + 3)
        return await original(*args)

    gateway.model = model
    worker = Worker(m, gateway, clock=lambda: NOW + 5)
    try:
        await worker.once()
        assert len(gateway.sends) == (0 if pause_during_model else 1)
    finally:
        worker.release()


def test_old_terminal_and_mismatched_identity_cannot_wake(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    with s.transaction() as c:
        c.execute("UPDATE terminals SET verified=0 WHERE app_id='device-a'")
    pat(s, m)
    text(s, m, mid=3)
    assert not any(
        t["state"] == "queued"
        for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    )


async def test_different_actors_are_not_merged(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    pat(s, m, mid=1, actor="actor_one")
    pat(s, m, mid=2, actor="actor_two")
    gateway = Gateway()
    worker = Worker(m, gateway, clock=lambda: NOW + 5)
    try:
        await worker.once()
        assert len(gateway.sends) == 1
        assert (
            sum(
                t["state"] == "queued"
                for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            )
            == 1
        )
        assert not any(
            t["state"] == "merged"
            for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
        )
    finally:
        worker.release()
