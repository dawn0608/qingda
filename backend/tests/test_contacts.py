import asyncio
import json

import pytest
from auth_fixtures import login_response
from test_account_refresh import NOW, account_service
from wechat_manager.contacts import contact_counts
from wechat_manager.providers import ProviderError
from wechat_manager.worker import Worker


def test_counts_exclude_system_self_public_and_groups_without_requiring_wxid_prefix():
    result = {
        "ret": 200,
        "data": {
            "friends": [
                "alice",
                "alice",
                "wxid_bob",
                "myself",
                "filehelper",
                "weixin",
                "tmessage",
                "medianote",
                "qmessage",
                "qqmail",
                "qqsafe",
                "exmail_tool",
                "gh_public",
                "123@chatroom",
            ],
            "chatrooms": ["123@chatroom", "123@chatroom", "456@chatroom"],
        },
    }
    assert contact_counts(result, "myself") == (2, 2)
    assert contact_counts({"ret": 200, "data": {"friends": [], "chatrooms": []}}, "me") == (0, 0)
    with pytest.raises(ProviderError):
        contact_counts({"ret": 200, "msg": "操作成功"}, "me")


async def test_online_refresh_queues_counts_without_waiting_and_worker_persists_them(tmp_path):
    store, manager, service, gateway, aid, clock = account_service(tmp_path)
    original = gateway.call

    async def call(path, payload, **kwargs):
        if path.endswith("fetchContactsListCache"):
            return {"ret": 200}
        if path.endswith("fetchContactsList"):
            return {
                "ret": 200,
                "data": {"friends": ["alice", "alice", "weixin"], "chatrooms": ["a@chatroom"]},
            }
        return await original(path, payload)

    gateway.call = call
    row = await service.online(aid)
    assert row["profile"]["mobile"] == "private-mobile"
    assert row["contacts"] == {
        "friend_count": None,
        "chatroom_count": None,
        "updated_at": None,
        "state": "queued",
    }
    worker = Worker(manager, gateway, clock=lambda: clock[0])
    assert await worker.contacts_once()
    assert manager.accounts("active")[0]["contacts"] == {
        "friend_count": 1,
        "chatroom_count": 1,
        "updated_at": NOW,
        "state": "idle",
    }
    restored = type(manager)(type(store)(store.url), clock=lambda: clock[0])
    assert restored.accounts("active")[0]["contacts"]["friend_count"] == 1
    await service.online(aid)
    assert not await worker.contacts_once()


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {"ret": 200},
        {"ret": 500},
        {"ret": 200, "data": {"friends": [], "chatrooms": None}},
        {"ret": 200, "data": {"friends": [1], "chatrooms": []}},
        {"ret": 200, "data": {"friends": [], "chatrooms": ["not-a-group"]}},
    ],
)
def test_missing_or_malformed_results_never_claim_zero(result):
    with pytest.raises(ProviderError):
        contact_counts(result, "me")


async def test_full_pull_timeout_reads_cache_without_repeating_full_pull(tmp_path):
    _, manager, service, gateway, aid, clock = account_service(tmp_path)
    original = gateway.call
    calls = []

    async def call(path, payload, **kwargs):
        if "/contacts/" not in path:
            return await original(path, payload)
        calls.append(path)
        if path.endswith("fetchContactsList"):
            raise ProviderError("platform_response_unknown", uncertain=True)
        if len(calls) == 1:
            return {"ret": 200}
        return {"ret": 200, "data": {"friends": [], "chatrooms": []}}

    gateway.call = call
    await service.online(aid)
    worker = Worker(manager, gateway, clock=lambda: clock[0])
    await worker.contacts_once()
    assert [p.rsplit("/", 1)[1] for p in calls] == [
        "fetchContactsList",
        "fetchContactsListCache",
    ]
    assert manager.accounts()[0]["contacts"]["friend_count"] == 0


async def test_first_failure_and_total_timeout_keep_counts_unknown(tmp_path, monkeypatch):
    import wechat_manager.contacts as contacts

    monkeypatch.setattr(contacts, "TASK_TIMEOUT", 0.02)
    _, manager, service, gateway, aid, clock = account_service(tmp_path)
    original = gateway.call
    calls = []

    async def call(path, payload, **kwargs):
        if "/contacts/" not in path:
            return await original(path, payload)
        calls.append(path)
        raise ProviderError("platform_response_unknown", uncertain=True)

    gateway.call = call
    await service.online(aid)
    worker = Worker(manager, gateway, clock=lambda: clock[0])
    await worker.contacts_once()
    assert manager.accounts()[0]["contacts"] == {
        "friend_count": None,
        "chatroom_count": None,
        "updated_at": None,
        "state": "failed",
    }
    assert len(calls) == 2


