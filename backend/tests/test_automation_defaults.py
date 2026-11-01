"""停用主动问候后，默认自动回复及人工选择仍持久有效。"""

from automation_fixtures import active, verified
from wechat_manager import automation_defaults
from wechat_manager.contact_changes import mark_removed, record_evidence
from wechat_manager.store import Store


async def test_default_reply_preserves_manual_off_prompt_and_pause_across_sync_and_restart(tmp_path):
    store, manager, _, aid, clock, worker, _ = await active(tmp_path)
    conv = manager.ensure_conversation(aid, "old")
    assert conv["allowed"] and manager.account(aid)["ai_enabled"]
    manager.configure_automation(aid, prompt="保留原有提示词")
    manager.set_auto_reply(conv["id"], False)
    with store.transaction() as conn:
        manager.takeover(conv["id"], conn)
        paused = manager.conversation(conv["id"], conn)["pause_until"]
        automation_defaults.seed_directory(manager, conn, aid)
        automation_defaults.maintain(manager, conn)
    reopened = type(manager)(Store(store.url), clock=lambda: clock[0])
    assert reopened.account(aid)["prompt"] == "保留原有提示词"
    assert not reopened.conversation(conv["id"])["allowed"]
    assert reopened.conversation(conv["id"])["pause_until"] == paused
    await worker.once()
    assert not store.query("SELECT * FROM tasks WHERE kind='greeting'")


async def test_relationship_restoration_keeps_saved_off_and_does_not_greet(tmp_path):
    store, manager, _, aid, clock, worker, _ = await active(tmp_path)
    verified(store, manager, aid, "old", clock)
    conv = manager.ensure_conversation(aid, "old")
    manager.set_auto_reply(conv["id"], False)
    with store.transaction() as conn:
        account = manager.account(aid, conn)
        proof = record_evidence(conn, account, "old", "isolated_removal", clock[0], clock[0], {})
        mark_removed(conn, account, "old", clock[0], "friend_removed", evidence_id=proof)
        conn.execute(
            "UPDATE contact_removals SET restored_at=%s,version=version+1 WHERE account_id=%s AND peer_id='old'",
            (clock[0], aid),
        )
    verified(store, manager, aid, "old", clock, restored=True)
    await worker.once()
    assert not manager.conversation(conv["id"])["allowed"]
    assert not store.query("SELECT * FROM tasks WHERE kind='greeting'")


async def test_new_account_and_new_verified_contact_get_reply_defaults_without_greetings(tmp_path):
    store, manager, _, aid, clock, worker, _ = await active(tmp_path)
    fresh = manager.add_account("fresh-terminal")
    manager.verify_identity("fresh-terminal", {"wxid": "fresh-me", "nickName": "新助手"})
    account = manager.account(fresh["id"])
    assert account["ai_enabled"] and "新助手" in account["prompt"]
    with store.transaction() as conn:
        assert automation_defaults.confirmed(manager, conn, aid, "new-contact")
    assert manager.ensure_conversation(aid, "new-contact")["allowed"]
    manager.configure_automation(aid, ai_enabled=False)
    with store.transaction() as conn:
        automation_defaults.maintain(manager, conn)
    assert not manager.account(aid)["ai_enabled"]
