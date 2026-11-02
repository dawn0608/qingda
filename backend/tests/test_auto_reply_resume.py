import uuid

import pytest
from auth_fixtures import login_response
from business_fixtures import allow_reply, phone_takeover
from test_automation import NOW, deliver, setup


def test_explicit_resume_clears_pause_without_replaying_old_messages(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    deliver(store, manager, 1)
    phone_takeover(manager, conv["id"], "人工回复", str(uuid.uuid4()))
    assert manager.conversation(conv["id"])["auto_reply_status"] == "paused"
    manager.clock = lambda: NOW + 60
    resumed = manager.set_auto_reply(conv["id"], True)
    assert resumed["pause_until"] == 0
    assert resumed["allowed"] == 1
    assert resumed["auto_reply_status"] == "enabled"
    assert resumed["server_time"] == NOW + 60
    deliver(store, manager, 2, created=NOW + 30, received=NOW + 61)
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["reason"]
        == "before_watermark"
    )
    deliver(store, manager, 3, created=NOW + 62, received=NOW + 63)
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "queued"
    )
    assert (
        next(
            t
            for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
            if t["message_id"] == 1
        )["state"]
        == "canceled"
    )


def test_repeat_manual_reply_extends_pause_and_expiry_keeps_old_messages_skipped(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    phone_takeover(manager, conv["id"], "第一次", str(uuid.uuid4()))
    manager.clock = lambda: NOW + 100
    phone_takeover(manager, conv["id"], "继续人工", str(uuid.uuid4()))
    assert manager.conversation(conv["id"])["pause_until"] == NOW + 1900
    manager.clock = lambda: NOW + 1901
    # 文件传输助手无关系证据过期限制，用于只验证暂停到期边界。
    peer = manager.ensure_conversation(account["id"], "filehelper")
    manager.clock = lambda: NOW
    allow_reply(manager, peer["id"], True)
    phone_takeover(manager, peer["id"], "人工", str(uuid.uuid4()))
    manager.clock = lambda: NOW + 1800
    assert manager.conversation(peer["id"])["auto_reply_status"] == "enabled"
    deliver(store, manager, 90, peer="filehelper", created=NOW + 1799, received=NOW + 1801)
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["reason"]
        == "before_watermark"
    )
    deliver(store, manager, 91, peer="filehelper", created=NOW + 1801, received=NOW + 1802)
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "queued"
    )


