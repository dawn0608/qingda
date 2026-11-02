import pytest
from qingda import automation_defaults, prompt_drafts
from qingda.reply_prompt import load_default
from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import user_scope
from test_database import database_url
from test_prompt_drafts import actor, parallel


def legacy(tmp_path):
    store = Store(database_url(tmp_path / "prompt-defaults"))
    manager = Manager(store, clock=lambda: 1000)
    aid = manager.add_account("untouched-terminal")["id"]
    with store.transaction() as conn:
        conn.execute("UPDATE accounts SET wxid='untouched-wxid',nickname='新助手' WHERE id=%s", (aid,))
    return store, manager, aid


@pytest.mark.parametrize("configured", [False, True])
def test_verified_account_gets_default_without_auto_enable(tmp_path, configured):
    store = Store(database_url(tmp_path / "new-account"))
    manager = Manager(store)
    aid = manager.add_account("untouched-terminal")["id"]
    if configured:
        store.set_setting("automation_defaults_active", False)
    account = manager.verify_identity("untouched-terminal", {"wxid": "untouched-wxid", "nickName": "新助手"})
    assert account["prompt"] == load_default("新助手")
    assert not account["ai_enabled"] and account["epoch"] == account["ai_since"] == 0
    assert not store.query("SELECT * FROM account_automation_state")
    assert not store.query("SELECT * FROM conversations")
    again = manager.verify_identity("untouched-terminal", {"wxid": "untouched-wxid", "nickName": "改名"})
    assert again["prompt"] == account["prompt"]
    assert len(store.query("SELECT id FROM audit WHERE action='assistant_prompt_initialized'")) == 1
    assert prompt_drafts.read(manager, aid)["markdown"] == account["prompt"]


@pytest.mark.parametrize("preserved", ["custom", "cleared", "draft", "empty_draft", "archived", "pending"])
def test_initialization_preserves_existing_choices(tmp_path, preserved):
    store, manager, aid = legacy(tmp_path)
    if preserved == "custom":
        manager.configure_automation(aid, prompt="用户自定义设定")
    elif preserved == "cleared":
        manager.configure_automation(aid, prompt="曾经编辑")
        manager.configure_automation(aid, prompt="")
    elif preserved in {"draft", "empty_draft"}:
        draft = prompt_drafts.read(manager, aid)
        prompt_drafts.save(manager, aid, "尚未生效", None, draft["base_sha256"])
        if preserved == "empty_draft":
            draft = prompt_drafts.read(manager, aid)
            prompt_drafts.save(manager, aid, "", draft["version"], draft["base_sha256"])
    else:
        with store.transaction() as conn:
            if preserved == "archived":
                conn.execute("UPDATE accounts SET archived_at=1 WHERE id=%s", (aid,))
            else:
                conn.execute("UPDATE accounts SET wxid=NULL WHERE id=%s", (aid,))
    before = manager.account(aid)
    draft = prompt_drafts.read(manager, aid)
    with store.transaction() as conn:
        assert not automation_defaults.initialize_prompt(manager, conn, aid)
    assert manager.account(aid) == before
    assert prompt_drafts.read(manager, aid) == draft


def test_initialization_is_scoped_and_rolls_back_with_transaction(tmp_path):
    store, manager, aid = legacy(tmp_path)
    with user_scope(2), store.transaction() as conn, pytest.raises(ValueError, match="账号不存在"):
        automation_defaults.initialize_prompt(manager, conn, aid)
    with pytest.raises(RuntimeError, match="rollback"), store.transaction() as conn:
        assert automation_defaults.initialize_prompt(manager, conn, aid)
        raise RuntimeError("rollback")
    assert manager.account(aid)["prompt"] == ""
    assert not store.query("SELECT id FROM audit WHERE action='assistant_prompt_initialized'")


def test_two_processes_initialize_once_and_recover_after_crash(tmp_path):
    store, manager, aid = legacy(tmp_path)
    process = actor(store, "crash-initialize", {"aid": aid})
    try:
        assert process.stdout.readline().strip() == "initialized"
        process.communicate("crash\n", timeout=15)
        assert process.returncode == 23
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert manager.account(aid)["prompt"] == ""
    results = parallel(actor(store, "initialize", {"aid": aid}), actor(store, "initialize", {"aid": aid}))
    assert sorted(row["changed"] for row in results) == [False, True]
    assert manager.account(aid)["prompt"] == load_default("新助手")
    assert len(store.query("SELECT id FROM audit WHERE action='assistant_prompt_initialized'")) == 1
    assert store.pool.get_stats()["pool_available"] == store.pool.get_stats()["pool_size"]


def test_two_processes_initialization_never_overwrites_a_saved_draft(tmp_path):
    store, manager, aid = legacy(tmp_path)
    results = parallel(
        actor(store, "initialize", {"aid": aid}),
        actor(
            store,
            "save",
            {
                "aid": aid,
                "markdown": "用户草稿",
                "version": None,
                "base_sha256": prompt_drafts.fingerprint(""),
            },
        ),
    )
    draft = prompt_drafts.read(manager, aid)
    if results[1]["status"] == "saved":
        assert not results[0]["changed"]
        assert draft["markdown"] == "用户草稿" and draft["active_prompt"] == ""
    else:
        assert results[1]["status"] == "conflict" and results[0]["changed"]
        assert draft["markdown"] == draft["active_prompt"] == load_default("新助手")
