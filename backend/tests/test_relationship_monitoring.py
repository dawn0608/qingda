"""通过 Worker、HTTP 和真实隔离数据库验收；平台结果均为隔离样例。"""

import asyncio
import json

from business_fixtures import allow_reply, queue_contact_check
from contact_fixtures import setup_directory
from qingda.accounts import AccountService
from qingda.worker import Worker


async def test_background_monitor_detects_unsaved_group_without_page_or_callback(tmp_path):
    client, store, manager, aid, _ = setup_directory(tmp_path)
    await AccountService(manager, client.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "unsaved@chatroom")

    async def absent(*args, **kwargs):
        return {"ret": 200, "data": {"memberList": [{"wxid": "remaining"}]}}

    client.app.state.gateway.call = absent
    worker = Worker(manager, client.app.state.gateway)
    await worker.monitor_contacts_once()
    await worker.verify_contacts_once()
    row = manager.conversation(conv["id"])
    assert row["relationship_reason"] == "group_left"
    assert not store.events()


async def test_query_failure_pauses_auto_keeps_allowlist_and_recovers_only_new_messages(tmp_path):
    client, store, manager, aid, headers = setup_directory(tmp_path)
    clock = [manager.clock()]
    manager.clock = lambda: clock[0]
    manager.configure_model({"model": "deepseek-flash"}, api_key="isolated-test-key")
    manager.configure_automation(aid, prompt="简洁回复", ai_enabled=True)
    await AccountService(manager, client.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "unsaved@chatroom")
    allow_reply(manager, conv["id"], True)
    good = [True]

    async def members(*args, **kwargs):
        return {"ret": 200, "data": {"memberList": [{"wxid": "me"}]}} if good[0] else {"ret": 500}

    client.app.state.gateway.call = members
    worker = Worker(manager, client.app.state.gateway, clock=manager.clock)

    async def verify():
        queue_contact_check(client, conv["id"])
        await worker.verify_contacts_once()
        return client.get("/wechat/api/v1/conversations").json()[0]

    def deliver(mid, created=None):
        store.receive(
            json.dumps(
                {
                    "TypeName": "AddMsg",
                    "Appid": "wx_current_terminal",
                    "Wxid": "me",
                    "Data": {
                        "NewMsgId": str(mid),
                        "MsgType": 1,
                        "FromUserName": {"string": "unsaved@chatroom"},
                        "ToUserName": {"string": "me"},
                        "Content": {"string": "other:\nhello"},
                        "CreateTime": created or clock[0],
                        "MsgSource": "<msgsource><atuserlist>me</atuserlist></msgsource>",
                    },
                }
            ).encode(),
            now=clock[0],
        )
        manager.process_events()

    row = await verify()
    assert row["verification"]["state"] == "verified"
    clock[0] += 1
    deliver(1)
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "queued"
    )

    clock[0] += 61
    good[0] = False
    row = await verify()
    assert row["allowed"] and row["removed_at"] is None
    assert row["verification"]["state"] == "failed"
    assert row["auto_reply_status"] == "relationship_unverified"
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "canceled"
    )
    deliver(2)
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["reason"]
        == "relationship_unverified"
    )
    clock[0] += 601
    good[0] = True
    assert (await verify())["auto_reply_status"] == "enabled"
    deliver(3, clock[0] - 20)
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["reason"]
        == "before_watermark"
    )
    clock[0] += 1
    deliver(4)
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "queued"
    )


async def test_monitor_shares_friend_pull_and_persists_failure_backoff(tmp_path):
    client, store, manager, aid, _ = setup_directory(tmp_path)
    clock = [manager.clock()]
    manager.clock = lambda: clock[0]
    await AccountService(manager, client.app.state.gateway).online(aid)
    for peer in ("alice", "bob"):
        manager.ensure_conversation(aid, peer)
    calls = []
    good = [False]

    async def friends(path, payload, **kwargs):
        calls.append(path)
        return (
            {"ret": 200, "data": {"friends": ["alice", "bob"], "chatrooms": []}} if good[0] else {"ret": 500}
        )

    client.app.state.gateway.call = friends
    worker = Worker(manager, client.app.state.gateway, clock=manager.clock)
    for delay in (600, 1200, 2400, 3600):
        await worker.monitor_contacts_once()
        assert await worker.verify_contacts_once()
        rows = client.get("/wechat/api/v1/conversations").json()
        assert all(r["verification"]["next_at"] == clock[0] + delay for r in rows)
        assert not await worker.verify_contacts_once()
        assert not await worker.monitor_contacts_once()
        clock[0] += delay
    assert len(calls) == 4
    worker.release()
    restarted = Worker(
        type(manager)(type(store)(store.url), clock=manager.clock),
        client.app.state.gateway,
        clock=manager.clock,
    )
    good[0] = True
    await restarted.monitor_contacts_once()
    await restarted.verify_contacts_once()
    assert all(
        r["verification"]["state"] == "verified" for r in client.get("/wechat/api/v1/conversations").json()
    )
    before = len(calls)
    rows = client.get(f"/wechat/api/v1/conversations?account_id={aid}").json()
    assert len(rows) == 2
    assert all(row["removed_at"] is None and row["verification"]["state"] == "verified" for row in rows)
    assert len(calls) == before


