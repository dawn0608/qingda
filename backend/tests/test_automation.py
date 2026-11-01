import json

import pytest
from business_fixtures import allow_reply
from test_database import database_url
from wechat_manager.service import Manager
from wechat_manager.store import Store

NOW = 1791448900.0


def verified_fixture(manager, conv):
    """既有自动回复用例的前置关系证据；核实失败本身由监测用例通过 HTTP 验收。"""
    with manager.store.connect() as conn:
        account = manager.account(conv["account_id"], conn)
        terminal = conn.execute(
            "SELECT operation_id FROM terminals WHERE app_id=%s", (account["current_app_id"],)
        ).fetchone()
        conn.execute("UPDATE accounts SET online=1 WHERE id=%s", (account["id"],))
        conn.execute(
            "INSERT INTO contact_monitor(account_id,peer_id,app_id,wxid,epoch,operation_id,state,verified_at,next_at,held) VALUES(%s,%s,%s,%s,%s,%s,'verified',%s,%s,0) ON CONFLICT DO NOTHING",
            (
                account["id"],
                conv["peer_id"],
                account["current_app_id"],
                account["wxid"],
                account["epoch"],
                terminal[0],
                NOW,
                NOW + 600,
            ),
        )


def setup(tmp_path):
    store = Store(database_url(tmp_path / "test.db"))
    manager = Manager(store, clock=lambda: NOW)
    a = manager.add_account("device-a", "主账号")
    manager.verify_identity("device-a", {"wxid": "wxid_me", "nickName": "我"})
    manager.configure_model({"model": "deepseek-flash"})
    manager.configure_automation(a["id"], prompt="请简洁回复", ai_enabled=True)
    c = manager.ensure_conversation(a["id"], "friend")
    allow_reply(manager, c["id"], True)
    verified_fixture(manager, c)
    return store, manager, a, c


def deliver(store, manager, mid, peer="friend", sender=None, created=None, source="", received=None, **extra):
    verified_fixture(manager, manager.ensure_conversation(manager.account(1)["id"], peer))
    raw = {
        "TypeName": "AddMsg",
        "Appid": "device-a",
        "Wxid": "wxid_me",
        "Data": {
            "NewMsgId": mid,
            "MsgType": 1,
            "FromUserName": {"string": sender or peer},
            "ToUserName": {"string": "wxid_me"},
            "Content": {"string": "hello"},
            "CreateTime": created if created is not None else NOW + 1,
            "MsgSource": source,
            **extra,
        },
    }
    received = max(NOW + 2, (created or 0) + 2) if received is None else received
    store.receive(json.dumps(raw).encode(), now=received)
    manager.process_events(now=received)


@pytest.mark.parametrize("peer", ["new-friend", "new-group@chatroom"])
def test_new_conversation_replies_by_default_after_relationship_verification(tmp_path, peer):
    store, manager, _, _ = setup(tmp_path)
    source = "<msgsource><atuserlist>wxid_me</atuserlist></msgsource>" if peer.endswith("@chatroom") else ""
    deliver(store, manager, 101, peer=peer, source=source)
    conv = manager.ensure_conversation(1, peer)
    assert conv["allowed"]
    task = store.query("SELECT * FROM tasks WHERE conversation_id=%s", (conv["id"],))[0]
    assert task["state"] == "queued"


def test_manual_off_and_explicit_group_mention_are_required(tmp_path):
    s, m, a, c = setup(tmp_path)
    deliver(s, m, 1)
    assert len(s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")) == 1
    disabled = m.ensure_conversation(a["id"], "not-allowed")
    m.set_auto_reply(disabled["id"], False)
    deliver(s, m, 2, peer="not-allowed")
    assert len(s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")) == 2
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["reason"]
        == "not_allowed"
    )
    group = m.ensure_conversation(a["id"], "group@chatroom")
    allow_reply(m, group["id"], True)
    deliver(
        s, m, 3, peer="group@chatroom", source="<msgsource><atuserlist>notify@all</atuserlist></msgsource>"
    )
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["reason"]
        == "not_explicitly_mentioned"
    )
    deliver(
        s,
        m,
        4,
        peer="group@chatroom",
        source="<msgsource><atuserlist><![CDATA[wxid_me]]></atuserlist></msgsource>",
    )
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"] == "queued"
    )


