"""账号队列的持久批次、发送完成水位与模型输入容量边界。"""

import json

from .providers import ProviderError

TEXT_LIMIT = 2 * 1024 * 1024
IMAGE_LIMIT = 47 * 1024 * 1024
SEND_INTERVAL_SECONDS = 1


def members(conn, task):
    rows = conn.execute(
        "SELECT t.* FROM reply_batch_items b JOIN tasks t ON t.id=b.task_id WHERE b.batch_id=%s ORDER BY b.position",
        (task["id"],),
    ).fetchall()
    return [dict(row) for row in rows] or [dict(task)]


def freeze(conn, task, queue, now, check):
    if (
        task["kind"] != "auto"
        or conn.execute("SELECT 1 FROM reply_batches WHERE task_id=%s", (task["id"],)).fetchone()
    ):
        return
    chosen = [task]
    collecting_pat = conn.execute("SELECT 1 FROM reply_not_before WHERE task_id=%s", (task["id"],)).fetchone()
    first = conn.execute("SELECT * FROM messages WHERE id=%s", (task["message_id"],)).fetchone()
    sender = first["sender"] if first else None
    if sender and not sender.endswith("@chatroom") and not check(task, conn):
        previous = first["id"]
        for candidate in queue:
            if candidate["account_id"] != task["account_id"]:
                continue
            if candidate["id"] == task["id"]:
                continue
            if (
                candidate["kind"] != "auto"
                or candidate["conversation_id"] != task["conversation_id"]
                or any(
                    candidate[k] != task[k] for k in ("account_epoch", "conversation_epoch", "config_version")
                )
                or conn.execute("SELECT 1 FROM reply_batches WHERE task_id=%s", (candidate["id"],)).fetchone()
                or check(candidate, conn)
            ):
                if collecting_pat:
                    continue
                break
            msg = conn.execute("SELECT * FROM messages WHERE id=%s", (candidate["message_id"],)).fetchone()
            if not msg or msg["sender"] != sender or msg["id"] <= previous:
                if collecting_pat:
                    continue
                break
            # 未进入自动队列的群消息、其他聊天、人工消息也构成连续边界。
            if (
                not collecting_pat
                and conn.execute(
                    "SELECT 1 FROM messages WHERE account_id=%s AND id>%s AND id<%s AND (conversation_id!=%s OR sender IS NULL OR sender!=%s OR direction!='inbound') LIMIT 1",
                    (task["account_id"], previous, msg["id"], task["conversation_id"], sender),
                ).fetchone()
            ):
                break
            chosen.append(candidate)
            previous = msg["id"]
    conn.execute("INSERT INTO reply_batches VALUES(%s,%s)", (task["id"], now))
    for position, item in enumerate(chosen):
        conn.execute("INSERT INTO reply_batch_items VALUES(%s,%s,%s)", (item["id"], task["id"], position))
        if position:
            conn.execute(
                "UPDATE tasks SET state='merged',reason='batch_joined',updated_at=%s WHERE id=%s AND state='queued'",
                (now, item["id"]),
            )


def split(conn, task, keep, now):
    """拆出不可再追加消息的剩余批次，不丢弃原任务或改变原入队顺序。"""
    remaining = members(conn, task)[keep:]
    if not remaining:
        return
    leader = remaining[0]["id"]
    conn.execute("INSERT INTO reply_batches VALUES(%s,%s)", (leader, now))
    for position, item in enumerate(remaining):
        conn.execute(
            "UPDATE reply_batch_items SET batch_id=%s,position=%s WHERE task_id=%s",
            (leader, position, item["id"]),
        )
    conn.execute(
        "UPDATE tasks SET state='queued',reason='batch_capacity_split',lease_owner=NULL,updated_at=%s WHERE id=%s AND state='merged'",
        (now, leader),
    )


def ended(conn, account_id, now):
    conn.execute(
        "INSERT INTO account_send_clock VALUES(%s,%s) ON CONFLICT(user_id,account_id) DO UPDATE SET ended_at=greatest(account_send_clock.ended_at,excluded.ended_at)",
        (account_id, now),
    )


def wait_seconds(conn, account_id, now):
    row = conn.execute(
        "SELECT ended_at FROM account_send_clock WHERE account_id=%s", (account_id,)
    ).fetchone()
    return max(0, row[0] + SEND_INTERVAL_SECONDS - now) if row else 0


def current_start(context):
    return next((i for i, item in enumerate(context) if item.get("current")), len(context) - 1)


