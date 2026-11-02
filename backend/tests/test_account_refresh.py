import json

import pytest
from qingda.accounts import AccountService
from qingda.providers import ProviderError
from qingda.service import Manager
from qingda.store import Store
from test_accounts import AccountGateway
from test_database import database_url

NOW = 1791590400.0


def account_service(tmp_path):
    store = Store(database_url(tmp_path / "refresh.db"))
    clock = [NOW]
    manager = Manager(store, clock=lambda: clock[0])
    account = manager.add_account("new-terminal")
    manager.verify_identity("new-terminal", {"wxid": "new-wxid"})
    gateway = AccountGateway(manager)
    return store, manager, AccountService(manager, gateway), gateway, account["id"], clock


async def test_successful_refresh_keeps_numeric_gender_and_plain_signature(tmp_path):
    _, manager, service, gateway, aid, _ = account_service(tmp_path)
    original = gateway.call

    async def profile(path, payload):
        result = await original(path, payload)
        if path.endswith("getProfile"):
            result["data"].update(sex=2, signature="<b>认真生活</b>")
        return result

    gateway.call = profile
    await service.online(aid)
    profile = manager.accounts("active")[0]["profile"]
    assert profile["sex"] == 2
    assert profile["signature"] == "<b>认真生活</b>"
    assert profile["mobile"] == "private-mobile"


async def test_online_refresh_keeps_stopped_account_disabled_and_ai_off(tmp_path):
    store, manager, service, _, aid, _ = account_service(tmp_path)
    manager.configure_automation(aid, "原账号提示词", True)
    with store.transaction() as connection:
        manager.stop_account(aid, "stopped", connection)
    refreshed = await service.online(aid)
    assert refreshed["online"] == 1 and refreshed["last_successful_refresh_at"] == NOW
    assert refreshed["enabled"] == 0 and refreshed["ai_enabled"] == 0
    restored = Manager(Store(store.url)).account(aid)
    assert restored["enabled"] == 0 and restored["ai_enabled"] == 0


async def test_last_successful_refresh_is_durable_and_failures_do_not_advance_it(tmp_path):
    store, manager, service, gateway, aid, clock = account_service(tmp_path)
    assert manager.accounts("active")[0]["last_successful_refresh_at"] is None
    await service.online(aid)
    assert manager.accounts("active")[0]["last_successful_refresh_at"] == NOW
    clock[0] = NOW + 60
    gateway.is_online = False
    await service.online(aid)
    offline = manager.accounts("active")[0]
    assert offline["online"] == 0 and offline["last_successful_refresh_at"] == NOW + 60
    assert offline["profile"]["nickName"] == "真实昵称"

    async def unavailable(app_id):
        raise ProviderError("platform_check_rejected")

    gateway.online = unavailable
    clock[0] = NOW + 120
    with pytest.raises(ProviderError):
        await service.online(aid)
    restored = Manager(Store(store.url)).accounts("active")[0]
    assert restored["online"] is None
    assert restored["online_checked_at"] == NOW + 120
    assert restored["last_successful_refresh_at"] == NOW + 60


@pytest.mark.parametrize("failure", ["online", "profile", "identity", "late"])
async def test_first_failed_or_superseded_refresh_never_claims_success(tmp_path, failure):
    _, manager, service, gateway, aid, _ = account_service(tmp_path)
    original = gateway.call

    async def online(app_id):
        if failure == "online":
            raise ProviderError("platform_check_rejected")
        return True

    async def profile(path, payload):
        if failure == "profile":
            raise ProviderError("profile_unavailable")
        result = await original(path, payload)
        if failure == "identity":
            result["data"]["wxid"] = "another-wxid"
        if failure == "late":
            service.remove(aid)
        return result

    gateway.online, gateway.call = online, profile
    with pytest.raises((ProviderError, ValueError)):
        await service.online(aid)
    assert manager.account(aid)["last_successful_refresh_at"] is None


def test_reopen_does_not_invent_a_success_time(tmp_path):
    path = tmp_path / "existing-account"
    store = Store(database_url(path))
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO accounts(wxid,current_app_id,online_checked_at,created_at) VALUES(%s,%s,%s,%s)",
            ("existing-wxid", "existing-app", NOW, NOW - 100),
        )
    manager = Manager(Store(database_url(path)))
    account = manager.accounts()[0]
    assert account["last_successful_refresh_at"] is None
    assert account["wxid"] == "existing-wxid" and account["online_checked_at"] == NOW
    assert Manager(Store(database_url(path))).accounts()[0]["last_successful_refresh_at"] is None


async def test_offline_callback_does_not_advance_successful_query_time(tmp_path):
    store, manager, service, _, aid, clock = account_service(tmp_path)
    await service.online(aid)
    clock[0] = NOW + 60
    store.receive(
        json.dumps({"TypeName": "Offline", "Appid": "new-terminal", "Wxid": "new-wxid", "Data": {}}).encode(),
        now=clock[0],
    )
    manager.process_events(now=clock[0])
    account = manager.accounts("active")[0]
    assert account["online"] == 0 and account["last_successful_refresh_at"] == NOW
