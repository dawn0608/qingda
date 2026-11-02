"""回答所需图片的可信来源、持久依赖与模型归属；不赋予素材独立唤醒权。"""

import hashlib
import json
import re

from .providers import ProviderError

VERSION = "deepseek-native-image-v5"
VISUAL = ("image",)
MEDIA_KINDS = (*VISUAL, "voice")


def references_media(question, kind):
    """只接受对素材的明确承接；数字、颜色、这/那等普通词不构成图片引用。"""
    noun = r"(?:图片|图像|截图|照片|相片)" if kind == "image" else r"(?:语音|录音|音频)"
    english = r"(?:images?|photos?|pictures?|screenshots?)" if kind == "image" else r"(?:audio|recordings?)"
    return bool(
        (kind == "image" and re.search(r"(?:这|那|上|前)(?:一|几|两)?张图|图[中里上内]", question))
        or re.search(
            rf"{noun}[中里上内的]|(?:这|那|刚才|刚发|之前|前面|上面|先前)[^。！？!?；;\n]{{0,12}}{noun}"
            rf"|(?:看|读|分析|比较|对比|识别|描述|总结|解释|锐评)[^。！？!?；;\n]{{0,6}}{noun}",
            question,
        )
        or re.search(
            rf"\b(?:this|that|these|those|previous|last|above|attached)\s+{english}\b"
            rf"|\b(?:in|on|from|read|analyze|compare|describe)\s+(?:the\s+)?{english}\b",
            question,
            re.I,
        )
    )


def revision(task):
    return task.get("_group_revision", task.get("_private_revision", 0))


def original(conn, message):
    reference_id = json.loads(message["metadata"]).get("reference_id")
    if not reference_id:
        return None
    rows = conn.execute(
        "SELECT m.* FROM messages m LEFT JOIN message_views v ON v.key=('m:'||lpad(m.id::text,20,'0')) "
        "WHERE m.account_id=%s AND m.conversation_id=%s AND m.platform_id=%s "
        "AND m.id<>%s AND m.created_at<=%s AND m.outbound_task_id IS NULL "
        "AND coalesce(v.visible,1)=1 AND coalesce(v.revoked,0)=0 LIMIT 2",
        (
            message["account_id"],
            message["conversation_id"],
            reference_id,
            message["id"],
            message["created_at"],
        ),
    ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def visible(conn, task, mid):
    return conn.execute(
        "SELECT m.* FROM messages m JOIN accounts a ON a.id=m.account_id "
        "JOIN events e ON e.id=m.event_id JOIN terminals t ON t.app_id=e.app_id AND t.account_id=a.id "
        "LEFT JOIN message_views v ON v.key=('m:'||lpad(m.id::text,20,'0')) "
        "WHERE m.id=%s AND m.account_id=%s AND m.conversation_id=%s "
        "AND e.app_id=a.current_app_id AND t.verified=1 "
        "AND coalesce(v.visible,1)=1 AND coalesce(v.revoked,0)=0",
        (mid, task["account_id"], task["conversation_id"]),
    ).fetchone()


def select(conn, task, inputs, group):
    from .emoji_policy import known_exclusion

    sources = []
    has_direct = any(json.loads(row["metadata"]).get("message_kind") in VISUAL for row in inputs)
    has_reference = any(json.loads(row["metadata"]).get("reference_id") for row in inputs)
    for row in inputs:
        meta = json.loads(row["metadata"])
        if meta.get("message_kind") in MEDIA_KINDS and not known_exclusion(conn, row):
            sources.append((row["id"], row["id"], "direct"))
        target = original(conn, row)
        if (
            target
            and json.loads(target["metadata"]).get("message_kind") in MEDIA_KINDS
            and not known_exclusion(conn, target)
        ):
            sources.append((row["id"], target["id"], "quote"))
    actual = [row for row in inputs if json.loads(row["metadata"]).get("message_kind") != "pat"]
    question = " ".join(
        (
            conn.execute(
                "SELECT transcript FROM incoming_media WHERE message_id=%s AND state='ready'", (row["id"],)
            ).fetchone()
            or [""]
        )[0]
        if json.loads(row["metadata"]).get("message_kind") == "voice"
        else (row["text"] or "")
        for row in actual
    )
    referenced_kinds = [kind for kind in MEDIA_KINDS if references_media(question, kind)]
    if group and actual and referenced_kinds and not has_direct and not has_reference:
        anchor = actual[-1]
        rows = conn.execute(
            "SELECT m.* FROM messages m WHERE m.account_id=%s AND m.conversation_id=%s "
            "AND m.sender=%s AND m.direction='inbound' AND m.metadata->>'message_kind'=ANY(%s) "
            "AND NOT(m.id=ANY(%s)) "
            "AND m.created_at BETWEEN %s AND %s AND m.received_at BETWEEN %s AND %s "
            "ORDER BY m.created_at DESC,m.id DESC LIMIT 30",
            (
                task["account_id"],
                task["conversation_id"],
                anchor["sender"],
                referenced_kinds,
                [row["id"] for row in inputs],
                anchor["created_at"] - 120,
                anchor["created_at"],
                anchor["received_at"] - 120,
                anchor["received_at"],
            ),
        ).fetchall()
        from .dialogue_windows import other_recipient

        candidates = []
        for row in rows:
            meta = json.loads(row["metadata"])
            if (
                meta.get("history")
                or meta.get("test")
                or other_recipient(meta)
                or known_exclusion(conn, row)
                or not visible(conn, task, row["id"])
            ):
                continue
            candidates.append((anchor["id"], row["id"], "recent"))
            if len(candidates) == 5:
                break
        sources.extend(reversed(candidates))
    return [s for s in sources if visible(conn, task, s[1])]


def sources(conn, task, inputs, group):
    if task.get("_media_frozen"):
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT origin_message_id,source_message_id,source_type FROM reply_media_dependencies "
                "WHERE task_id=%s AND input_revision=%s ORDER BY position",
                (task["id"], revision(task)),
            )
        ]
    return select(conn, task, inputs, group)


