import json

import pytest
from business_fixtures import queue_contact_check
from contact_fixtures import setup_directory
from test_contact_removal import delete_event
from wechat_manager.accounts import AccountService
from wechat_manager.worker import Worker


def modify_event(store, manager, peer, **changes):
    event = {
        "TypeName": "ModContacts",
        "Appid": "wx_current_terminal",
        "Wxid": "me",
        "Data": {"UserName": {"string": peer}},
    }
    event.update(changes)
    store.receive(json.dumps(event).encode(), now=manager.clock())
    manager.process_events()


@pytest.mark.parametrize("peer", ["saved@chatroom", "unsaved@chatroom"])
async def test_group_requires_current_account_membership_without_saving_to_directory(tmp_path, peer):
    c, store, manager, aid, headers = setup_directory(tmp_path)
    clock = [manager.clock()]
    manager.clock = lambda: clock[0]
    await AccountService(manager, c.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, peer)
    delete_event(store, manager, peer)
    joined = [False]
    calls = []

    async def members(path, payload, **kwargs):
        calls.append((path, payload))
        return {"ret": 200, "data": {"memberList": [{"wxid": "me" if joined[0] else "other"}]}}

    c.app.state.gateway.call = members
    worker = Worker(manager, c.app.state.gateway, clock=manager.clock)

    queue_contact_check(c, conv["id"])
    assert await worker.verify_contacts_once()
    assert manager.conversation(conv["id"])["relationship_status"] == "group_left"
    assert not await worker.verify_contacts_once()
    joined[0] = True
    modify_event(store, manager, peer)
    modify_event(store, manager, peer)
    assert not await worker.verify_contacts_once()
    clock[0] += 61
    assert await worker.verify_contacts_once()
    assert manager.conversation(conv["id"])["relationship_status"] == "active"
    assert not manager.conversation(conv["id"])["allowed"]
    assert len(calls) == 2 and all(p == "/group/getChatroomMemberList" for p, _ in calls)
    assert not store.query("SELECT * FROM contact_entries WHERE peer_id=%s", (peer,))


async def test_friends_share_fresh_pull_and_cooldown_defers_only_one_job(tmp_path):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    clock = [manager.clock()]
    manager.clock = lambda: clock[0]
    await AccountService(manager, c.app.state.gateway).online(aid)
    for peer in ("alice", "bob"):
        manager.ensure_conversation(aid, peer)
        delete_event(store, manager, peer)
        modify_event(store, manager, peer)
    worker = Worker(manager, c.app.state.gateway, clock=manager.clock)
    assert await worker.verify_contacts_once()
    assert not await worker.verify_contacts_once()
    assert all(v["relationship_status"] == "active" for v in c.get("/wechat/api/v1/conversations").json())
    delete_event(store, manager, "alice")
    modify_event(store, manager, "alice")
    assert await worker.verify_contacts_once()  # 延后至共享全量拉取冷却结束。
    for _ in range(3):
        modify_event(store, manager, "alice")
        assert not await worker.verify_contacts_once()
    assert len(store.query("SELECT * FROM contact_verifications WHERE state='queued'")) == 1
    clock[0] += 601
    assert await worker.verify_contacts_once()
    assert [p for p, _ in c.app.state.gateway.calls].count("/contacts/fetchContactsList") == 2


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"ret": 500},
        {"ret": 200, "data": {}},
        {"ret": 200, "data": {"friends": [], "chatrooms": []}},
        "timeout",
    ],
)
async def test_failure_or_insufficient_evidence_keeps_marker_without_query_loop(tmp_path, response):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    await AccountService(manager, c.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "alice")
    delete_event(store, manager, "alice")
    modify_event(store, manager, "alice")
    calls = []

    async def result(*args, **kwargs):
        calls.append(args)
        if response == "timeout":
            raise TimeoutError()
        return response

    c.app.state.gateway.call = result
    worker = Worker(manager, c.app.state.gateway)
    assert await worker.verify_contacts_once()
    assert manager.conversation(conv["id"])["relationship_status"] == "friend_removed"
    assert not await worker.verify_contacts_once()
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["delete", "pending_delete", "terminal", "archived", "account", "lease"])
async def test_late_positive_result_cannot_cross_deletion_or_identity_version(tmp_path, change):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    await AccountService(manager, c.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "alice")
    delete_event(store, manager, "alice")
    modify_event(store, manager, "alice")

    async def late(*args, **kwargs):
        if change == "delete":
            delete_event(store, manager, "alice")
        elif change == "pending_delete":
            store.receive(
                json.dumps(
                    {
                        "TypeName": "DelContacts",
                        "Appid": "wx_current_terminal",
                        "Wxid": "me",
                        "Data": {"UserName": {"string": "alice"}},
                    }
                ).encode(),
                now=manager.clock(),
            )
        else:
            with store.connect() as conn:
                if change == "terminal":
                    conn.execute("UPDATE accounts SET current_app_id=NULL WHERE id=%s", (aid,))
                elif change == "archived":
                    conn.execute("UPDATE accounts SET archived_at=%s WHERE id=%s", (manager.clock(), aid))
                elif change == "account":
                    conn.execute("UPDATE accounts SET wxid='different-account' WHERE id=%s", (aid,))
                else:
                    conn.execute("UPDATE worker_lock SET expires_at=0")
        return {"ret": 200, "data": {"friends": ["alice"], "chatrooms": []}}

    c.app.state.gateway.call = late
    assert await Worker(manager, c.app.state.gateway).verify_contacts_once()
    assert manager.conversation(conv["id"])["relationship_status"] == "friend_removed"


