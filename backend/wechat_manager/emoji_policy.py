"""表情素材的 AI 准入和批次剔除；原始消息及媒体定位信息始终保留。"""

import json

VERSION = "native-emoji-static-image-flash-v4"
ANIMATED = "animated_emoji_disabled"
UNVERIFIED = "emoji_animation_unverified"
CUSTOM = "custom_emoji_disabled"


def is_emoji(message):
    return str(message["msg_type"]) == "47"


def is_image(message):
    return str(message["msg_type"]) in ("3", "47")


def exclusion(message):
    if str(message["msg_type"]) in ("43", "62"):
        return "video_disabled"
    if is_emoji(message):
        return CUSTOM
    if not is_image(message):
        return ""
    meta = json.loads(message["metadata"])
    count = meta.get("visual_frame_count", meta.get("emoji_frame_count"))
    return ANIMATED if isinstance(count, int) and count > 1 else meta.get("auto_reply_exclusion", "")


def known_exclusion(conn, message):
    if reason := exclusion(message):
        return reason
    if not is_image(message):
        return ""
    record = conn.execute(
        "SELECT state,reason,trace_json FROM incoming_media WHERE message_id=%s", (message["id"],)
    ).fetchone()
    if record:
        trace = json.loads(record["trace_json"])
        if trace.get("animated") is True or (
            isinstance(trace.get("frame_count"), int) and trace["frame_count"] > 1
        ):
            return ANIMATED
        if record["state"] == "skipped":
            return record["reason"] or UNVERIFIED
    return ""


def mark(conn, mid, now, *, frame_count=None, reason="", frame_count_exact=True):
    trace = {
        "emoji_policy": VERSION,
        "visual_policy": VERSION,
        "frame_count": frame_count,
        "animated": frame_count > 1 if frame_count else None,
        "frame_count_exact": frame_count_exact,
    }
    conn.execute(
        "UPDATE incoming_media SET trace_json=trace_json||%s::jsonb,state=CASE WHEN %s<>'' THEN 'skipped' ELSE state END,reason=%s,updated_at=%s WHERE message_id=%s",
        (json.dumps(trace), reason, reason, now, mid),
    )
    metadata = {"visual_policy": VERSION, "visual_frame_count": frame_count}
    if reason:
        metadata["auto_reply_exclusion"] = reason
    conn.execute(
        "UPDATE message_records SET metadata=(metadata-'auto_reply_exclusion')||%s::jsonb||CASE WHEN msg_type='47' THEN %s::jsonb ELSE '{}'::jsonb END WHERE id=%s AND msg_type IN ('3','47')",
        (json.dumps(metadata), json.dumps({"emoji_policy": VERSION, "emoji_frame_count": frame_count}), mid),
    )


def prune(manager, conn, task, now):
    """租约持有者在短事务内剔除已判定表情，剩余原任务按既有机制重排。"""
    from .dialogue_windows import inputs, recover_pat
    from .reply_queue import members

    lock = conn.execute("SELECT owner,expires_at FROM worker_lock WHERE id=1 FOR UPDATE").fetchone()
    if not lock or lock["owner"] != task.get("_owner") or lock["expires_at"] <= now:
        return False
    current = conn.execute("SELECT * FROM tasks WHERE id=%s FOR UPDATE", (task["id"],)).fetchone()
    if (
        not current
        or current["kind"] != "auto"
        or current["state"] not in ("queued", "generating", "preparing", "ready")
        or current["lease_owner"] != task.get("_owner")
    ):
        return False
    batch = members(conn, task)
    excluded = {m["id"]: exclusion(m) for m in inputs(conn, batch, include_excluded=True) if exclusion(m)}
    if not excluded:
        return False
    for mid, reason in excluded.items():
        conn.execute("UPDATE reply_input_links SET in_context=0 WHERE message_id=%s", (mid,))
        conn.execute(
            "UPDATE dialogue_windows SET closed_reason=%s WHERE source_mid=%s AND closed_reason=''",
            (reason, mid),
        )
    conn.execute("DELETE FROM reply_batch_items WHERE batch_id=%s", (task["id"],))
    conn.execute("DELETE FROM reply_batches WHERE task_id=%s", (task["id"],))
    conn.execute("DELETE FROM reply_view_versions WHERE task_id=%s", (task["id"],))
    for member in batch:
        reason = excluded.get(member["message_id"], "")
        if not reason:
            message = conn.execute("SELECT * FROM messages WHERE id=%s", (member["message_id"],)).fetchone()
            if message:
                guard = manager.guard(
                    manager.account(member["account_id"], conn),
                    manager.conversation(member["conversation_id"], conn),
                    json.loads(message["metadata"]),
                    message["created_at"],
                    now,
                    conn,
                )
                if guard == "emoji_wakeup_disabled":
                    reason = guard
        if reason:
            recover_pat(conn, member, now)
        conn.execute(
            "UPDATE tasks SET state=%s,reason=%s,content='',context_json='[]',lease_owner=NULL,updated_at=%s,completed_at=%s WHERE id=%s AND state IN ('queued','generating','preparing','ready','merged')",
            (
                "skipped" if reason else "queued",
                reason or "batch_rebuilt",
                now,
                now if reason else None,
                member["id"],
            ),
        )
    return True
