"""成员级连续对话授权与拍一拍输入关联；所有写入与回调入库共用事务。"""

import json

from .callbacks import revoked

IDLE = 120
ROUND = 600
PAT_WAIT = 3
KINDS = ("text", "image", "voice", "emoji")


def other_recipient(metadata):
    return metadata.get("directed_elsewhere") or (
        bool(metadata.get("mentions")) and not metadata.get("mentioned")
    )


def active(conn, account, conv, sender, now):
    row = conn.execute(
        "SELECT * FROM dialogue_windows WHERE account_id=%s AND conversation_id=%s AND sender=%s",
        (account["id"], conv["id"], sender),
    ).fetchone()
    if not row or row["closed_reason"]:
        return None
    reason = ""
    if row["account_epoch"] != account["epoch"] or row["conversation_epoch"] != conv["epoch"]:
        reason = "rules_changed"
    elif revoked(conn, row["source_mid"]):
        reason = "wake_revoked"
    elif now >= min(row["expires_at"], row["hard_deadline"]):
        reason = "window_expired"
    if reason:
        conn.execute(
            "UPDATE dialogue_windows SET closed_reason=%s WHERE account_id=%s AND conversation_id=%s AND sender=%s",
            (reason, account["id"], conv["id"], sender),
        )
        return None
    return row


def annotate(conn, account, conv, msg, now):
    meta = msg["metadata"]
    if (
        conv["kind"] != "group"
        or not meta.get("dialogue_version")
        or msg["outgoing"]
        or meta.get("wakeup_reason")
        or meta.get("message_kind") not in KINDS
        or other_recipient(meta)
    ):
        return
    window = active(conn, account, conv, msg["sender"], now)
    source = (
        conn.execute("SELECT created_at FROM messages WHERE id=%s", (window["source_mid"],)).fetchone()
        if window
        else None
    )
    if window and source and (msg["created_at"] or 0) >= source[0]:
        meta.update(wakeup_reason="continuation", window_source_id=window["source_mid"])


