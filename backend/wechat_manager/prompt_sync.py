"""运维用账号设定同步：快照校验、逐用户原子应用、干净草稿同步。"""

import hashlib
import json
import uuid

from . import prompt_drafts
from .reply_prompt import load_default
from .tenancy import current_user


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def preview(manager, conn=None):
    if conn is None:
        with manager.store.connect(snapshot=True) as conn:
            return preview(manager, conn)
    rows = conn.execute(
        "SELECT id,nickname,wxid,current_app_id,prompt,epoch,enabled,ai_enabled "
        "FROM accounts WHERE archived_at IS NULL ORDER BY id"
    ).fetchall()
    accounts = []
    for row in rows:
        accounts.append(
            {
                **dict(row),
                "draft": manager.store.setting(prompt_drafts.key(row["id"]), None, conn),
                "candidate": load_default(row["nickname"].strip() or "WeMind"),
            }
        )
    return {"version": 1, "user_id": current_user.get(), "accounts": accounts}


def apply(manager, snapshot, expected_sha256):
    if (
        not expected_sha256
        or digest(snapshot) != expected_sha256
        or snapshot.get("user_id") != current_user.get()
    ):
        raise ValueError("设定快照哈希或用户范围不符")
    with manager.store.transaction() as conn:
        receipt_key = "assistant_prompt_sync:" + expected_sha256
        previous = manager.store.setting(receipt_key, None, conn)
        if previous:
            return {**previous, "repeated": True}
        # 项目写事务锁与编辑器锁顺序一致，不跨网络等待。
        conn.execute("SELECT id FROM accounts WHERE archived_at IS NULL ORDER BY id FOR UPDATE")
        if preview(manager, conn) != snapshot:
            raise ValueError("账号或草稿已变化，请重新预览；没有应用任何设定")
        changed, conflicts = [], []
        for account in snapshot["accounts"]:
            aid, candidate, old = account["id"], account["candidate"], account["prompt"]
            if old == candidate:
                continue
            manager.configure_automation(aid, prompt=candidate, conn=conn)
            draft = account["draft"]
            if draft and draft["markdown"] != old:
                # 原文与旧基线保留，现有编辑器会明确呈现冲突。
                conflicts.append(aid)
            else:
                version = uuid.uuid4().hex
                manager.store.set_setting(
                    prompt_drafts.key(aid),
                    {
                        "markdown": candidate,
                        "version": version,
                        "applied_version": version,
                        "base_sha256": prompt_drafts.fingerprint(candidate),
                        "updated_at": manager.clock(),
                    },
                    conn,
                )
            changed.append(aid)
        receipt = {
            "snapshot_sha256": expected_sha256,
            "changed_accounts": changed,
            "preserved_drafts": conflicts,
            "applied_at": manager.clock(),
        }
        manager.store.set_setting(receipt_key, receipt, conn)
        manager.store.audit("assistant_prompts_synchronized", detail=receipt, conn=conn)
        return receipt
