"""新增对象发现契约；在服务器独立临时库执行，不发送微信或模型请求。"""

import json

import pytest
from business_fixtures import contacts
from qingda.contact_changes import mark_removed, record_evidence
from qingda.contact_discovery import enqueue, group_members
from qingda.contacts import enqueue_contacts
from qingda.store import Store
from qingda.worker import Worker
from test_account_refresh import account_service


async def setup(tmp_path, response=None):
    store, manager, service, gateway, aid, clock = account_service(tmp_path)
    await service.online(aid)
    calls = []

    async def call(path, payload, **kwargs):
        calls.append((path, payload))
        if path.endswith("checkRelation"):
            return (
                response
                if response is not None
                else {"ret": 200, "data": [{"wxid": payload["wxids"][0], "relation": 0}]}
            )
        if path.endswith("getChatroomMemberList"):
            return (
                response
                if response is not None
                else {"ret": 200, "data": {"memberList": [{"wxid": "new-wxid"}, {"wxid": "other"}]}}
            )
        if path.endswith(("fetchContactsListCache", "fetchContactsList")):
            return {"ret": 200, "data": {"friends": [], "chatrooms": [], "isCache": True}}
        raise AssertionError(path)

    gateway.call = call
    worker = Worker(manager, gateway, clock=lambda: clock[0])
    worker.lease()
    return store, manager, gateway, aid, clock, worker, calls


def queue(store, aid, peer, now, event_id=None):
    with store.transaction() as conn:
        enqueue(conn, aid, peer, now, event_id)


def remove(conn, manager, aid, peer, now):
    account = manager.account(aid, conn)
    proof = record_evidence(conn, account, peer, "isolated_removal", now, now, {"removed": True})
    mark_removed(conn, account, peer, now, "friend_removed", evidence_id=proof)


async def test_friend_exact_confirmation_publishes_with_default_reply_without_full_snapshot(tmp_path):
    store, manager, _, aid, clock, worker, calls = await setup(tmp_path)
    queue(store, aid, "alice", clock[0])
    assert await worker.contact_discovery.once()
    result = contacts(manager, aid)
    assert result["friend_count"] == result["total"] == 1
    assert result["items"][0]["saved_to_contacts"] is True
    assert result["items"][0]["owner_wxid"] == "new-wxid"
    assert len(store.query("SELECT * FROM conversations WHERE allowed=1")) == 1
    assert not store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    conv = manager.ensure_conversation(aid, "alice")
    assert conv["allowed"] == 1
    assert calls == [("/contacts/checkRelation", {"appId": "new-terminal", "wxids": ["alice"]})]
    assert store.query("SELECT state FROM chat_profiles")[0]["state"] == "queued"


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        [{"wxid": "another", "relation": 0}],
        [{"wxid": "alice", "relation": "0"}],
        [{"wxid": "alice", "relation": False}],
        [{"wxid": "alice", "relation": 0}, {"wxid": "alice", "relation": 0}],
        *[[{"wxid": "alice", "relation": n}] for n in (1, 2, 3, 4, 5, 6, 7, 99)],
    ],
)
async def test_only_exact_integer_normal_relation_is_accepted(tmp_path, data):
    store, manager, _, aid, clock, worker, _ = await setup(tmp_path, {"ret": 200, "data": data})
    queue(store, aid, "alice", clock[0])
    assert await worker.contact_discovery.once()
    assert not contacts(manager, aid)["items"]
    row = store.query("SELECT * FROM contact_discoveries")[0]
    assert row["state"] == "failed" and row["next_at"] > clock[0]
    if data and data[0].get("relation") == 5:
        assert row["next_at"] >= clock[0] + 60


async def test_unsaved_group_is_discovered_and_survives_saved_list_refresh(tmp_path):
    store, manager, _, aid, clock, worker, _ = await setup(tmp_path)
    queue(store, aid, "room@chatroom", clock[0])
    await worker.contact_discovery.once()
    await worker.contacts_once()
    directory = contacts(manager, aid, "group")
    assert directory["joined_group_count"] == directory["total"] == 1
    assert directory["chatroom_count"] == 0
    assert not directory["items"][0]["saved_to_contacts"]
    assert manager.ensure_conversation(aid, "room@chatroom")["allowed"]
    assert manager.accounts()[0]["contacts"]["chatroom_count"] == 0
    assert not store.query("SELECT saved_to_contacts FROM contact_entries WHERE peer_id='room@chatroom'")[0][
        "saved_to_contacts"
    ]


@pytest.mark.parametrize(
    "response",
    [
        {"ret": 500},
        {"ret": 200, "data": {"memberList": []}},
        {"ret": 200, "data": {"memberList": [{"wxid": "new-wxid"}], "hasMore": True}},
        {"ret": 200, "data": {"memberList": [{"wxid": "new-wxid"}, {}]}},
    ],
)
async def test_unknown_group_membership_never_publishes_or_removes(tmp_path, response):
    store, manager, _, aid, clock, worker, _ = await setup(tmp_path, response)
    queue(store, aid, "room@chatroom", clock[0])
    await worker.contact_discovery.once()
    assert contacts(manager, aid, "group")["joined_group_count"] == 0
    assert not store.query("SELECT * FROM contact_removals")


async def test_historical_groups_are_deduplicated_and_absence_preserves_chat(tmp_path):
    store, manager, _, aid, _, worker, calls = await setup(
        tmp_path, {"ret": 200, "data": {"memberList": [{"wxid": "other"}]}}
    )
    conv = manager.ensure_conversation(aid, "room@chatroom")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO contact_entries(account_id,peer_id,kind,display_name,sort_key,owner_wxid) VALUES(%s,%s,'group','群','room','new-wxid')",
            (aid, "room@chatroom"),
        )
    await worker.contact_discovery.once()
    assert not contacts(manager, aid, "group")["items"]
    assert manager.conversation(conv["id"])["removed_at"] is not None
    assert len(store.query("SELECT * FROM contact_discoveries")) == len(calls) == 1
    assert not await worker.contact_discovery.once()