def register(manager, conn, account, conv, msg, task, now):
    """仅通过全部准入保护的新任务可以建立授权或取得其他消息的处理权。"""
    meta = msg["metadata"]
    mid = task["message_id"]
    if task["state"] != "queued" or not meta.get("dialogue_version"):
        return
    reason = meta.get("wakeup_reason")
    if conv["kind"] == "group" and reason and reason != "wait_continuation":
        old = active(conn, account, conv, msg["sender"], now)
        start = old["started_at"] if old else now
        deadline = old["hard_deadline"] if old else now + ROUND
        source = mid if reason != "continuation" else meta["window_source_id"]
        # 续聊只是候选输入，只有实际回复成功才能续期；明确唤醒仍可开窗。
        expires = old["expires_at"] if reason == "continuation" and old else min(now + IDLE, deadline)
        activity = old["last_activity"] if reason == "continuation" and old else now
        conn.execute(
            "INSERT INTO dialogue_windows VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'') ON CONFLICT (user_id,account_id,conversation_id,sender) DO UPDATE SET started_at=excluded.started_at,expires_at=excluded.expires_at,hard_deadline=excluded.hard_deadline,last_activity=excluded.last_activity,source_mid=excluded.source_mid,source=excluded.source,account_epoch=excluded.account_epoch,conversation_epoch=excluded.conversation_epoch,closed_reason=excluded.closed_reason",
            (
                account["id"],
                conv["id"],
                msg["sender"],
                start,
                expires,
                deadline,
                activity,
                source,
                old["source"] if reason == "continuation" else reason,
                account["epoch"],
                conv["epoch"],
            ),
        )
        meta["window_source_id"] = source
        conn.execute("UPDATE message_records SET metadata=%s WHERE id=%s", (json.dumps(meta), mid))
    if reason != "pat":
        return
    parent = conn.execute(
        "SELECT t.* FROM tasks t JOIN messages m ON m.id=t.message_id WHERE t.account_id=%s AND t.conversation_id=%s AND t.kind='auto' AND t.id!=%s AND t.state IN ('queued','generating','preparing','ready','sending') AND m.sender=%s AND t.account_epoch=%s AND t.conversation_epoch=%s AND t.config_version=%s AND (((m.metadata #>> '{dialogue_version}'))::bigint)=1 AND m.created_at>=%s ORDER BY t.created_at,t.ordinal LIMIT 1",
        (
            account["id"],
            conv["id"],
            task["id"],
            msg["sender"],
            account["epoch"],
            conv["epoch"],
            task["config_version"],
            now - 300,
        ),
    ).fetchone()
    owner = dict(parent) if parent else task
    frozen = conn.execute("SELECT 1 FROM reply_batches WHERE task_id=%s", (owner["id"],)).fetchone()
    from .private_replies import round_table

    table = round_table(conn, owner)
    revisable = (
        owner["state"] in ("queued", "generating", "preparing", "ready")
        and conn.execute(f"SELECT 1 FROM {table} WHERE task_id=%s", (owner["id"],)).fetchone()
    )
    duplicate = bool(
        parent and any(json.loads(m["metadata"]).get("message_kind") == "pat" for m in inputs(conn, [owner]))
    )
    if revisable:
        frozen = False
        if not duplicate:
            conn.execute(f"UPDATE {table} SET revision=revision+1 WHERE task_id=%s", (owner["id"],))
    if parent:
        conn.execute(
            "INSERT INTO reply_input_links VALUES(%s,%s,%s,%s,%s,%s)",
            (
                mid,
                owner["id"],
                "pat",
                int(not duplicate and not frozen and parent["state"] != "sending"),
                now,
                mid,
            ),
        )
        conn.execute("UPDATE tasks SET state='merged',reason='pat_joined' WHERE id=%s", (task["id"],))
    if owner["state"] == "queued" and not frozen and not duplicate:
        conn.execute(
            "INSERT INTO reply_not_before VALUES(%s,%s) ON CONFLICT DO NOTHING", (owner["id"], now + PAT_WAIT)
        )
        # 保留未唤醒任务的审计状态，只让本次拍一拍取得输入关联。
        candidates = conn.execute(
            "SELECT m.* FROM messages m JOIN tasks t ON t.message_id=m.id AND t.kind='auto' WHERE m.account_id=%s AND m.conversation_id=%s AND m.sender=%s AND m.id<%s AND m.created_at>=%s AND m.created_at<=%s AND m.received_at>=%s AND t.state='skipped' AND t.reason='not_explicitly_mentioned' AND NOT EXISTS(SELECT 1 FROM reply_input_links l WHERE l.message_id=m.id) ORDER BY m.created_at DESC,m.id DESC",
            (account["id"], conv["id"], msg["sender"], mid, now - IDLE, now, now - IDLE),
        ).fetchall()
        count = conn.execute(
            "SELECT count(*) FROM reply_input_links WHERE task_id=%s AND kind='backfill'", (owner["id"],)
        ).fetchone()[0]
        for candidate in candidates:
            if count >= 5:
                break
            cm = json.loads(candidate["metadata"])
            if (
                not cm.get("dialogue_version")
                or cm.get("message_kind") not in KINDS
                or other_recipient(cm)
                or revoked(conn, candidate["id"])
            ):
                continue
            cm.update(wakeup_reason="pat_backfill", window_source_id=mid)
            if manager.guard(account, conv, cm, candidate["created_at"], now, conn):
                continue
            conn.execute(
                "INSERT INTO reply_input_links VALUES(%s,%s,%s,%s,%s,%s)",
                (candidate["id"], owner["id"], "backfill", 1, now, mid),
            )
            count += 1
            if count == 5:
                break


def inputs(conn, batch, *, include_excluded=False):
    from .emoji_policy import exclusion

    result = {}
    for task in batch:
        rows = [(task["message_id"], "primary", None)]
        rows += [
            tuple(r)
            for r in conn.execute(
                "SELECT message_id,kind,source_mid FROM reply_input_links WHERE task_id=%s AND in_context=1",
                (task["id"],),
            )
        ]
        for mid, kind, source in rows:
            if kind != "primary" and (revoked(conn, mid) or revoked(conn, source)):
                continue
            msg = conn.execute("SELECT * FROM messages WHERE id=%s", (mid,)).fetchone()
            if msg:
                if not include_excluded and exclusion(msg):
                    continue
                item = dict(msg)
                item["_input_kind"] = kind
                if kind == "backfill":
                    meta = json.loads(item["metadata"])
                    meta["wakeup_reason"] = "pat_backfill"
                    item["metadata"] = json.dumps(meta)
                result[mid] = item
    return sorted(result.values(), key=lambda m: (m["created_at"], m["id"]))