async def test_replay_and_restart_preserve_deletion_version_and_recover_persisted_job(tmp_path):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    await AccountService(manager, c.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "alice")
    delete_event(store, manager, "alice")
    before = manager.conversation(conv["id"])["epoch"]
    with store.connect() as conn:
        conn.execute("UPDATE events SET processed=0 WHERE event_type='DelContacts'")
    manager.process_events()
    assert manager.conversation(conv["id"])["epoch"] == before
    modify_event(store, manager, "alice")
    with store.connect() as conn:
        conn.execute("UPDATE contact_verifications SET state='running',owner='dead-worker',lease_until=0")
    restarted = type(manager)(type(store)(store.url))
    assert await Worker(restarted, c.app.state.gateway).verify_contacts_once()
    assert restarted.conversation(conv["id"])["relationship_status"] == "active"
    assert len(store.query("SELECT * FROM conversations")) == 1


@pytest.mark.parametrize(
    "changes", [{"Wxid": "other"}, {"Appid": "old-terminal"}, {"Data": {}}, {"Data": {"UserName": " alice"}}]
)
async def test_untrusted_modify_cannot_queue_relation_check(tmp_path, changes):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    await AccountService(manager, c.app.state.gateway).online(aid)
    manager.ensure_conversation(aid, "alice")
    delete_event(store, manager, "alice")
    modify_event(store, manager, "alice", **changes)
    assert not store.query("SELECT * FROM contact_verifications")


async def test_postgres_restart_preserves_pending_verification_and_evidence(tmp_path):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    await AccountService(manager, c.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "alice")
    delete_event(store, manager, "alice")
    from wechat_manager.contact_changes import enqueue_verification

    with store.transaction() as conn:
        enqueue_verification(conn, aid, "alice", manager.clock())
    evidence = store.query("SELECT id,payload,raw_sha256 FROM events")
    restarted = type(manager)(type(store)(store.url))
    assert len(store.query("SELECT * FROM contact_verifications WHERE state='queued'")) == 1
    assert await Worker(restarted, c.app.state.gateway).verify_contacts_once()
    assert restarted.conversation(conv["id"])["relationship_status"] == "active"
    assert store.query("SELECT id,payload,raw_sha256 FROM events") == evidence


@pytest.mark.parametrize("case", ["live", "history", "test", "old", "migration", "other-account"])
async def test_only_new_trusted_live_message_queues_verification_without_restoring_itself(tmp_path, case):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    await AccountService(manager, c.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "alice")
    delete_event(store, manager, "alice")
    event = {
        "TypeName": "AddMsg",
        "Appid": "wx_current_terminal",
        "Wxid": "other" if case == "other-account" else "me",
        "Data": {
            "NewMsgId": "1234567",
            "MsgType": 1,
            "FromUserName": {"string": "alice"},
            "ToUserName": {"string": "me"},
            "Content": {"string": "新的消息"},
            "CreateTime": manager.clock() - (601 if case == "old" else 0),
        },
    }
    if case == "history":
        event["Data"]["IsHistory"] = True
    if case == "test":
        event["Data"]["IsTest"] = True
    store.receive(
        json.dumps(event).encode(), origin="migration" if case == "migration" else "live", now=manager.clock()
    )
    manager.process_events()
    assert manager.conversation(conv["id"])["relationship_status"] == "friend_removed"
    assert bool(store.query("SELECT * FROM contact_verifications")) == (case == "live")