async def test_relationship_queries_allow_two_concurrent_groups(tmp_path):
    client, store, manager, aid, _ = setup_directory(tmp_path)
    await AccountService(manager, client.app.state.gateway).online(aid)
    for peer in ("one@chatroom", "two@chatroom", "three@chatroom"):
        manager.ensure_conversation(aid, peer)
    running = [0, 0]
    barrier = asyncio.Event()

    async def members(*args, **kwargs):
        running[0] += 1
        running[1] = max(running)
        if running[0] == 2:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 1)
        await asyncio.sleep(0.01)
        running[0] -= 1
        return {"ret": 200, "data": {"memberList": [{"wxid": "me"}]}}

    client.app.state.gateway.call = members
    worker = Worker(manager, client.app.state.gateway)
    await worker.monitor_contacts_once()
    await asyncio.gather(*(worker.verify_contacts_once() for _ in range(3)))
    assert running[1] == 2
    assert all(
        r["verification"]["state"] == "verified" for r in client.get("/wechat/api/v1/conversations").json()
    )


async def test_unverified_and_expired_evidence_hold_auto_without_mutating_reads(tmp_path):
    client, store, manager, aid, headers = setup_directory(tmp_path)
    clock = [manager.clock()]
    manager.clock = lambda: clock[0]
    await AccountService(manager, client.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "alice")
    assert conv["verification"]["state"] == "unverified"
    worker = Worker(manager, client.app.state.gateway, clock=manager.clock)
    await worker.monitor_contacts_once()
    await worker.verify_contacts_once()
    assert manager.conversation(conv["id"])["verification"]["state"] == "verified"
    original_epoch = manager.conversation(conv["id"])["epoch"]
    clock[0] += 1200
    for _ in range(3):
        assert client.app.state.manager.conversation(conv["id"])["verification"]["state"] == "stale"
    assert manager.conversation(conv["id"])["epoch"] == original_epoch
    await worker.monitor_contacts_once()
    assert manager.conversation(conv["id"])["epoch"] == original_epoch + 1
    await worker.monitor_contacts_once()
    assert manager.conversation(conv["id"])["epoch"] == original_epoch + 1
    await worker.verify_contacts_once()
    assert manager.conversation(conv["id"])["verification"]["state"] == "verified"
    client.post("/wechat/api/v1/auth/logout", headers=headers)
    assert client.get(f"/wechat/api/v1/conversations?account_id={aid}").status_code == 401


async def test_monitor_scans_beyond_frontend_limit_and_excludes_system_contacts(tmp_path):
    client, store, manager, aid, _ = setup_directory(tmp_path)
    await AccountService(manager, client.app.state.gateway).online(aid)
    with store.connect() as conn:
        conn.cursor().executemany(
            "INSERT INTO conversations(account_id,peer_id,kind,updated_at) VALUES(%s,%s,%s,%s)",
            [(aid, f"peer-{i}", "private", manager.clock()) for i in range(501)],
        )
    for peer in ("me", "weixin", "filehelper", "gh_public"):
        manager.ensure_conversation(aid, peer)
    worker = Worker(manager, client.app.state.gateway)
    assert await worker.monitor_contacts_once() == 501
    rows = client.get(f"/wechat/api/v1/conversations?account_id={aid}").json()
    assert len(rows) == 501
    with store.connect() as conn:
        monitored = conn.execute("SELECT peer_id FROM contact_monitor WHERE account_id=%s", (aid,)).fetchall()
    assert {row["peer_id"] for row in monitored} == {f"peer-{i}" for i in range(501)}


async def test_opening_or_events_cannot_bypass_persisted_failure_backoff(tmp_path):
    client, store, manager, aid, headers = setup_directory(tmp_path)
    clock = [manager.clock()]
    manager.clock = lambda: clock[0]
    await AccountService(manager, client.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "group@chatroom")
    calls = []

    async def failed(*args, **kwargs):
        calls.append(1)
        return {"ret": 500}

    client.app.state.gateway.call = failed
    worker = Worker(manager, client.app.state.gateway, clock=manager.clock)

    for delay in (600, 1200, 2400, 3600):
        queue_contact_check(client, conv["id"])
        assert await worker.verify_contacts_once()
        deadline = manager.conversation(conv["id"])["verification"]["next_at"]
        assert deadline == clock[0] + delay
        clock[0] += 61
        for _ in range(3):
            queue_contact_check(client, conv["id"])
        assert not await worker.verify_contacts_once()
        assert store.query("SELECT next_at FROM contact_verifications")[0]["next_at"] == deadline
        clock[0] = deadline
    assert len(calls) == 4