def freeze(manager, task, batch):
    from .dialogue_windows import inputs
    from .emoji_policy import known_exclusion

    with manager.store.transaction() as conn:
        current = conn.execute(
            "SELECT lease_owner,state FROM tasks WHERE id=%s FOR UPDATE", (task["id"],)
        ).fetchone()
        if not current or current["state"] != "generating" or current["lease_owner"] != task.get("_owner"):
            raise ProviderError("task_canceled")
        group = manager.conversation(task["conversation_id"], conn)["kind"] == "group"
        current_inputs = inputs(conn, batch)
        task["_excluded_media_reference"] = any(
            (target := original(conn, row)) and known_exclusion(conn, target) for row in current_inputs
        )
        selected = select(conn, task, current_inputs, group)
        conn.execute("DELETE FROM reply_media_dependencies WHERE task_id=%s", (task["id"],))
        for position, (origin, mid, kind) in enumerate(selected):
            conn.execute(
                "INSERT INTO reply_media_dependencies(task_id,origin_message_id,source_message_id,source_type,input_revision,position) VALUES(%s,%s,%s,%s,%s,%s)",
                (task["id"], origin, mid, kind, revision(task), position),
            )
    task["_media_frozen"] = True
    return list(dict.fromkeys(mid for _, mid, kind in selected if kind != "direct"))


def check(conn, task):
    from .private_replies import change_reason

    for row in conn.execute(
        "SELECT source_message_id FROM reply_media_dependencies WHERE task_id=%s AND input_revision=%s",
        (task["id"], revision(task)),
    ):
        if not visible(conn, task, row[0]):
            return change_reason(task)
    return ""