def fit_context(context, media=None, text_tokens=None):
    import copy

    from .context_budget import INPUT_TOKENS, tokens

    context = copy.deepcopy(context)
    budget = INPUT_TOKENS if text_tokens is None else text_tokens
    while context:
        text_size = len(json.dumps(context, ensure_ascii=False).encode())
        image_size = (
            sum(
                media.path(ref).stat().st_size * 4 // 3 + 1024
                for ref in {
                    (r["message_id"], r.get("sha256")): r for item in context for r in item.get("images", [])
                }.values()
            )
            if media
            else 0
        )
        token_size = tokens(context) + sum(len(v.get("images", [])) * 1056 for v in context)
        if text_size <= TEXT_LIMIT and image_size < IMAGE_LIMIT and token_size <= budget:
            anchor = context[current_start(context)]
            if "history_selection" in anchor:
                retained = [v for v in context if not v.get("current")]
                anchor["history_selection"].update(
                    oldest_timestamp=min((v.get("timestamp") or 0 for v in retained), default=None),
                    newest_timestamp=max((v.get("timestamp") or 0 for v in retained), default=None),
                    selected_messages=len(retained),
                    selected_rounds=len(
                        {v.get("history_round") for v in retained if v.get("context_scope") != "background"}
                    ),
                )
            return context
        if image_size >= IMAGE_LIMIT:
            # 当前问题与真实引用优先，容量删减必须在对应素材上显式标记。
            chosen = next((v for v in context if not v.get("current") and v.get("images")), None)
            recent = False
            if chosen is None:
                chosen = next(
                    (
                        v
                        for v in context
                        if any(r.get("source_type") == "recent" for r in v.get("images", []))
                    ),
                    None,
                )
                recent = chosen is not None
            if chosen is not None:
                if recent:
                    chosen["images"] = [r for r in chosen["images"] if r.get("source_type") != "recent"]
                    for value in chosen.get("recent_images", []):
                        value["media_status"] = "omitted_capacity"
                else:
                    chosen.pop("images", None)
                    chosen["media_status"] = "historical_image_omitted"
                continue
            chosen = next((v for v in reversed(context) if v.get("images")), None)
            if chosen is not None:
                removed = chosen["images"][-1]["message_id"]
                chosen["images"] = [r for r in chosen["images"] if r["message_id"] != removed]
                target = (
                    chosen.get("reference", {})
                    if chosen.get("reference", {}).get("message_id") == removed
                    else chosen
                )
                target.update(media_status="omitted_capacity", media_note="图片超出本轮容量，未附带")
                continue
        # 背景先让出容量，然后整轮移除较远相关历史；绝不切断当前问题或引用。
        background = next(
            (v for v in context if not v.get("current") and v.get("context_scope") == "background"), None
        )
        historical = background or next(
            (v for v in context if not v.get("current") and not v.get("unresolved_clarification")), None
        )
        if historical is not None:
            anchor = context[current_start(context)]
            if "history_selection" in anchor:
                coverage = anchor["history_selection"]
                coverage["has_more"] = True
                reasons = coverage.setdefault("trim_reasons", {})
                reasons["request_capacity"] = reasons.get("request_capacity", 0) + 1
            round_id = historical.get("history_round")
            context = [
                v
                for v in context
                if v is not historical
                and (round_id is None or v.get("current") or v.get("history_round") != round_id)
            ]
            continue
        boundary = current_start(context)
        pending = next(
            (
                i
                for i, value in enumerate(context)
                if value.get("current") and value.get("input_scope") == "pending"
            ),
            None,
        )
        if pending is not None:
            anchor = context[boundary]
            context.pop(pending)
            if context and pending == boundary:
                for key in (
                    "participant_profiles",
                    "profile_schema_version",
                    "presence_suppressed",
                    "reply_target",
                    "conversation_kind",
                    "history_selection",
                ):
                    if key in anchor:
                        context[current_start(context)][key] = anchor[key]
            continue
        profiles = context[boundary].get("participant_profiles")
        if (text_size > TEXT_LIMIT or token_size > budget) and profiles:
            profiles = dict(profiles)
            target = context[boundary].get("reply_target")
            secondary = [member for member in profiles if member != target]
            if secondary:
                profiles.pop(secondary[-1])
            else:
                profiles.clear()
            context[boundary]["participant_profiles"] = profiles
            continue
        if current_start(context) <= 0:
            raise ProviderError("current_batch_too_large")
        first = context.pop(0)
        if "history_round" in first:
            context = [
                value
                for value in context
                if value.get("current") or value.get("history_round") != first["history_round"]
            ]
    raise ProviderError("empty_model_context")
