import asyncio
import json

import pytest
from qingda.providers import ProviderError
from qingda.service import Manager
from qingda.store import Store
from qingda.worker import Worker
from test_automation import NOW
from test_database import database_url


class Gateway:
    def __init__(self):
        self.calls = []

    async def call(self, path, payload):
        self.calls.append((path, payload))
        assert path == "/personal/getProfile"
        return {"ret": 200, "data": {"wxid": "wxid_" + payload["appId"], "nickName": "发现账号"}}

    async def online(self, app_id):
        self.calls.append(("online", app_id))
        return True


def receive(store, manager, app_id="new-a", wxid=None, mid=1, now=NOW, **data):
    event = {
        "TypeName": "AddMsg",
        "Appid": app_id,
        "Wxid": wxid if wxid is not None else "wxid_" + app_id,
        "Data": {
            "NewMsgId": mid,
            "MsgType": 1,
            "FromUserName": {"string": "same-friend"},
            "ToUserName": {"string": wxid or "wxid_" + app_id},
            "Content": {"string": "自动发现验收消息"},
            "CreateTime": now,
            **data,
        },
    }
    eid = store.receive(json.dumps(event).encode(), now=now)
    manager.process_events(now=now)
    return eid


async def test_callbacks_discover_two_accounts_without_inheriting_automation(tmp_path):
    store = Store(database_url(tmp_path / "test.db"))
    manager = Manager(store, clock=lambda: NOW)
    gateway = Gateway()
    for app_id in ("new-a", "new-b"):
        receive(store, manager, app_id)
    assert manager.accounts("active") == []
    assert gateway.calls == []
    worker = Worker(manager, gateway, clock=lambda: NOW)
    assert await worker.discover_once() is True
    assert await worker.discover_once() is True
    accounts = manager.accounts("active")
    assert {a["wxid"] for a in accounts} == {"wxid_new-a", "wxid_new-b"}
    assert all(a["ai_enabled"] == 0 and a["online"] == 1 for a in accounts)
    assert all(a["profile"]["nickName"] == "发现账号" for a in accounts)
    assert all(a["last_successful_refresh_at"] == NOW for a in accounts)
    assert all(
        t["state"] == "skipped"
        for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    )
    assert len(store.query("SELECT * FROM messages WHERE platform_id='1'")) == 2
    calls = len(gateway.calls)
    receive(store, manager, "new-a")
    assert await worker.discover_once() is False
    assert len(gateway.calls) == calls
    worker.release()


async def test_conflicting_callback_never_enters_messages_tasks(tmp_path):
    store = Store(database_url(tmp_path / "test.db"))
    manager = Manager(store, clock=lambda: NOW)
    receive(store, manager)
    before = store.query("SELECT * FROM messages")
    tasks = store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    eid = receive(store, manager, wxid="wxid_wrong", mid=2, IsHistory=True)
    assert store.query("SELECT * FROM messages") == before
    assert store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100") == tasks
    assert store.events()[0]["parse_error"] == "account_identity_mismatch"
    assert store.event(eid)["Wxid"] == "wxid_wrong"
    worker = Worker(manager, Gateway(), clock=lambda: NOW)
    await worker.discover_once()
    before = store.query("SELECT * FROM messages")
    tasks = store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    receive(store, manager, wxid="wxid_wrong", mid=3)
    assert store.query("SELECT * FROM messages") == before
    assert store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100") == tasks
    worker.release()


@pytest.mark.parametrize("mode", ["mismatch", "archived", "changed_appid", "offline", "unknown"])
async def test_discovered_identity_is_checked_and_existing_history_is_retained(tmp_path, mode):
    store = Store(database_url(tmp_path / "test.db"))
    manager = Manager(store, clock=lambda: NOW)
    wxid = "wxid_old" if mode in ("archived", "changed_appid") else "wxid_new-a"
    previous = None
    if mode in ("archived", "changed_appid"):
        previous = manager.add_account("old-app")
        manager.verify_identity("old-app", {"wxid": wxid})
        receive(store, manager, "old-app", wxid=wxid, mid=7)
        if mode == "archived":
            with store.connect() as conn:
                conn.execute(
                    "UPDATE accounts SET archived_at=%s,enabled=0 WHERE id=%s", (NOW, previous["id"])
                )
    receive(store, manager, wxid=wxid, mid=7)
    gateway = Gateway()

    async def profile(path, payload):
        return {"ret": 200, "data": {"wxid": "wrong" if mode == "mismatch" else wxid}}

    async def online(app_id):
        if mode == "unknown":
            raise ProviderError("platform_check_rejected")
        return mode != "offline"

    gateway.call, gateway.online = profile, online
    worker = Worker(manager, gateway, clock=lambda: NOW)
    await worker.discover_once()
    accounts = manager.accounts("active")
    if mode in ("mismatch", "archived"):
        assert accounts == []
        assert await worker.discover_once() is False
    else:
        assert len(accounts) == 1 and accounts[0]["wxid"] == wxid
        assert accounts[0]["online"] == {"offline": 0, "unknown": None}.get(mode, 1)
        assert accounts[0]["last_successful_refresh_at"] == (None if mode == "unknown" else NOW)
        if previous:
            assert accounts[0]["id"] == previous["id"]
            assert accounts[0]["current_app_id"] == "new-a"
            assert len(store.query("SELECT * FROM messages WHERE account_id=%s", (previous["id"],))) == 1
    assert all(
        t["state"] == "skipped"
        for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    )
    worker.release()


