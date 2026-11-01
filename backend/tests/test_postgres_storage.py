"""长期聊天保留、单份正文及跨进程恢复验收。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_automation import NOW, deliver, setup
from test_database import database_url
from wechat_manager.media_references import record_outbound


@pytest.mark.parametrize(
    "group,enabled,allowed",
    [(False, False, False), (False, True, False), (True, False, False), (True, True, False)],
)
def test_all_chat_is_saved_independent_of_assistant(tmp_path, group, enabled, allowed):
    store, manager, account, conv = setup(tmp_path)
    manager.configure_automation(account["id"], ai_enabled=enabled)
    peer = "members@chatroom" if group else "friend"
    conversation = manager.ensure_conversation(account["id"], peer)
    manager.set_auto_reply(conversation["id"], allowed)
    body = "member-a:\n始终保存这条聊天" if group else "始终保存这条聊天"
    deliver(store, manager, 123, peer=peer, Content={"string": body})
    row = store.query("SELECT * FROM messages WHERE platform_id='123'")[0]
    assert row["text"] == "始终保存这条聊天"
    assert row["sender"] == ("member-a" if group else "friend")
    payload = json.loads(store.query("SELECT payload FROM events")[0]["payload"])
    assert "Content" not in payload["Data"]
    assert store.query("SELECT count(*) AS count FROM message_contents")[0]["count"] == 1


def test_media_descriptors_keep_urls_without_embedded_base64(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    manager.configure_automation(account["id"], ai_enabled=False)
    xml = '<msg><img aeskey="isolated-key" cdnmidimgurl="cdn-locator" /></msg>'
    deliver(
        store,
        manager,
        123,
        MsgType=3,
        Content={"string": xml},
        ImgBuf={"buffer": "EXCLUDED_BASE64", "iLen": 500},
    )
    assert len(store.query("SELECT * FROM messages")) == 1
    resource = store.query("SELECT * FROM media_resources")[0]
    assert resource["msg_id"] == ""
    assert json.loads(resource["urls"])[0]["value"] == "cdn-locator"
    assert store.query("SELECT xml FROM message_contents")[0]["xml"] == xml
    assert "EXCLUDED_BASE64" not in store.query("SELECT payload FROM events")[0]["payload"]
    assert store.query("SELECT * FROM incoming_media") == []


def test_exact_outbound_receipt_merges_phone_copy_without_duplicate_body(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    with store.transaction() as conn:
        task = manager.create_task(conn, "auto", account, conv, content="已确认发送", state="accepted")
        record_outbound(store, conn, task, {"data": {"newMsgId": "123"}}, "accepted", NOW + 1)
    deliver(
        store,
        manager,
        123,
        sender="wxid_me",
        ToUserName={"string": "friend"},
        Content={"string": "已确认发送"},
    )
    rows = store.query("SELECT * FROM messages")
    assert len(rows) == 1 and rows[0]["text"] == "已确认发送"
    assert rows[0]["event_id"] is not None
    assert store.query("SELECT * FROM message_contents") == []
    assert manager.conversation(conv["id"])["pause_until"] == 0


def test_two_processes_crash_lease_expiry_and_unknown_send_do_not_replay(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    deliver(store, manager, 1)
    actor = Path(__file__).with_name("pg_process_actor.py")
    env = {
        **os.environ,
        "ACTOR_DATABASE_URL": store.url,
        "ACTOR_CLOCK": str(NOW + 10),
        "PGAPPNAME": "wechat-manager-isolated-actor",
    }
    first = subprocess.run(
        [sys.executable, str(actor), "crash"], env=env, capture_output=True, text=True, timeout=30
    )
    assert first.returncode == 23, first.stderr
    assert (
        store.query("SELECT count(*) AS count FROM audit WHERE action='isolated_send_boundary'")[0]["count"]
        == 1
    )
    second = subprocess.run(
        [sys.executable, str(actor), "lease"], env=env, capture_output=True, text=True, timeout=20
    )
    assert second.returncode == 0 and "lease_busy" in second.stdout, second.stderr
    third = subprocess.run(
        [sys.executable, str(actor), "resume"],
        env={**env, "ACTOR_CLOCK": str(NOW + 150)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert third.returncode == 0, third.stderr
    assert store.query("SELECT state FROM tasks")[0]["state"] == "unknown"
    assert (
        store.query("SELECT count(*) AS count FROM audit WHERE action='isolated_send_boundary'")[0]["count"]
        == 1
    )
    assert store.query("SELECT * FROM worker_lock") == []
    assert (
        store.query(
            "SELECT count(*) AS count FROM pg_stat_activity WHERE datname=current_database() AND application_name='wechat-manager-isolated-actor'"
        )[0]["count"]
        == 0
    )


async def test_media_link_is_refreshed_and_failure_preserves_chat(tmp_path):
    from wechat_manager.providers import Gateway, ProviderError

    store, manager, account, conv = setup(tmp_path)
    manager.configure_automation(account["id"], ai_enabled=False)
    xml = '<msg><img cdnmidimgurl="platform-locator" /></msg>'
    deliver(store, manager, 12, MsgType=3, Content={"string": xml})
    mid = store.query("SELECT id FROM messages")[0]["id"]
    gateway = Gateway(manager)
    calls = []

    async def download(path, payload, **kwargs):
        calls.append(path)
        return {"ret": 200, "data": {"fileUrl": "https://media.example/fresh-" + str(len(calls))}}

    async def fetch(*args, **kwargs):
        from test_reply_images import png

        return png()

    gateway.call = download
    gateway.fetch_media = fetch
    try:
        await gateway.download_media("device-a", "image", xml, message_id=mid)
        row = store.query("SELECT * FROM media_resources")[0]
        assert row["url_expires_at"] - row["url_obtained_at"] == __import__("pytest").approx(7 * 86400, abs=1)
        assert any(item["value"] == "platform-locator" for item in json.loads(row["urls"]))
        store.query("UPDATE media_resources SET url_expires_at=1 RETURNING message_id")
        await gateway.download_media("device-a", "image", xml, message_id=mid)
        assert len(calls) == 2

        async def missing(*args, **kwargs):
            return {"ret": 200, "data": {}}

        gateway.call = missing
        with pytest.raises(ProviderError, match="missing_media_url"):
            await gateway.download_media("device-a", "image", xml, message_id=mid)
        row = store.query("SELECT * FROM media_resources")[0]
        assert row["state"] == "unavailable" and row["reason"] == "missing_media_url"
        assert store.query("SELECT xml FROM message_contents")[0]["xml"] == xml
        assert len(store.query("SELECT * FROM messages")) == 1
    finally:
        await gateway.close()


def test_repeated_invalid_callback_only_increments_delivery_count(tmp_path):
    from wechat_manager.store import Store

    store = Store(database_url(tmp_path / "postgres"))
    one = store.receive(b"{broken", now=1)
    two = store.receive(b"{broken", now=2)
    assert one == two
    row = store.events()[0]
    assert row["deliveries"] == 2 and row["received_at"] == 1 and row["last_received_at"] == 2
    assert row["parse_error"] == "invalid_json"


def test_chat_content_wrapping_and_binary_changes_do_not_create_versions(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    from test_inbox import event

    data = event(ImgBuf={"buffer": "first"})
    raw = json.dumps(data).encode()
    eid = store.receive(raw)
    data["Data"]["Content"] = data["Data"]["Content"]["string"]
    data["Data"]["ImgBuf"] = {"buffer": "second"}
    assert store.receive(json.dumps(data).encode()) == eid
    assert store.events()[0]["deliveries"] == 2
    assert store.query("SELECT * FROM callback_versions") == []
    assert store.query("SELECT count(*) AS count FROM message_contents") == [{"count": 1}]


def test_identical_relationship_callback_after_processing_is_new_evidence(tmp_path):
    from wechat_manager.store import Store

    store = Store(database_url(tmp_path / "postgres"))
    raw = json.dumps(
        {"Appid": "device-a", "Wxid": "wxid_me", "TypeName": "DelContacts", "Data": {"UserName": "friend"}}
    ).encode()
    first = store.receive(raw, now=1)
    assert store.receive(raw, now=2) == first
    store.query("UPDATE events SET processed=1 WHERE id=%s RETURNING id", (first,))
    second = store.receive(raw, now=3)
    assert second != first
    assert len(store.events()) == 2


def test_semantic_callback_version_shares_unchanged_content(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    from test_inbox import event

    data = event()
    eid = store.receive(json.dumps(data).encode())
    data["Data"]["MsgSource"] = "<msgsource><atuserlist>wxid_me</atuserlist></msgsource>"
    store.receive(json.dumps(data).encode())
    version = store.query("SELECT payload FROM callback_versions")[0]
    payload = json.loads(version["payload"])
    assert "Content" not in payload["Data"]
    assert payload["Data"]["_content_event_id"] == eid
    assert store.query("SELECT count(*) AS count FROM message_contents") == [{"count": 1}]


def test_unknown_chat_type_keeps_original_and_parse_status(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    manager.configure_automation(account["id"], ai_enabled=False)
    body = "<future-message>必须保留未知正文</future-message>"
    deliver(store, manager, 25, MsgType=999999, Content={"string": body})
    row = store.query("SELECT * FROM messages")[0]
    assert json.loads(row["metadata"])["parse_status"] == "unknown_type"
    assert store.query("SELECT xml FROM message_contents")[0]["xml"] == body
