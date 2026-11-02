"""账号及已核实关系的默认自动回复；不创建主动消息。"""

from .contacts import SYSTEM_CONTACTS, publish_directory, snapshot
from .reply_prompt import load_default


def initialize_prompt(manager, conn, aid):
    """只补齐已核实且从未编辑的空设定，不启用助手或改变回复权限。"""
    account = conn.execute("SELECT * FROM accounts WHERE id=%s FOR UPDATE", (aid,)).fetchone()
    if not account:
        raise ValueError("账号不存在")
    if (
        not account["wxid"]
        or account["archived_at"] is not None
        or account["prompt"]
        or account["epoch"] != 0
        or account["ai_enabled"]
        or account["ai_since"] != 0
    ):
        return False
    from .prompt_drafts import key

    if manager.store.setting(key(aid), None, conn) is not None:
        return False
    prompt = load_default(account["nickname"].strip() or "WeMind")
    conn.execute("UPDATE accounts SET prompt=%s WHERE id=%s", (prompt, aid))
    manager.store.audit("assistant_prompt_initialized", aid, conn=conn)
    return True


def eligible(manager, conn, account, peer):
    if not peer or peer in SYSTEM_CONTACTS or peer == account["wxid"] or peer.startswith("gh_"):
        return False
    return not conn.execute(
        "SELECT 1 FROM contact_removals WHERE account_id=%s AND peer_id=%s AND restored_at IS NULL",
        (account["id"], peer),
    ).fetchone()


def initialize(manager, conn, aid):
    if not manager.store.setting("automation_defaults_active", False, conn):
        return False
    account = manager.account(aid, conn)
    current = snapshot(conn, aid, require_online=False)
    if not current or account["archived_at"] is not None:
        return False
    old = conn.execute("SELECT * FROM account_automation_state WHERE account_id=%s", (aid,)).fetchone()
    if old:
        if old["wxid"] != account["wxid"]:
            return False
        if not old["ai_ready"] and not old["manual_ai"] and manager.store.setting("model_key", None, conn):
            conn.execute(
                "UPDATE accounts SET ai_enabled=1,ai_since=%s,epoch=epoch+1 WHERE id=%s",
                (manager.clock(), aid),
            )
            conn.execute("UPDATE account_automation_state SET ai_ready=1 WHERE account_id=%s", (aid,))
        return False
    now = manager.clock()
    name = account["nickname"].strip() or "WeMind"
    prompt = account["prompt"] or load_default(name)
    ready = bool(manager.store.setting("model_key", None, conn)) or manager.vault is None
    conn.execute(
        "INSERT INTO account_automation_state(account_id,wxid,initialized_at,ai_ready) VALUES(%s,%s,%s,%s)",
        (aid, account["wxid"], now, ready),
    )
    conn.execute(
        "UPDATE accounts SET prompt=%s,ai_enabled=%s,ai_since=%s,epoch=epoch+1 WHERE id=%s",
        (prompt, ready, now, aid),
    )
    manager.cancel_auto(conn, "automation_defaults_initialized", account_id=aid)
    seed_directory(manager, conn, aid)
    for row in conn.execute("SELECT peer_id FROM conversations WHERE account_id=%s", (aid,)).fetchall():
        if eligible(manager, conn, account, row[0]):
            apply_default(manager, conn, aid, row[0])
    manager.store.audit("automation_defaults_initialized", aid, {"ai_ready": ready}, conn)
    return True


def apply_default(manager, conn, aid, peer):
    account = manager.account(aid, conn)
    if not conn.execute(
        "SELECT 1 FROM account_automation_state WHERE account_id=%s AND wxid=%s", (aid, account["wxid"])
    ).fetchone() or not eligible(manager, conn, account, peer):
        return
    conv = manager.ensure_conversation(aid, peer, conn)
    pref = conn.execute(
        "SELECT enabled FROM conversation_preferences WHERE conversation_id=%s", (conv["id"],)
    ).fetchone()
    desired = pref[0] if pref else 1
    if conv["allowed"] != desired:
        conn.execute(
            "UPDATE conversations SET allowed=%s,ai_since=%s,epoch=epoch+1 WHERE id=%s",
            (desired, manager.clock(), conv["id"]),
        )


def seed_directory(manager, conn, aid):
    account = manager.account(aid, conn)
    base = conn.execute("SELECT * FROM account_automation_state WHERE account_id=%s", (aid,)).fetchone()
    if not base:
        return
    stats = conn.execute(
        "SELECT * FROM account_contacts WHERE account_id=%s AND wxid=%s", (aid, account["wxid"])
    ).fetchone()
    if not stats or stats["list_updated_at"] is None:
        return
    publish_directory(conn, aid, manager.clock())
    for row in conn.execute(
        "SELECT peer_id FROM contact_entries WHERE account_id=%s AND owner_wxid=%s", (aid, account["wxid"])
    ).fetchall():
        apply_default(manager, conn, aid, row[0])
    conn.execute("UPDATE account_automation_state SET friends_ready=1 WHERE account_id=%s", (aid,))


def remember_choice(conn, cid, enabled):
    conn.execute(
        "INSERT INTO conversation_preferences VALUES(%s,%s) ON CONFLICT(user_id,conversation_id) DO UPDATE SET enabled=excluded.enabled",
        (cid, bool(enabled)),
    )


def confirmed(manager, conn, aid, peer):
    account = manager.account(aid, conn)
    if not conn.execute(
        "SELECT 1 FROM account_automation_state WHERE account_id=%s AND wxid=%s", (aid, account["wxid"])
    ).fetchone() or not eligible(manager, conn, account, peer):
        return False
    apply_default(manager, conn, aid, peer)
    return True


def maintain(manager, conn):
    """补全等待模型配置的默认状态；不访问平台、不生成或发送消息。"""
    for (aid,) in conn.execute(
        "SELECT id FROM accounts WHERE archived_at IS NULL AND enabled=1 AND wxid IS NOT NULL"
    ).fetchall():
        initialize(manager, conn, aid)
        base = conn.execute(
            "SELECT friends_ready FROM account_automation_state WHERE account_id=%s", (aid,)
        ).fetchone()
        if base and not base[0]:
            seed_directory(manager, conn, aid)
