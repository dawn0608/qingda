"""号主授权公开资料；与助手行为设定独立，绑定已核验的微信身份。"""

import hashlib


def key(aid, wxid):
    identity = hashlib.sha256(wxid.encode()).hexdigest()
    return f"owner_profile:{aid}:{identity}"


def active(manager, account, conn):
    if not account["wxid"]:
        return {}
    return manager.store.setting(key(account["id"], account["wxid"]), {}, conn)


def decorate(manager, account, conn):
    value = active(manager, account, conn)
    account["owner_profile"] = value.get("markdown", "")
    account["owner_profile_version"] = value.get("version")


def check_identity(manager, account, wxid, conn):
    from .prompt_drafts import DraftConflict

    terminal = conn.execute(
        "SELECT verified FROM terminals WHERE account_id=%s AND app_id=%s",
        (account["id"], account["current_app_id"]),
    ).fetchone()
    if (
        not wxid
        or account["wxid"] != wxid
        or account["archived_at"] is not None
        or not terminal
        or not terminal[0]
    ):
        raise DraftConflict("微信身份已变化或尚未核验，当前内容已保留，请重新选择账号")


def apply(manager, account, markdown, version, conn):
    manager.store.set_setting(
        key(account["id"], account["wxid"]),
        {"markdown": markdown, "version": version, "updated_at": manager.clock()},
        conn,
    )
    conn.execute(
        "UPDATE accounts SET epoch=epoch+1,ai_since=%s WHERE id=%s",
        (manager.clock(), account["id"]),
    )
    manager.cancel_auto(conn, "owner_profile_changed", account_id=account["id"])
    manager.store.audit(
        "owner_profile_changed",
        account["id"],
        {"version": version, "sha256": hashlib.sha256(markdown.encode()).hexdigest()},
        conn,
    )
    return manager.account(account["id"], conn)
