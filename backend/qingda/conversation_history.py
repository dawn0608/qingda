"""按实际处理关联读取历史；群聊先限定成员，历史原文不产生新的回复任务。"""

import json

from . import context_budget

MESSAGE_FROM = (
    " FROM messages m JOIN events e ON e.id=m.event_id "
    "JOIN accounts a ON a.id=m.account_id "
    "JOIN terminals terminal ON terminal.app_id=e.app_id AND terminal.account_id=a.id "
    "LEFT JOIN message_views v ON v.key=('m:'||lpad(m.id::text,20,'0')) "
)
MESSAGE_VISIBLE = (
    "m.account_id=%s AND m.conversation_id=%s AND terminal.verified=1 "
    "AND coalesce(e.payload->>'Wxid',a.wxid)=a.wxid "
    "AND coalesce(v.visible,1)=1 AND coalesce(v.revoked,0)=0 "
    "AND NOT coalesce((m.metadata->>'test')::boolean,false) "
    "AND m.metadata->>'message_kind' IN ('text','image','voice','pat')"
)
ANSWER_FROM = (
    " FROM tasks t JOIN messages m ON m.id=t.message_id "
    "LEFT JOIN reply_decisions d ON d.task_id=t.id "
    "LEFT JOIN message_views v ON v.key='t:'||t.id "
)
ANSWER_SELECT = "SELECT t.*,m.sender AS reply_to_sender,d.close_dialogue,d.reason AS decision_reason"
ANSWER_VISIBLE = (
    "t.account_id=%s AND t.conversation_id=%s AND t.kind='auto' AND t.state='accepted' "
    "AND coalesce(v.visible,1)=1 AND coalesce(v.revoked,0)=0 "
    "AND NOT EXISTS(SELECT 1 FROM message_views av JOIN messages am "
    "ON av.key=('m:'||lpad(am.id::text,20,'0')) WHERE am.account_id=t.account_id "
    "AND am.conversation_id=t.conversation_id AND am.platform_id=t.response_json#>>'{data,newMsgId}' "
    "AND (av.revoked=1 OR av.visible=0))"
)


def order(value):
    mid = value["message_id"]
    identity = str(mid).zfill(20) if isinstance(mid, int) else str(mid)
    return (value.get("timestamp") or 0, value.get("received_at") or value.get("timestamp") or 0, identity)


def rows_by_ids(manager, conn, task, ids):
    from .private_context import input_allowed

    if not ids:
        return {}
    return {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT m.*" + MESSAGE_FROM + " WHERE " + MESSAGE_VISIBLE + " AND m.id=ANY(%s)",
            (task["account_id"], task["conversation_id"], list(ids)),
        )
        if input_allowed(manager, row, conn)
    }


def answer_item(row):
    from .private_context import normalize_emotions

    result = {
        "role": "assistant",
        "speaker_type": "assistant",
        "message_id": row["id"],
        "sender": "self",
        "content": normalize_emotions(row["content"]),
        "timestamp": row["completed_at"] or row["updated_at"],
        "reply_to_sender": row["reply_to_sender"],
        "close_dialogue": bool(row["close_dialogue"]),
        "processing_state": "answered",
        "decision_reason": row["decision_reason"],
    }
    if result["content"] != row["content"]:
        result["original_content"] = row["content"]
    return result


def answer_rounds(manager, conn, task, answers, excluded=()):
    from .private_context import item
    from .reply_decisions import handled_ids

    linked = [(dict(answer), handled_ids(conn, dict(answer))) for answer in answers]
    ids = set().union(*(ids for _, ids in linked)) if linked else set()
    rows = rows_by_ids(manager, conn, task, ids)
    result = []
    for answer, ids in linked:
        # 撤回或不可见的问题不能留下脱离来源的助手答案。
        if not ids or not ids <= rows.keys() or ids.intersection(excluded):
            continue
        values = []
        for mid in ids:
            if json.loads(rows[mid]["metadata"]).get("message_kind") == "pat":
                continue
            value = item(manager, rows[mid], conn)
            value.update(processing_state="answered", reply_task_id=answer["id"])
            values.append(value)
        if not values:
            continue
        values.sort(key=order)
        values.append(answer_item(answer))
        for value in values:
            value["history_round"] = answer["id"]
        result.append(values)
    return result


