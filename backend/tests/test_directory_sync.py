import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from business_fixtures import database_snapshot
from contact_fixtures import setup_directory
from wechat_manager.accounts import AccountService
from wechat_manager.automation_defaults import seed_directory
from wechat_manager.contacts import publish_directory
from wechat_manager.tenancy import user_scope
from wechat_manager.worker import Worker


async def setup(tmp_path):
    http, store, manager, aid, _ = setup_directory(tmp_path)
    clock = [1791590400.0]
    manager.clock = lambda: clock[0]
    await AccountService(manager, http.app.state.gateway).online(aid)
    worker = Worker(manager, http.app.state.gateway, clock=manager.clock)
    return http, store, manager, aid, clock, worker


@pytest.mark.parametrize("peer", ["alice", "saved@chatroom"])
async def test_message_created_conversation_defaults_on_and_preserves_manual_off(tmp_path, peer):
    _, store, manager, aid, clock, worker = await setup(tmp_path)
    conv = manager.ensure_conversation(aid, peer)
    assert conv["allowed"]
    manager.set_auto_reply(conv["id"], False)
    assert await worker.contacts_once()
    with store.transaction() as conn:
        assert publish_directory(conn, aid, clock[0]) == []
    assert not manager.ensure_conversation(aid, peer)["allowed"]
    assert not store.query("SELECT * FROM tasks")


@pytest.mark.parametrize("peer", ["me", "filehelper", "weixin", "gh_public"])
async def test_hidden_conversation_does_not_default_on(tmp_path, peer):
    _, _, manager, aid, _, _ = await setup(tmp_path)
    assert not manager.ensure_conversation(aid, peer)["allowed"]


async def test_zero_messages_directory_populates_scope_and_keeps_permissions(tmp_path):
    http, store, manager, aid, clock, worker = await setup(tmp_path)
    assert not store.query("SELECT * FROM conversations")
    assert await worker.contacts_once()
    friend = http.get(f"/wechat/api/v1/accounts/{aid}/reply-scope").json()
    groups = http.get(f"/wechat/api/v1/accounts/{aid}/reply-scope?kind=group").json()
    assert friend["total"] == 2 and groups["total"] == 1
    assert all(r["allowed"] for r in friend["items"] + groups["items"])
    assert len(http.get(f"/wechat/api/v1/conversations?account_id={aid}&kind=private").json()) == 2
    with store.connect() as conn:
        before = database_snapshot(conn)
    http.get(f"/wechat/api/v1/accounts/{aid}/reply-scope")
    with store.connect() as conn:
        assert database_snapshot(conn) == before
    alice = manager.ensure_conversation(aid, "alice")
    manager.set_auto_reply(alice["id"], True)
    bob = manager.ensure_conversation(aid, "bob")
    manager.set_auto_reply(bob["id"], False)
    with store.transaction() as conn:
        manager.takeover(alice["id"], conn)
        original = dict(conn.execute("SELECT * FROM conversations WHERE id=%s", (alice["id"],)).fetchone())
        assert publish_directory(conn, aid, clock[0]) == []
        # 后续初始化保留默认开启及手动关闭，不覆盖人工暂停。
        conn.execute(
            "INSERT INTO account_automation_state(account_id,wxid,initialized_at,ai_ready) VALUES(%s,'me',%s,0)",
            (aid, clock[0]),
        )
        seed_directory(manager, conn, aid)
        assert (
            dict(conn.execute("SELECT * FROM conversations WHERE id=%s", (alice["id"],)).fetchone())
            == original
        )
    assert not manager.ensure_conversation(aid, "bob")["allowed"]
    with user_scope(2), store.transaction() as conn:
        assert publish_directory(conn, aid, clock[0]) == []
    assert not store.query("SELECT * FROM message_records") and not store.query("SELECT * FROM tasks")
    assert not manager.account(aid)["ai_enabled"]


async def test_worker_refreshes_without_browser_and_retries_failed_sync(tmp_path):
    http, store, manager, aid, clock, worker = await setup(tmp_path)
    gateway = http.app.state.gateway
    assert await worker.contacts_once()
    first = len([p for p, _ in gateway.calls if p.endswith("fetchContactsList")])
    clock[0] += 599
    assert not await worker.contacts_once()
    clock[0] += 1
    assert await worker.contacts_once()
    assert len([p for p, _ in gateway.calls if p.endswith("fetchContactsList")]) == first + 1
    old_count = len(store.query("SELECT * FROM contact_entries"))
    gateway.fail = True
    clock[0] += 600
    assert await worker.contacts_once()
    assert len(store.query("SELECT * FROM contact_entries")) == old_count
    assert store.query("SELECT state FROM account_contacts")[0]["state"] == "failed"
    gateway.fail = False
    clock[0] += 600
    assert await worker.contacts_once()
    assert store.query("SELECT state FROM account_contacts")[0]["state"] == "idle"


async def test_offline_account_recovers_and_syncs_without_webpage(tmp_path):
    http, store, manager, aid, clock, worker = await setup(tmp_path)
    with store.transaction() as conn:
        conn.execute("UPDATE accounts SET online=0,online_checked_at=%s WHERE id=%s", (clock[0], aid))
    await worker.contacts_once()
    assert not store.query("SELECT * FROM conversations")
    clock[0] += 600
    assert await worker.contacts_once()
    assert manager.account(aid)["online"] == 1
    assert len(store.query("SELECT * FROM conversations")) == 3
    assert not store.query("SELECT * FROM tasks")