def test_account_explicit_enable_resumes_only_its_pauses_and_prompt_save_does_not(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    other = manager.add_account("other-device")
    manager.verify_identity("other-device", {"wxid": "other-self"})
    manager.configure_automation(other["id"], prompt="其他账号", ai_enabled=True)
    second = manager.ensure_conversation(other["id"], "other-peer")
    allow_reply(manager, second["id"], True)
    phone_takeover(manager, conv["id"], "人工", str(uuid.uuid4()))
    phone_takeover(manager, second["id"], "其他人工", str(uuid.uuid4()))
    assert manager.account(account["id"])["paused_conversation_count"] == 1
    manager.configure_automation(account["id"], prompt="仅保存提示词")
    assert manager.conversation(conv["id"])["pause_until"] == NOW + 1800
    manager.configure_automation(account["id"], ai_enabled=False)
    assert manager.conversation(conv["id"])["pause_until"] == NOW + 1800
    manager.clock = lambda: NOW + 20
    manager.configure_automation(account["id"], ai_enabled=True)
    assert manager.conversation(conv["id"])["auto_reply_status"] == "enabled"
    assert manager.account(account["id"])["paused_conversation_count"] == 0
    assert manager.conversation(second["id"])["pause_until"] == NOW + 1800
    assert manager.account(other["id"])["paused_conversation_count"] == 1


def test_disabling_paused_conversation_does_not_resume_at_expiry_or_account_enable(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    phone_takeover(manager, conv["id"], "人工", str(uuid.uuid4()))
    row = manager.set_auto_reply(conv["id"], False)
    assert row["allowed"] == 0 and row["pause_until"] == 0
    manager.configure_automation(account["id"], ai_enabled=True)
    manager.clock = lambda: NOW + 1900
    assert manager.conversation(conv["id"])["auto_reply_status"] == "not_allowed"


@pytest.mark.parametrize("blocker", ["ai_disabled", "account_offline", "relationship_unverified"])
def test_resume_does_not_hide_other_blockers(tmp_path, blocker):
    store, manager, account, conv = setup(tmp_path)
    phone_takeover(manager, conv["id"], "人工", str(uuid.uuid4()))
    if blocker == "ai_disabled":
        manager.configure_automation(account["id"], ai_enabled=False)
    elif blocker == "account_offline":
        with store.transaction() as conn:
            conn.execute("UPDATE accounts SET online=0 WHERE id=%s", (account["id"],))
    else:
        with store.transaction() as conn:
            conn.execute("DELETE FROM contact_monitor")
    row = manager.set_auto_reply(conv["id"], True)
    assert row["pause_until"] == 0
    assert row["auto_reply_status"] == blocker
    if blocker == "ai_disabled":
        assert manager.account(account["id"])["ai_enabled"] == 0


def test_summary_counts_beyond_first_500_conversations(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    with store.transaction() as conn:
        for i in range(501):
            row = manager.ensure_conversation(account["id"], f"peer-{i}", conn)
            conn.execute("UPDATE conversations SET allowed=1 WHERE id=%s", (row["id"],))
            manager.takeover(row["id"], conn)
    assert manager.account(account["id"])["paused_conversation_count"] == 501
    summary = next(row for row in manager.accounts() if row["id"] == account["id"])
    assert summary["paused_conversation_count"] == 501
    assert summary["next_pause_until"] == NOW + 1800
    assert summary["server_time"] == NOW


def test_resume_api_requires_auth_csrf_and_returns_current_status(tmp_path):
    from test_automation import verified_fixture
    from test_http import client

    http, store = client(tmp_path)
    manager = http.app.state.manager
    manager.clock = lambda: NOW
    manager.configure_model({}, api_key="isolated-only")
    account = manager.add_account("api-device")
    manager.verify_identity("api-device", {"wxid": "api-self"})
    manager.configure_automation(account["id"], prompt="测试", ai_enabled=True)
    conv = manager.ensure_conversation(account["id"], "api-peer")
    allow_reply(manager, conv["id"], True)
    verified_fixture(manager, conv)
    phone_takeover(manager, conv["id"], "人工", str(uuid.uuid4()))
    url = f"/wechat/api/v1/conversations/{conv['id']}/auto-reply"
    assert http.patch(url, json={"enabled": True}).status_code == 401
    csrf = login_response(http).json()["csrf"]
    assert http.patch(url, json={"enabled": True}).status_code == 403
    response = http.patch(url, json={"enabled": True}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.json()["auto_reply_status"] == "enabled"
    # 退役白名单接口返回 404，不改变暂停。
    phone_takeover(manager, conv["id"], "再次人工", str(uuid.uuid4()))
    response = http.patch(
        url.replace("auto-reply", "allowlist"), json={"allowed": True}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 404
    assert manager.conversation(conv["id"])["pause_until"] == NOW + 1800


async def test_resume_during_generation_invalidates_old_result(tmp_path):
    from qingda.worker import Worker
    from test_worker import Gateway

    store, manager, account, conv = setup(tmp_path)
    deliver(store, manager, 1)
    gateway = Gateway()
    original = gateway.model

    async def resume(*args):
        manager.set_auto_reply(conv["id"], True)
        return await original(*args)

    gateway.model = resume
    worker = Worker(manager, gateway, clock=lambda: NOW + 10)
    await worker.once()
    assert not gateway.sends
    assert (
        store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")[0]["state"]
        == "canceled"
    )
    worker.release()