def test_history_self_and_duplicates_do_not_trigger_auto(tmp_path):
    s, m, a, c = setup(tmp_path)
    deliver(s, m, 1, created=NOW - 20)
    assert (
        s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "skipped"
    )
    deliver(s, m, 2, sender="wxid_me")
    assert m.conversation(c["id"])["pause_until"] == 0  # 自身到自身是另一会话
    deliver(s, m, 3)
    deliver(s, m, 3)
    assert (
        len(
            [
                t
                for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
                if t["message_id"] == 3
            ]
        )
        <= 1
    )


def test_private_context_includes_followups_in_same_open_round(tmp_path):
    s, m, a, c = setup(tmp_path)
    deliver(s, m, 1)
    first = s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]
    deliver(s, m, 2, created=NOW + 3)
    context = m.context(first)
    assert len(context) == 2
    assert context[0]["content"] == "hello"


def test_online_identity_refresh_does_not_reenable_a_stopped_account(tmp_path):
    s, m, a, c = setup(tmp_path)
    with s.transaction() as connection:
        m.stop_account(a["id"], "stopped", connection)
    m.verify_identity("device-a", {"wxid": "wxid_me", "nickName": "更新的昵称"})
    assert m.account(a["id"])["enabled"] == 0
    assert m.account(a["id"])["ai_enabled"] == 0


def test_same_named_peer_and_platform_id_are_isolated_between_accounts(tmp_path):
    s, m, a, c = setup(tmp_path)
    deliver(s, m, 1)
    second = m.add_account("device-b")
    m.verify_identity("device-b", {"wxid": "wxid_second"})
    m.configure_automation(second["id"], "第二账号提示词", True)
    other = m.ensure_conversation(second["id"], "friend")
    allow_reply(m, other["id"], True, "相同名称")
    allow_reply(m, c["id"], True, "相同名称")
    raw = s.event(1)
    raw["Appid"], raw["Wxid"] = "device-b", "wxid_second"
    raw["Data"]["ToUserName"] = {"string": "wxid_second"}
    raw["Data"]["Content"] = {"string": "仅第二账号可见"}
    s.receive(json.dumps(raw).encode(), now=NOW + 3)
    m.process_events(now=NOW + 3)
    tasks = [
        t
        for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
        if t["kind"] == "auto"
    ]
    assert len(tasks) == 2 and len({t["account_id"] for t in tasks}) == 2
    first = next(t for t in tasks if t["account_id"] == a["id"])
    second_task = next(t for t in tasks if t["account_id"] == second["id"])
    assert [
        {k: v for k, v in item.items() if k not in ("received_at", "history_selection")}
        for item in m.context(first)
    ] == [
        {
            "role": "user",
            "content": "hello",
            "current": True,
            "message_id": 1,
            "sender": "friend",
            "timestamp": NOW + 1,
            "speaker_type": "member",
            "wakeup_reason": None,
            "conversation_kind": "private",
            "reply_target": "friend",
            "context_scope": "current",
            "input_scope": "current",
            "message_kind": "text",
            "presence_suppressed": False,
            "profile_schema_version": "participant-profiles-v1",
            "participant_profiles": {
                "friend": {
                    "nickname": "",
                    "gender": "未知",
                    "profile_region": {},
                    "freshness": "missing",
                    "role": "reply_target",
                }
            },
        }
    ]
    assert [
        {k: v for k, v in item.items() if k not in ("received_at", "history_selection")}
        for item in m.context(second_task)
    ] == [
        {
            "role": "user",
            "content": "仅第二账号可见",
            "current": True,
            "message_id": 2,
            "sender": "friend",
            "timestamp": NOW + 1,
            "speaker_type": "member",
            "wakeup_reason": None,
            "conversation_kind": "private",
            "reply_target": "friend",
            "context_scope": "current",
            "input_scope": "current",
            "message_kind": "text",
            "presence_suppressed": False,
            "profile_schema_version": "participant-profiles-v1",
            "participant_profiles": {
                "friend": {
                    "nickname": "",
                    "gender": "未知",
                    "profile_region": {},
                    "freshness": "missing",
                    "role": "reply_target",
                }
            },
        }
    ]


def test_internal_terminal_discovery_is_idempotent_and_preserves_remark(tmp_path):
    s, m, a, _ = setup(tmp_path)
    assert m.add_account("device-a", "不应覆盖")["id"] == a["id"]
    second = m.add_account("new-device", "新终端")
    assert m.add_account("new-device")["id"] == second["id"]
    assert m.account(a["id"])["remark"] == "主账号"
    assert len(m.accounts()) == 2 and s.health()["database_ready"] is True