def snapshot(manager, task):
    result = []
    with manager.store.transaction() as conn:
        current = conn.execute(
            "SELECT lease_owner,state FROM tasks WHERE id=%s FOR UPDATE", (task["id"],)
        ).fetchone()
        if not current or current["state"] != "generating" or current["lease_owner"] != task.get("_owner"):
            raise ProviderError("task_canceled")
        if reason := check(conn, task):
            raise ProviderError(reason)
        for row in conn.execute(
            "SELECT * FROM reply_media_dependencies WHERE task_id=%s ORDER BY position", (task["id"],)
        ).fetchall():
            record = conn.execute(
                "SELECT * FROM incoming_media WHERE message_id=%s", (row["source_message_id"],)
            ).fetchone()
            state = record["state"] if record else "not_prepared"
            frames = json.loads(record["frames_json"]) if record else []
            fingerprint = hashlib.sha256(json.dumps([state, frames], sort_keys=True).encode()).hexdigest()
            conn.execute(
                "UPDATE reply_media_dependencies SET media_state=%s,fingerprint=%s WHERE task_id=%s AND position=%s",
                (state, fingerprint, task["id"], row["position"]),
            )
            result.append(
                {
                    "origin_message_id": row["origin_message_id"],
                    "source_message_id": row["source_message_id"],
                    "source_type": row["source_type"],
                    "media_state": state,
                    "fingerprint": fingerprint,
                }
            )
    return result


def context_images(manager, task, context, commit):
    """当前素材优先；仅明确追问时附带相关历史像素，并冻结其来源依赖。"""
    current = [value for value in context if value.get("current")]
    question = " ".join(value.get("content", "") for value in current)
    historical = []
    if (
        not any(value.get("images") for value in current)
        and not task.get("_excluded_media_reference")
        and not any(word in question for word in ("视频", "动图", "表情"))
        and references_media(question, "image")
    ):
        historical = [
            value
            for value in context
            if not value.get("current") and value.get("context_scope") != "background" and value.get("images")
        ][-2:]
    selected = {value["message_id"] for value in historical}
    for value in context:
        if not value.get("current") and value.get("images") and value.get("message_id") not in selected:
            value.pop("images")
            value["media_status"] = "historical_image_omitted"
    if selected:
        with manager.store.transaction() as conn:
            if reason := commit(conn):
                raise ProviderError(reason)
            position = conn.execute(
                "SELECT coalesce(max(position),-1)+1 FROM reply_media_dependencies WHERE task_id=%s",
                (task["id"],),
            ).fetchone()[0]
            for value in historical:
                mid = value["message_id"]
                if not visible(conn, task, mid):
                    raise ProviderError("history_source_changed")
                if not conn.execute(
                    "SELECT 1 FROM reply_media_dependencies WHERE task_id=%s AND source_message_id=%s",
                    (task["id"], mid),
                ).fetchone():
                    conn.execute(
                        "INSERT INTO reply_media_dependencies(task_id,origin_message_id,source_message_id,source_type,input_revision,position) VALUES(%s,%s,%s,'recent',%s,%s)",
                        (task["id"], task["message_id"], mid, revision(task), position),
                    )
                    position += 1
    return context


def attach(manager, conn, task, current, inputs, group):
    from .private_context import input_allowed, item

    by_id = {v["message_id"]: v for v in current}
    for value in current:
        for frame in value.get("images", []):
            frame.update(
                source_type="direct", origin_message_id=value["message_id"], sender=value.get("sender")
            )
    for origin, mid, kind in sources(conn, task, inputs, group):
        value = by_id.get(origin)
        if not value or kind == "direct":
            continue
        row = visible(conn, task, mid)
        if not row or not input_allowed(manager, row, conn):
            continue
        material = item(manager, dict(row), conn)
        frames = material.pop("images", [])
        material.update(source_type=kind, origin_message_id=origin)
        if kind == "quote":
            value["reference"] = material
        else:
            field = "recent_images" if material.get("message_kind") == "image" else "recent_media"
            value.setdefault(field, []).append(material)
        value.setdefault("images", []).extend(
            {**frame, "source_type": kind, "origin_message_id": origin, "sender": row["sender"]}
            for frame in frames
        )
    return {frame["message_id"] for value in current for frame in value.get("images", [])}