def wake_revoked(conn, mid):
    row = conn.execute(
        "SELECT (((metadata #>> '{window_source_id}'))::bigint) FROM messages WHERE id=%s", (mid,)
    ).fetchone()
    return bool(row and row[0] and revoked(conn, row[0]))


def recover_pat(conn, task, now):
    """主问题被撤回后保留仍有效的拍一拍；复用已有自动任务，不突破唯一约束。"""
    pat = conn.execute(
        "SELECT t.* FROM reply_input_links l JOIN tasks t ON t.message_id=l.message_id WHERE l.task_id=%s AND l.kind='pat' AND l.in_context=1 AND t.state='merged' AND NOT EXISTS(SELECT 1 FROM message_views v WHERE v.key=('m:' || lpad((l.message_id)::text,20,'0')) AND v.revoked=1) ORDER BY l.created_at,t.ordinal LIMIT 1",
        (task["id"],),
    ).fetchone()
    if not pat or wake_revoked(conn, pat["message_id"]):
        return
    conn.execute("DELETE FROM reply_input_links WHERE message_id=%s", (pat["message_id"],))
    conn.execute("UPDATE reply_input_links SET task_id=%s WHERE task_id=%s", (pat["id"], task["id"]))
    conn.execute(
        "UPDATE tasks SET state='queued',reason='batch_rebuilt',lease_owner=NULL WHERE id=%s", (pat["id"],)
    )
    conn.execute(
        "INSERT INTO reply_not_before SELECT %s,ready_at FROM reply_not_before WHERE task_id=%s ON CONFLICT DO NOTHING",
        (pat["id"], task["id"]),
    )


def absorb_answered_pat(conn, task, now):
    """所有会话共用持久应声去重；等满收集期，先给新问题合并机会。"""
    from .reply_queue import members

    if task["kind"] != "auto":
        return False
    current = inputs(conn, members(conn, task))
    if not current or any(json.loads(m["metadata"]).get("message_kind") != "pat" for m in current):
        return False
    candidates = conn.execute(
        "SELECT t.* FROM tasks t JOIN messages m ON m.id=t.message_id WHERE t.account_id=%s AND t.conversation_id=%s AND m.sender=%s AND t.kind='auto' AND t.state='accepted' AND t.completed_at>=%s AND t.account_epoch=%s AND t.conversation_epoch=%s AND NOT EXISTS(SELECT 1 FROM message_views v WHERE v.key='t:'||t.id AND v.revoked=1) ORDER BY t.completed_at DESC",
        (
            task["account_id"],
            task["conversation_id"],
            current[0]["sender"],
            now - IDLE,
            task["account_epoch"],
            task["conversation_epoch"],
        ),
    ).fetchall()
    for parent in candidates:
        from .reply_decisions import record

        record(
            conn,
            task,
            {
                "action": "silent",
                "content": "",
                "reason": "off_topic",
                "close_dialogue": False,
                "handled_message_ids": [],
            },
            now,
        )
        conn.execute(
            "UPDATE tasks SET state='skipped',reason='pat_already_answered',completed_at=%s,updated_at=%s WHERE id=%s",
            (now, now, task["id"]),
        )
        return True
    return False


def waiting(conn, task, now):
    """等待中的拍一拍不占用模型槽；同成员新问题不能越过它先执行。"""
    return bool(
        conn.execute(
            "SELECT 1 FROM reply_not_before d JOIN tasks t ON t.id=d.task_id JOIN messages m ON m.id=t.message_id JOIN messages current ON current.id=%s WHERE t.state='queued' AND d.ready_at>%s AND t.account_id=%s AND t.conversation_id=%s AND m.sender=current.sender AND t.created_at<=%s LIMIT 1",
            (task["message_id"], now, task["account_id"], task["conversation_id"], task["created_at"]),
        ).fetchone()
    )