async def test_duplicate_callbacks_and_checkpoint_recovery(tmp_path):
    store, manager, gateway, aid, clock, worker, calls = await setup(tmp_path)
    for _ in range(3):
        queue(store, aid, "alice", clock[0])
    with store.transaction() as conn:
        conn.execute(
            "UPDATE contact_discoveries SET state='running',owner='dead-worker',lease_until=%s",
            (clock[0] + 30,),
        )
    reopened = Store(store.url)
    restored = Worker(type(manager)(reopened, clock=lambda: clock[0]), gateway, clock=lambda: clock[0])
    # worker_lock 仍由同一个有效 worker 持有时不得抢占。
    assert not await restored.contact_discovery.once()
    with store.transaction() as conn:
        conn.execute("UPDATE worker_lock SET expires_at=0")
    restored.lease()
    assert await restored.contact_discovery.once()
    assert len(calls) == 1
    queue(store, aid, "alice", clock[0])
    assert not await restored.contact_discovery.once()
    assert reopened.query("SELECT state FROM contact_discoveries")[0]["state"] == "ready"


async def test_failed_verification_retries_after_restart(tmp_path):
    store, manager, gateway, aid, clock, worker, _ = await setup(tmp_path, {"ret": 200, "data": []})
    queue(store, aid, "alice", clock[0])
    await worker.contact_discovery.once()
    row = store.query("SELECT * FROM contact_discoveries")[0]
    assert not await worker.contact_discovery.once()
    clock[0] = row["next_at"]

    async def success(*args, **kwargs):
        return {"ret": 200, "data": [{"wxid": "alice", "relation": 0}]}

    gateway.call = success
    worker.contact_discovery.store = Store(store.url)
    assert await worker.contact_discovery.once()
    assert contacts(manager, aid)["total"] == 1


@pytest.mark.parametrize("change", ["delete", "identity"])
async def test_late_positive_result_cannot_restore_removed_or_changed_identity(tmp_path, change):
    store, manager, gateway, aid, clock, worker, _ = await setup(tmp_path)
    queue(store, aid, "alice", clock[0])

    async def late(*args, **kwargs):
        with store.transaction() as conn:
            if change == "delete":
                remove(conn, manager, aid, "alice", clock[0])
            else:
                conn.execute("UPDATE accounts SET epoch=epoch+1 WHERE id=%s", (aid,))
        return {"ret": 200, "data": [{"wxid": "alice", "relation": 0}]}

    gateway.call = late
    await worker.contact_discovery.once()
    assert not contacts(manager, aid)["items"]


async def test_removed_friend_uses_strict_existing_recovery_queue(tmp_path):
    store, manager, _, aid, clock, worker, calls = await setup(tmp_path)
    manager.ensure_conversation(aid, "alice")
    with store.transaction() as conn:
        remove(conn, manager, aid, "alice", clock[0])
    queue(store, aid, "alice", clock[0])
    assert not await worker.contact_discovery.once()
    assert not calls
    assert store.query("SELECT state FROM contact_verifications")[0]["state"] == "queued"


async def test_older_cached_and_inflight_full_snapshots_keep_new_confirmation(tmp_path):
    store, manager, gateway, aid, clock, worker, _ = await setup(tmp_path)
    queue(store, aid, "alice", clock[0])
    await worker.contact_discovery.once()
    await worker.contacts_once()
    assert contacts(manager, aid)["total"] == 1
    clock[0] += 601
    with store.transaction() as conn:
        enqueue_contacts(conn, aid, clock[0])

    async def full(path, payload, **kwargs):
        if path.endswith("fetchContactsListCache"):
            return {"ret": 500}
        if path.endswith("fetchContactsList"):
            clock[0] += 1
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE contact_entries SET confirmed_at=%s WHERE account_id=%s", (clock[0], aid)
                )
            return {"ret": 200, "data": {"friends": ["someone-else"], "chatrooms": []}}
        raise AssertionError(path)

    gateway.call = full
    await worker.contacts_once()
    assert {i["peer_id"] for i in contacts(manager, aid)["items"]} == {"alice", "someone-else"}


def test_group_member_parser_rejects_empty_data():
    with pytest.raises(Exception):
        group_members(None)


@pytest.mark.parametrize("event_type", ["ModContacts", "AddMsg"])
@pytest.mark.parametrize("origin,history", [("live", False), ("replay", False), ("live", True)])
async def test_callback_ingestion_trust_boundary(tmp_path, event_type, origin, history):
    store, manager, _, aid, clock, _, _ = await setup(tmp_path)
    data = {"UserName": {"string": "alice"}, "NickName": {"string": "新好友"}, "IsHistory": history}
    if event_type == "AddMsg":
        data.update(
            NewMsgId=12345,
            MsgType=1,
            FromUserName={"string": "alice"},
            ToUserName={"string": "new-wxid"},
            Content={"string": "回调原文"},
            CreateTime=int(clock[0]),
        )
    value = {"TypeName": event_type, "Appid": "new-terminal", "Wxid": "new-wxid", "Data": data}
    store.receive(json.dumps(value, ensure_ascii=False).encode(), origin=origin, now=clock[0])
    manager.process_events(now=clock[0])
    assert bool(store.query("SELECT * FROM contact_discoveries")) == (origin == "live" and not history)
    assert not contacts(manager, aid)["items"]
    assert not [
        t
        for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
        if t["state"] == "queued"
    ]