async def test_slow_discovery_does_not_block_inbox_and_shutdown_is_recoverable(tmp_path):
    store = Store(database_url(tmp_path / "test.db"))
    manager = Manager(store, clock=lambda: NOW)
    existing = manager.add_account("existing")
    manager.verify_identity("existing", {"wxid": "wxid_existing"})
    receive(store, manager)
    started = asyncio.Event()
    gateway = Gateway()

    async def slow(*args):
        started.set()
        await asyncio.Event().wait()

    async def close():
        pass

    gateway.call, gateway.close = slow, close
    worker = Worker(manager, gateway, clock=lambda: NOW)
    running = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(started.wait(), 3)
        event = {
            "TypeName": "AddMsg",
            "Appid": "existing",
            "Wxid": "wxid_existing",
            "Data": {
                "NewMsgId": 10,
                "MsgType": 1,
                "FromUserName": {"string": "friend"},
                "ToUserName": {"string": "wxid_existing"},
                "Content": {"string": "业务继续"},
            },
        }
        store.receive(json.dumps(event).encode(), now=NOW)
        for _ in range(50):
            if store.query("SELECT 1 FROM messages WHERE account_id=%s", (existing["id"],)):
                break
            await asyncio.sleep(0.02)
        assert store.query("SELECT text FROM messages WHERE account_id=%s", (existing["id"],)) == [
            {"text": "业务继续"}
        ]
    finally:
        running.cancel()
        await asyncio.wait_for(running, 3)
    gateway = Gateway()
    restarted = Worker(manager, gateway, clock=lambda: NOW)
    assert await restarted.discover_once() is True
    assert len(manager.accounts("active")) == 2
    restarted.release()


@pytest.mark.parametrize(
    "app_id,wxid,data",
    [
        ("bad app", "wxid_valid", {}),
        (" app ", "wxid_valid", {}),
        ("missing-id", "", {}),
        ("history", "wxid_history", {"IsHistory": True}),
        ("test", "wxid_test", {"IsTest": True}),
    ],
)
async def test_invalid_or_non_live_identity_is_preserved_without_discovery(tmp_path, app_id, wxid, data):
    store = Store(database_url(tmp_path / "test.db"))
    manager = Manager(store, clock=lambda: NOW)
    receive(store, manager, app_id, wxid, **data)
    gateway = Gateway()
    worker = Worker(manager, gateway, clock=lambda: NOW)
    assert await worker.discover_once() is False
    assert gateway.calls == []
    assert len(store.events()) == 1 and store.events()[0]["processed"] == 1
    assert (
        manager.accounts("active") == []
        and store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100") == []
    )
    worker.release()


async def test_late_profile_cannot_restore_archived_candidate(tmp_path):
    from qingda.accounts import AccountService

    store = Store(database_url(tmp_path / "test.db"))
    manager = Manager(store, clock=lambda: NOW)
    receive(store, manager)
    gateway = Gateway()
    original = gateway.call
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args):
        started.set()
        await release.wait()
        return await original(*args)

    gateway.call = delayed
    worker = Worker(manager, gateway, clock=lambda: NOW)
    pending = asyncio.create_task(worker.discover_once())
    await started.wait()
    AccountService(manager, gateway).remove(manager.accounts()[0]["id"])
    release.set()
    await pending
    assert manager.accounts("active") == []
    assert len(manager.accounts("archived")) == 1
    assert manager.accounts("archived")[0]["wxid"] is None
    assert len(store.events()) == 1
    worker.release()


async def test_discovery_backoff_survives_restart_and_requires_fresh_callback_after_pause(tmp_path):
    now = [NOW]
    store = Store(database_url(tmp_path / "test.db"))
    manager = Manager(store, clock=lambda: now[0])
    gateway = Gateway()
    original = gateway.call
    failures = []

    async def failed(*args):
        failures.append(now[0])
        raise ProviderError("profile_unavailable")

    gateway.call = failed
    receive(store, manager)
    worker = Worker(manager, gateway, clock=lambda: now[0])
    for due in (0, 30, 150, 750):
        now[0] = NOW + due
        assert await worker.discover_once() is True
        assert manager.accounts("active") == []
        assert await worker.discover_once() is False
        worker.release()
        worker = Worker(manager, gateway, clock=lambda: now[0])
    assert failures == [NOW, NOW + 30, NOW + 150, NOW + 750]
    now[0] = NOW + 2549
    receive(store, manager, mid=2, now=now[0])
    assert await worker.discover_once() is False
    now[0] += 1
    assert await worker.discover_once() is False
    receive(store, manager, mid=2, now=now[0])
    assert await worker.discover_once() is False  # 重复投递不算新的回调事件。
    gateway.call = original
    receive(store, manager, mid=3, now=now[0])
    assert await worker.discover_once() is True
    assert len(manager.accounts("active")) == 1
    worker.release()