def recent_answers(conn, task, boundary, sender=None, limit=100):
    return conn.execute(
        ANSWER_SELECT
        + ANSWER_FROM
        + " WHERE "
        + ANSWER_VISIBLE
        + " AND t.completed_at<=%s"
        + (" AND m.sender=%s" if sender is not None else "")
        + " ORDER BY t.completed_at DESC,t.ordinal DESC LIMIT %s",
        (task["account_id"], task["conversation_id"], boundary)
        + ((sender,) if sender is not None else ())
        + (limit,),
    ).fetchall()


def raw_recent(manager, conn, task, boundary, excluded, limit):
    from .private_context import input_allowed, item

    rows = conn.execute(
        "SELECT m.*,t.state AS processing_state,d.reason AS decision_reason"
        + MESSAGE_FROM
        + " LEFT JOIN tasks t ON t.message_id=m.id AND t.kind='auto' "
        "LEFT JOIN reply_decisions d ON d.task_id=t.id WHERE "
        + MESSAGE_VISIBLE
        + " AND m.received_at<=%s AND NOT(m.id=ANY(%s)) AND m.outbound_task_id IS NULL "
        "AND m.metadata->>'message_kind'<>'pat' "
        "AND coalesce(t.reason,'') NOT LIKE 'reply_wait%%' "
        "AND NOT EXISTS(SELECT 1 FROM reply_input_waits w WHERE w.account_id=m.account_id "
        "AND w.conversation_id=m.conversation_id AND w.message_ids @> jsonb_build_array(m.id)) "
        "AND NOT EXISTS(SELECT 1 FROM tasks sent WHERE sent.account_id=m.account_id "
        "AND sent.conversation_id=m.conversation_id AND sent.state='accepted' "
        "AND sent.response_json#>>'{data,newMsgId}'=m.platform_id) "
        "ORDER BY m.created_at DESC,m.id DESC LIMIT %s",
        (task["account_id"], task["conversation_id"], boundary, list(excluded), limit),
    ).fetchall()
    result = []
    for row in rows:
        if not input_allowed(manager, row, conn):
            continue
        value = item(manager, row, conn)
        value.update(history_round="m:" + str(row["id"]), processing_state=row["processing_state"])
        if row["decision_reason"]:
            value["decision_reason"] = row["decision_reason"]
        result.append([value])
    # 管理台人工发送记录不一定有独立回调；保留其 owner 身份。
    for row in conn.execute(
        "SELECT t.* FROM tasks t LEFT JOIN message_views v ON v.key='t:'||t.id "
        "WHERE t.account_id=%s AND t.conversation_id=%s AND t.kind='manual' AND t.state='accepted' "
        "AND t.completed_at<=%s AND coalesce(v.visible,1)=1 AND coalesce(v.revoked,0)=0 "
        "ORDER BY t.completed_at DESC LIMIT %s",
        (task["account_id"], task["conversation_id"], boundary, limit),
    ):
        result.append(
            [
                {
                    "message_id": row["id"],
                    "history_round": row["id"],
                    "role": "user",
                    "speaker_type": "owner",
                    "sender": "self",
                    "content": row["content"],
                    "timestamp": row["completed_at"],
                    "processing_state": "manual",
                }
            ]
        )
    return result


