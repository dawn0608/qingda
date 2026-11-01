import pytest
from test_automation import setup
from wechat_manager import prompt_drafts, prompt_sync


@pytest.mark.parametrize("dirty", [False, True])
def test_sync_keeps_switches_and_real_draft_with_idempotent_receipt(tmp_path, dirty):
    store, manager, account, conv = setup(tmp_path)
    aid = account["id"]
    draft = prompt_drafts.read(manager, aid)
    if dirty:
        prompt_drafts.save(manager, aid, "尚未提交的自定义内容", draft["version"], draft["base_sha256"])
    with store.transaction() as conn:
        manager.takeover(conv["id"], conn)
    before = manager.conversation(conv["id"])
    snapshot = prompt_sync.preview(manager)
    result = prompt_sync.apply(manager, snapshot, prompt_sync.digest(snapshot))
    updated = manager.account(aid)
    assert result["changed_accounts"] == [aid] and updated["ai_enabled"]
    assert manager.conversation(conv["id"])["pause_until"] == before["pause_until"]
    saved = prompt_drafts.read(manager, aid)
    assert saved["conflict"] is dirty and saved["dirty"] is dirty
    if dirty:
        assert saved["markdown"] == "尚未提交的自定义内容"
    else:
        assert saved["markdown"] == updated["prompt"]
    repeat = prompt_sync.apply(manager, snapshot, prompt_sync.digest(snapshot))
    assert repeat["repeated"] and manager.account(aid)["epoch"] == updated["epoch"]


def test_sync_snapshot_conflict_is_atomic(tmp_path):
    store, manager, account, _ = setup(tmp_path)
    snapshot = prompt_sync.preview(manager)
    manager.configure_automation(account["id"], prompt="预览后的修改")
    with pytest.raises(ValueError, match="重新预览"):
        prompt_sync.apply(manager, snapshot, prompt_sync.digest(snapshot))
    assert manager.account(account["id"])["prompt"] == "预览后的修改"
