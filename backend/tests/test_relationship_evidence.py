"""真实服务边界复现近期核实证据丢失；平台响应均为隔离夹具。"""

import pytest
from business_fixtures import allow_reply
from contact_fixtures import setup_directory
from wechat_manager.accounts import AccountService
from wechat_manager.worker import Worker


async def test_recent_full_contacts_proof_prevents_initial_pending_and_duplicate_pull(tmp_path):
    client, store, manager, aid, headers = setup_directory(tmp_path)
    clock = [manager.clock()]
    manager.clock = lambda: clock[0]
    gateway = client.app.state.gateway
    original = gateway.call

    async def call(path, payload, **kwargs):
        if path.endswith("fetchContactsListCache"):
            return {"ret": 404}
        response = await original(path, payload, **kwargs)
        if path.endswith("fetchContactsList"):
            # 本用例只验证好友证据；默认开启的群聊需另行核实成员。
            response["data"]["chatrooms"] = []
        return response

    gateway.call = call
    await AccountService(manager, gateway).online(aid)
    worker = Worker(manager, gateway, clock=manager.clock)
    assert await worker.contacts_once()
    verified_at = clock[0]
    clock[0] += 180
    conv = manager.ensure_conversation(aid, "alice")
    allow_reply(manager, conv["id"], True)
    epoch = manager.conversation(conv["id"])["epoch"]
    await worker.monitor_contacts_once()
    row = client.app.state.manager.conversation(conv["id"])
    assert row["verification"]["state"] == "verified"
    assert row["verification"]["verified_at"] == verified_at
    assert row["verification"]["next_at"] == verified_at + 600
    assert row["allowed"] and row["epoch"] == epoch
    assert not await worker.verify_contacts_once()
    assert sum(path.endswith("fetchContactsList") for path, _ in gateway.calls) == 1
    rows = client.get(f"/wechat/api/v1/conversations?account_id={aid}").json()
    assert all(row["verification"]["state"] in ("verified", "excluded") for row in rows)
    worker.release()
    restarted = Worker(
        type(manager)(type(store)(store.url), clock=manager.clock), gateway, clock=manager.clock
    )
    await restarted.monitor_contacts_once()
    assert not await restarted.verify_contacts_once()
    assert manager.conversation(conv["id"])["verification"]["verified_at"] == verified_at


async def prepare_contacts_proof(tmp_path):
    client, store, manager, aid, headers = setup_directory(tmp_path)
    clock = [manager.clock()]
    manager.clock = lambda: clock[0]
    gateway = client.app.state.gateway
    original = gateway.call
    result = {"fail": False, "friends": ["alice", "bob"]}

    async def call(path, payload, **kwargs):
        if path.endswith("fetchContactsListCache"):
            return {"ret": 404}
        response = await original(path, payload, **kwargs)
        if path.endswith("fetchContactsList"):
            return (
                {"ret": 500}
                if result["fail"]
                else {"ret": 200, "data": {"friends": result["friends"], "chatrooms": []}}
            )
        return response

    gateway.call = call
    await AccountService(manager, gateway).online(aid)
    worker = Worker(manager, gateway, clock=manager.clock)
    assert await worker.contacts_once()
    return client, store, manager, aid, headers, clock, gateway, worker, result


async def test_contact_cache_alone_never_becomes_relationship_proof(tmp_path):
    client, store, manager, aid, _ = setup_directory(tmp_path)
    gateway = client.app.state.gateway
    original = gateway.call

    async def cached(path, payload, **kwargs):
        response = await original(path, payload, **kwargs)
        if path.endswith(("fetchContactsList", "fetchContactsListCache")):
            response["data"]["isCache"] = True
        return response

    gateway.call = cached
    await AccountService(manager, gateway).online(aid)
    worker = Worker(manager, gateway)
    assert await worker.contacts_once()
    conv = manager.ensure_conversation(aid, "alice")
    await worker.monitor_contacts_once()
    assert manager.conversation(conv["id"])["verification"]["state"] == "unverified"


@pytest.mark.parametrize("peer", ["alice", "unsaved@chatroom"])
@pytest.mark.parametrize("held", [False, True])
async def test_ai_configuration_does_not_invalidate_same_wechat_identity(tmp_path, peer, held):
    client, store, manager, aid, headers, clock, gateway, worker, result = await prepare_contacts_proof(
        tmp_path
    )
    original = gateway.call

    async def call(path, payload, **kwargs):
        if path.endswith("getChatroomMemberList"):
            gateway.calls.append((path, payload))
            return {"ret": 200, "data": {"memberList": [{"wxid": "me"}, {"wxid": "other"}]}}
        return await original(path, payload, **kwargs)

    gateway.call = call
    conv = manager.ensure_conversation(aid, peer)
    await worker.monitor_contacts_once()
    while await worker.verify_contacts_once():
        pass
    before = manager.conversation(conv["id"])
    calls = len(gateway.calls)
    clock[0] += 5
    manager.configure_model({"model": "deepseek-flash"}, api_key="isolated-test-key")
    manager.configure_automation(aid, prompt="新的回复规则", ai_enabled=True)
    # 兼容旧版已因配置版本变化错误暂停的监测行。
    if held:
        with store.connect() as conn:
            conn.execute("UPDATE contact_monitor SET held=1 WHERE account_id=%s AND peer_id=%s", (aid, peer))
    assert manager.conversation(conv["id"])["verification"]["state"] == "verified"
    await worker.monitor_contacts_once()
    row = manager.conversation(conv["id"])
    assert row["verification"]["state"] == "verified"
    assert row["verification"]["verified_at"] <= before["verification"]["verified_at"]
    assert not await worker.verify_contacts_once() and len(gateway.calls) == calls
    assert row["epoch"] == before["epoch"] + int(held)
    assert store.query("SELECT held FROM contact_monitor WHERE peer_id=%s", (peer,))[0]["held"] == 0


async def test_config_change_during_relationship_query_keeps_result_but_reply_policy_stays_disabled(tmp_path):
    from test_contact_removal import delete_event

    client, store, manager, aid, headers = setup_directory(tmp_path)
    await AccountService(manager, client.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "unsaved@chatroom")
    delete_event(store, manager, conv["peer_id"])

    async def call(*args, **kwargs):
        manager.configure_automation(aid, prompt="新的配置", ai_enabled=False)
        return {"ret": 200, "data": {"memberList": [{"wxid": "me"}]}}

    client.app.state.gateway.call = call
    worker = Worker(manager, client.app.state.gateway)
    await worker.monitor_contacts_once()
    assert await worker.verify_contacts_once()
    row = manager.conversation(conv["id"])
    assert row["verification"]["state"] == "verified" and row["relationship_status"] == "active"
    assert not row["allowed"] and row["auto_reply_status"] == "ai_disabled"