@pytest.mark.parametrize("cached", [True, False])
async def test_restart_recovers_from_cache_without_another_full_pull(tmp_path, cached):
    store, manager, service, gateway, aid, clock = account_service(tmp_path)
    original = gateway.call
    entered = asyncio.Event()

    async def call(path, payload, **kwargs):
        if "/contacts/" not in path:
            return await original(path, payload)
        if path.endswith("fetchContactsListCache"):
            return {"ret": 200}
        entered.set()
        await asyncio.Event().wait()

    gateway.call = call
    await service.online(aid)
    first = Worker(manager, gateway, clock=lambda: clock[0])
    running = asyncio.create_task(first.contacts_once())
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    first.release()
    restored = type(manager)(type(store)(store.url), clock=lambda: clock[0])
    paths = []

    async def recovered(path, payload, **kwargs):
        paths.append(path)
        assert path.endswith("fetchContactsListCache")
        return {"ret": 200, "data": {"friends": ["alice"], "chatrooms": []}} if cached else {"ret": 200}

    gateway.call = recovered
    second = Worker(restored, gateway, clock=lambda: clock[0])
    assert await second.contacts_once()
    result = restored.accounts()[0]["contacts"]
    assert result["state"] == ("idle" if cached else "failed")
    assert result["friend_count"] == (1 if cached else None)
    assert len(paths) == 1


async def test_slow_contacts_do_not_block_events_and_late_identity_result_is_discarded(tmp_path):
    store, manager, service, gateway, aid, clock = account_service(tmp_path)
    original = gateway.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def call(path, payload, **kwargs):
        if "/contacts/" not in path:
            return await original(path, payload)
        entered.set()
        await release.wait()
        return {"ret": 200, "data": {"friends": ["alice"], "chatrooms": []}}

    gateway.call = call
    await service.online(aid)
    worker = Worker(manager, gateway, clock=lambda: clock[0])
    running = asyncio.create_task(worker.contacts_once())
    await entered.wait()
    store.receive(
        json.dumps({"TypeName": "Probe", "Appid": "new-terminal", "Wxid": "new-wxid", "Data": {}}).encode(),
        now=clock[0],
    )
    assert await worker.once() == 0
    assert store.query("SELECT processed FROM events")[0]["processed"] == 1
    service.remove(aid)
    release.set()
    await running
    assert manager.accounts("archived")[0]["contacts"]["friend_count"] is None


async def test_duplicate_triggers_and_multiple_accounts_share_one_contacts_query_at_a_time(tmp_path):
    _, manager, service, gateway, aid, clock = account_service(tmp_path)
    other = manager.add_account("second-terminal")
    manager.verify_identity("second-terminal", {"wxid": "second-wxid"})
    active, peak, calls = 0, 0, []

    async def call(path, payload, **kwargs):
        nonlocal active, peak
        if path.endswith("getProfile"):
            return {
                "ret": 200,
                "data": {"wxid": "new-wxid" if payload["appId"] == "new-terminal" else "second-wxid"},
            }
        calls.append(payload["appId"])
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            "ret": 200,
            "data": {
                "friends": ["alice"] if payload["appId"] == "new-terminal" else ["alice", "bob"],
                "chatrooms": [],
            },
        }

    gateway.call = call
    await service.online(aid)
    await service.online(aid)
    await service.online(other["id"])
    worker = Worker(manager, gateway, clock=lambda: clock[0])
    await asyncio.gather(worker.contacts_once(), worker.contacts_once())
    assert peak == 1 and len(calls) == 2
    assert [a["contacts"]["friend_count"] for a in manager.accounts("active")] == [1, 2]


def test_accounts_mobile_requires_login_and_never_enters_audit(tmp_path):
    from test_http import client

    c, store = client(tmp_path)
    response = c.get("/wechat/api/v1/accounts")
    assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
    login_response(c)
    from wechat_manager.service import Manager

    manager = Manager(store)
    account = manager.add_account("mobile-test")
    manager.verify_identity("mobile-test", {"wxid": "mobile-wxid"})
    with store.connect() as conn:
        conn.execute(
            "UPDATE accounts SET profile_json=%s WHERE id=%s",
            (json.dumps({"mobile": "13800000000"}), account["id"]),
        )
    response = c.get("/wechat/api/v1/accounts")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()[0]["profile"]["mobile"] == "13800000000"
    assert "13800000000" not in json.dumps(store.query("SELECT * FROM audit"))


async def test_failure_preserves_counts_and_automatically_retries_after_cooldown(tmp_path):
    _, manager, service, gateway, aid, clock = account_service(tmp_path)
    original = gateway.call
    calls = []
    failing = False

    async def call(path, payload, **kwargs):
        if "/contacts/" not in path:
            return await original(path, payload)
        calls.append(path)
        if failing:
            clock[0] += 20
            raise ProviderError("platform_check_rejected")
        return {"ret": 200, "data": {"friends": ["alice"], "chatrooms": []}}

    gateway.call = call
    worker = Worker(manager, gateway, clock=lambda: clock[0])
    await service.online(aid)
    await worker.contacts_once()
    failing = True
    clock[0] = NOW + 601
    await service.online(aid)
    await worker.contacts_once()
    assert manager.accounts()[0]["contacts"] == {
        "friend_count": 1,
        "chatroom_count": 0,
        "updated_at": NOW,
        "state": "failed",
    }
    failed_at = clock[0]
    clock[0] = failed_at + 599
    await service.online(aid)
    assert not await worker.contacts_once()
    clock[0] = failed_at + 600
    assert await worker.contacts_once()