async def test_profiles_use_separate_contracts_and_failed_items_retry_without_losing_names(tmp_path):
    http, store, manager, aid, clock, worker = await setup(tmp_path)
    gateway = http.app.state.gateway
    await worker.contacts_once()
    original = gateway.call
    failures = [True]

    async def call(path, payload, **kwargs):
        if path.endswith("getDetailInfo") and failures[0]:
            return {
                "ret": 200,
                "data": [
                    {"userName": "alice", "nickName": "昵称", "remark": "优先备注"},
                    {"userName": "foreign", "nickName": "越界"},
                ],
            }
        return await original(path, payload, **kwargs)

    gateway.call = call
    assert await worker.contact_profiles_once()
    assert await worker.contact_profiles_once()
    assert not await worker.contact_profiles_once()
    rows = {r["peer_id"]: r for r in store.query("SELECT * FROM contact_entries")}
    assert rows["alice"]["display_name"] == "优先备注"
    assert rows["bob"]["profile_state"] == "failed"
    assert rows["saved@chatroom"]["display_name"] == "保存的群聊"
    assert not rows["saved@chatroom"]["profile_json"].__contains__("memberList")
    assert not store.query("SELECT * FROM chat_member_profiles")
    assert not any(p.endswith("getBriefInfo") for p, _ in gateway.calls)
    clock[0] += 599
    assert not await worker.contact_profiles_once()
    failures[0] = False
    clock[0] += 1
    assert await worker.contact_profiles_once()
    assert (
        store.query("SELECT profile_state FROM contact_entries WHERE peer_id='bob'")[0]["profile_state"]
        == "ready"
    )
    assert not store.query("SELECT profile_owner FROM account_contacts")[0]["profile_owner"]


async def test_incomplete_result_and_old_cache_do_not_prune_current_entries(tmp_path):
    http, store, manager, aid, clock, worker = await setup(tmp_path)
    await worker.contacts_once()
    gateway = http.app.state.gateway
    before = store.query("SELECT peer_id FROM contact_entries ORDER BY peer_id")

    async def incomplete(*args, **kwargs):
        return {"ret": 200, "data": {"friends": [], "chatrooms": [], "hasMore": True}}

    gateway.call = incomplete
    clock[0] += 600
    await worker.contacts_once()
    assert store.query("SELECT peer_id FROM contact_entries ORDER BY peer_id") == before
    with store.transaction() as conn:
        conn.execute("UPDATE account_contacts SET attempted_at=0,state='queued'")

    async def cache(path, payload, **kwargs):
        assert path.endswith("fetchContactsListCache")
        return {"ret": 200, "data": {"friends": [], "chatrooms": []}}

    gateway.call = cache
    await worker.contacts_once()
    assert store.query("SELECT peer_id FROM contact_entries ORDER BY peer_id") == before


def actor(store, mode, aid, now):
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("directory_sync_actor.py")), mode],
        env={
            **os.environ,
            "DIRECTORY_ACTOR_USER": "1",
            "DIRECTORY_ACTOR_DATABASE": store.url,
            "DIRECTORY_ACTOR_PAYLOAD": json.dumps({"aid": aid, "now": now}),
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def test_two_processes_publish_once_and_recover_crashed_pull_with_cache(tmp_path):
    http, store, manager, aid, clock, worker = await setup(tmp_path)
    crashed = actor(store, "crash-sync", aid, clock[0])
    try:
        assert crashed.stdout.readline().strip() == "requested"
        busy = actor(store, "sync", aid, clock[0])
        stdout, stderr = busy.communicate(timeout=20)
        assert busy.returncode == 0, stderr
        assert json.loads(stdout) == {"busy": True, "calls": []}
        crashed.communicate("crash\n", timeout=15)
        assert crashed.returncode == 23
    finally:
        if crashed.poll() is None:
            crashed.kill()
            crashed.wait()
    clock[0] += 121
    recovered = actor(store, "sync", aid, clock[0])
    stdout, stderr = recovered.communicate(timeout=20)
    assert recovered.returncode == 0, stderr
    result = json.loads(stdout)
    assert result["worked"] and result["calls"] == ["/contacts/fetchContactsListCache"]
    assert len(store.query("SELECT * FROM conversations")) == 3
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO contact_entries(account_id,peer_id,kind,display_name,sort_key,owner_wxid) VALUES(%s,'fresh','friend','新好友','fresh','me')",
            (aid,),
        )
    processes = [actor(store, "publish", aid, clock[0]) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda p: p.communicate("\n", timeout=25), processes))
    assert all(p.returncode == 0 for p in processes), results
    assert sorted(json.loads(out)["changed"] for out, _ in results) == [0, 1]
    assert len(store.query("SELECT * FROM conversations")) == 4
    fresh = store.query("SELECT id,allowed FROM conversations WHERE peer_id='fresh'")[0]
    assert fresh["allowed"] == 1
    assert store.query(
        "SELECT enabled FROM conversation_preferences WHERE conversation_id=%s", (fresh["id"],)
    )[0]["enabled"]
    assert not store.query("SELECT * FROM tasks")
    assert store.pool.get_stats()["pool_available"] == store.pool.get_stats()["pool_size"]