def select_units(units, limits, reasons=None):
    rounds, count, budget = limits
    chosen, size, used = [], 0, 0
    seen = set()
    reasons = {} if reasons is None else reasons
    for values in sorted(
        units,
        key=lambda unit: (any(v.get("unresolved_clarification") for v in unit), max(order(v) for v in unit)),
        reverse=True,
    ):
        cost = context_budget.tokens(values)
        identities = {v["message_id"] for v in values}
        if len(chosen) >= rounds:
            reasons["round_limit"] = len(units) - len(chosen)
            break
        if seen.intersection(identities):
            reasons["duplicate_round"] = reasons.get("duplicate_round", 0) + 1
            continue
        if size + len(values) > count or used + cost > budget:
            reason = "message_limit" if size + len(values) > count else "token_limit"
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        chosen.append(values)
        seen.update(identities)
        size, used = size + len(values), used + cost
    return chosen, used


def build(manager, conn, task, targets, group):
    boundary = max(v["received_at"] for v in targets)
    excluded = {v["id"] for v in targets}
    target = targets[0]["sender"]
    limits = context_budget.GROUP if group else context_budget.PRIVATE
    answers = recent_answers(conn, task, boundary, target if group else None, limits[0] * 2)
    units = answer_rounds(manager, conn, task, answers, excluded)
    if answers and answers[0]["decision_reason"] == "clarify":
        for unit in units:
            if unit[-1]["message_id"] == answers[0]["id"]:
                for value in unit:
                    value["unresolved_clarification"] = True
    linked = {v["message_id"] for unit in units for v in unit if isinstance(v["message_id"], int)}
    if not group:
        units += raw_recent(manager, conn, task, boundary, excluded | linked, limits[1])
    for unit in units:
        for value in unit:
            value["context_scope"] = "related"
    reasons = {}
    selected, used = select_units(units, limits, reasons)
    history = []
    for unit in sorted(selected, key=lambda values: max(order(v) for v in values)):
        for value in unit:
            value["context_scope"] = "related"
            history.append(value)
    if group:
        others = [a for a in recent_answers(conn, task, boundary, limit=20) if a["reply_to_sender"] != target]
        background = answer_rounds(manager, conn, task, others, excluded)
        other_ids = {v["message_id"] for unit in background for v in unit if isinstance(v["message_id"], int)}
        background += raw_recent(manager, conn, task, boundary, excluded | linked | other_ids, 20)
        for unit in background:
            for value in unit:
                value["context_scope"] = "background"
        bg_count, bg_tokens = context_budget.BACKGROUND
        selected_bg, _ = select_units(
            background,
            (
                bg_count,
                min(bg_count, max(0, limits[1] - len(history))),
                min(bg_tokens, max(0, limits[2] - used)),
            ),
        )
        for unit in sorted(selected_bg, key=lambda values: max(order(v) for v in values)):
            for value in unit:
                value["context_scope"] = "background"
                history.append(value)
    # 批次曾包含多个原任务时去重；轮次标识仍来自实际已发送回答。
    unique = {}
    for value in history:
        unique.setdefault(value["message_id"], value)
    older = conn.execute(
        "SELECT 1"
        + MESSAGE_FROM
        + " WHERE "
        + MESSAGE_VISIBLE
        + " AND m.received_at<=%s AND NOT(m.id=ANY(%s))"
        + (" AND m.sender=%s" if group else "")
        + " LIMIT 1",
        (
            task["account_id"],
            task["conversation_id"],
            boundary,
            list(excluded | {mid for mid in unique if isinstance(mid, int)}),
        )
        + ((target,) if group else ()),
    ).fetchone()
    return list(unique.values()), {
        "policy": context_budget.VERSION,
        "candidate_rounds": len(units),
        "selected_rounds": len(selected),
        "trim_reasons": reasons,
        "has_more": bool(older),
        "scope": "current_member" if group else "current_conversation",
        "oldest_timestamp": min((v.get("timestamp") or 0 for v in history), default=None),
        "newest_timestamp": max((v.get("timestamp") or 0 for v in history), default=None),
    }
