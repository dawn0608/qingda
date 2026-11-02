"""账号设定草稿：复用逐用户 settings，短事务 CAS；草稿不改变运行提示词。"""

import hashlib
import uuid


class DraftConflict(ValueError):
    pass


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def key(aid, owner_wxid=None):
    if owner_wxid is not None:
        from .owner_profile import key as profile_key

        return profile_key(aid, owner_wxid) + ":draft"
    return f"assistant_prompt_draft:{aid}"


def read(manager, aid, conn=None, *, owner_wxid=None):
    if conn is None:
        with manager.store.connect() as connection:
            return read(manager, aid, connection, owner_wxid=owner_wxid)
    account = manager.account(aid, conn)
    if owner_wxid is not None:
        from .owner_profile import check_identity

        check_identity(manager, account, owner_wxid, conn)
    active = account["owner_profile" if owner_wxid is not None else "prompt"] or ""
    stored = manager.store.setting(key(aid, owner_wxid), None, conn)
    return {
        "version": stored["version"] if stored else None,
        "markdown": stored["markdown"] if stored else active,
        "base_sha256": stored["base_sha256"] if stored else fingerprint(active),
        "active_prompt": active,
        "active_sha256": fingerprint(active),
        "dirty": bool(stored and stored["markdown"] != active),
        "conflict": bool(stored and stored["base_sha256"] != fingerprint(active)),
        "updated_at": stored.get("updated_at") if stored else None,
    }


def save(manager, aid, markdown, version, base_sha256, *, owner_wxid=None):
    if len(markdown) > 50000:
        raise ValueError("设定不能超过 50000 个字符，请精简后继续")
    # 所有该账号草稿写入先锁账号，避免不存在的 settings 行无法锁定。
    # 不领取后台任务、不等待网络、不使用进程内锁证明多进程互斥。
    with manager.store.connect() as conn:
        if not conn.execute("SELECT id FROM accounts WHERE id=%s FOR UPDATE", (aid,)).fetchone():
            raise ValueError("账号不存在")
        current = read(manager, aid, conn, owner_wxid=owner_wxid)
        if base_sha256 != current["active_sha256"] or current["conflict"]:
            raise DraftConflict("生效设定已在另一处修改，当前草稿已保留，请核对后继续")
        # 结果不确定的同内容重试只读回回执，不增加版本或触碰任务。
        if current["markdown"] == markdown:
            return current
        if version != current["version"]:
            raise DraftConflict("草稿已在另一处修改，当前内容已保留，请核对后继续")
        value = {
            "markdown": markdown,
            "version": uuid.uuid4().hex,
            "base_sha256": base_sha256,
            "updated_at": manager.clock(),
        }
        manager.store.set_setting(key(aid, owner_wxid), value, conn)
        return read(manager, aid, conn, owner_wxid=owner_wxid)


def apply(manager, aid, version, base_sha256, *, owner_wxid=None):
    # 生效沿用现有任务取消的锁顺序：项目事务锁 -> 账号 -> 草稿。
    with manager.store.transaction() as conn:
        if not conn.execute("SELECT id FROM accounts WHERE id=%s FOR UPDATE", (aid,)).fetchone():
            raise ValueError("账号不存在")
        current = read(manager, aid, conn, owner_wxid=owner_wxid)
        stored = manager.store.setting(key(aid, owner_wxid), None, conn)
        if not stored or version != current["version"]:
            raise DraftConflict("草稿已在另一处修改，当前内容已保留，请核对后继续")
        if stored.get("applied_version") == version and not current["dirty"]:
            return {"draft": current, "account": manager.account(aid, conn), "changed": False}
        if base_sha256 != current["active_sha256"] or current["conflict"]:
            raise DraftConflict("生效设定已在另一处修改，当前草稿已保留，请核对后继续")
        changed = current["dirty"]
        if owner_wxid is None:
            account = manager.configure_automation(aid, prompt=current["markdown"], conn=conn)
        else:
            from .owner_profile import apply as apply_profile

            account = manager.account(aid, conn)
            if changed:
                account = apply_profile(manager, account, current["markdown"], version, conn)
        stored.update(base_sha256=fingerprint(current["markdown"]), applied_version=version)
        manager.store.set_setting(key(aid, owner_wxid), stored, conn)
        return {
            "draft": read(manager, aid, conn, owner_wxid=owner_wxid),
            "account": account,
            "changed": changed,
        }


def resolve(manager, aid, markdown, version, active_sha256, *, owner_wxid=None):
    """仅用户明确选择保留本页内容后，核对最新版本重新建立草稿基线。"""
    with manager.store.connect() as conn:
        if not conn.execute("SELECT id FROM accounts WHERE id=%s FOR UPDATE", (aid,)).fetchone():
            raise ValueError("账号不存在")
        current = read(manager, aid, conn, owner_wxid=owner_wxid)
        if version != current["version"] or active_sha256 != current["active_sha256"]:
            raise DraftConflict("设定再次发生变化，请重新核对")
        value = {
            "markdown": markdown,
            "version": uuid.uuid4().hex,
            "base_sha256": active_sha256,
            "updated_at": manager.clock(),
        }
        manager.store.set_setting(key(aid, owner_wxid), value, conn)
        return read(manager, aid, conn, owner_wxid=owner_wxid)
