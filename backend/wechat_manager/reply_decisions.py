"""回复决策契约；静默是明确终态，不能从空正文或模型异常推断。"""

import json

from .providers import ProviderError
from .reply_policy import VERSION

REASONS = (
    "answer",
    "clarify",
    "social_close",
    "ack_only",
    "off_topic",
    "addressed_elsewhere",
    "explicit_silence",
    "presence",
    "awaiting_input",
)
TOOL = {
    "type": "function",
    "function": {
        "name": "finish_reply",
        "description": "结束本轮处理。需要回复时提交微信正文；无需回复时明确静默。不发送消息、不改变权限。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["reply", "silent", "wait"]},
                "content": {
                    "type": "string",
                    "description": "实际微信正文；silent/wait为空。按问题复杂度自然表达，完整处理当前需求，精确输出保留原字符。",
                },
                "reason": {
                    "type": "string",
                    "enum": list(REASONS),
                    "description": "answer回答；clarify必要澄清；presence纯拍应声；social_close收尾；ack_only无需确认；off_topic无关；addressed_elsewhere面向他人；explicit_silence停止；awaiting_input明确等待后续输入。",
                },
                "handled_message_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "uniqueItems": True,
                    "description": "覆盖所有current_messages的message_id，另可选择与本次唤醒直接相关的pending_messages；不得填写信号、历史或引用ID。纯拍一拍填空数组。",
                },
                "close_dialogue": {
                    "type": "boolean",
                    "description": "reason为social_close、ack_only、explicit_silence时必须true；其他原因必须false",
                },
                "wait_evidence": {
                    "type": "object",
                    "description": "仅action=wait必填：本轮真实文字或语音中明确要求等待的原文，不得用引用、图片、历史或已承接材料。",
                    "properties": {"message_id": {"type": "integer"}, "quote": {"type": "string"}},
                    "required": ["message_id", "quote"],
                    "additionalProperties": False,
                },
            },
            "required": ["action", "content", "reason", "close_dialogue", "handled_message_ids"],
            "additionalProperties": False,
        },
    },
}


def original_text(value):
    text = value.get("original_content", value.get("content", ""))
    return text.removeprefix("[语音转写] ") if value.get("message_kind") == "voice" else text


def validate(value, context=None):
    if (
        not isinstance(value, dict)
        or set(value)
        - {"action", "content", "reason", "close_dialogue", "handled_message_ids", "wait_evidence"}
        or not {"action", "content", "reason", "close_dialogue", "handled_message_ids"} <= set(value)
        or value["action"] not in ("reply", "silent", "wait")
        or value["reason"] not in REASONS
        or type(value["close_dialogue"]) is not bool
        or not isinstance(value["content"], str)
        or len(value["content"]) > 10000
        or not isinstance(value["handled_message_ids"], list)
        or len(value["handled_message_ids"]) > 1000
        or any(type(mid) is not int or mid <= 0 for mid in value["handled_message_ids"])
        or len(set(value["handled_message_ids"])) != len(value["handled_message_ids"])
    ):
        raise ProviderError("invalid_reply_decision")
    if value["action"] == "reply":
        valid = bool(value["content"].strip()) and value["reason"] in (
            "answer",
            "clarify",
            "social_close",
            "presence",
        )
    elif value["action"] == "wait":
        valid = not value["content"] and value["reason"] == "awaiting_input"
    else:
        valid = not value["content"] and value["reason"] not in (
            "answer",
            "clarify",
            "presence",
            "awaiting_input",
        )
    evidence = value.get("wait_evidence")
    if value["action"] == "wait":
        valid = valid and isinstance(evidence, dict) and set(evidence) == {"message_id", "quote"}
        valid = valid and type(evidence.get("message_id")) is int and isinstance(evidence.get("quote"), str)
        valid = valid and bool(evidence["quote"].strip()) and len(evidence["quote"]) <= 500
    elif "wait_evidence" in value:
        valid = False
    if value["close_dialogue"] and value["reason"] not in ("social_close", "ack_only", "explicit_silence"):
        valid = False
    if value["reason"] in ("social_close", "ack_only", "explicit_silence") and not value["close_dialogue"]:
        valid = False
    if not valid:
        raise ProviderError("invalid_reply_decision")
    if value["reason"] == "presence" and len(value["content"]) > 12:
        raise ProviderError("invalid_reply_decision")
    if context is not None:
        current = [v for v in context if v.get("current")]
        actual = [v for v in current if v.get("message_kind") != "pat"]
        allowed = {v["message_id"] for v in actual if type(v.get("message_id")) is int}
        required = {
            v["message_id"]
            for v in actual
            if v.get("input_scope") != "pending" and type(v.get("message_id")) is int
        }
        selected = set(value["handled_message_ids"])
        if value["action"] == "wait" and not any(
            v.get("message_id") == evidence["message_id"]
            and v.get("input_scope", "current") == "current"
            and v.get("message_kind") in ("text", "voice")
            and v.get("media_status") != "unavailable"
            and evidence["quote"] in original_text(v)
            for v in actual
        ):
            raise ProviderError("invalid_reply_decision")
        # 停止必须对应本轮实际发言，不能仅凭历史停止指令吞掉后续唤醒。
        if value["reason"] == "explicit_silence" and not selected:
            raise ProviderError("invalid_reply_decision")
        target = next((v.get("reply_target", v.get("sender")) for v in current), None)
        if not required <= selected <= allowed or any(
            v.get("sender") != target for v in actual if v.get("message_id") in selected
        ):
            raise ProviderError("invalid_reply_decision")
        if value["reason"] == "presence" and (
            selected
            or not any(v.get("message_kind") == "pat" for v in current)
            or any(v.get("presence_suppressed") for v in current)
        ):
            raise ProviderError("invalid_reply_decision")
        if current and value["action"] == "reply" and not selected and value["reason"] != "presence":
            raise ProviderError("invalid_reply_decision")
    return value


def record(conn, task, value, now):
    from .callbacks import revoked
    from .dialogue_windows import inputs
    from .reply_queue import members

    messages = inputs(conn, members(conn, task))
    context = [
        {
            "current": True,
            "message_id": m["id"],
            "sender": m["sender"],
            "message_kind": json.loads(m["metadata"]).get("message_kind"),
            "input_scope": {"backfill": "pending", "deferred": "deferred"}.get(
                m.get("_input_kind"), "current"
            ),
            "content": (
                (
                    conn.execute(
                        "SELECT transcript FROM incoming_media WHERE message_id=%s AND state='ready'",
                        (m["id"],),
                    ).fetchone()
                    or [""]
                )[0]
                if json.loads(m["metadata"]).get("message_kind") == "voice"
                else m["text"]
            ),
        }
        for m in messages
    ]
    value = validate(value, context)
    if any(revoked(conn, mid) for mid in value["handled_message_ids"]):
        raise ProviderError("message_revoked")
    revision = task.get("_group_revision", task.get("_private_revision", 0))
    conn.execute(
        "INSERT INTO reply_decisions(task_id,input_revision,action,reason,close_dialogue,policy_version,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id,task_id) DO UPDATE SET input_revision=excluded.input_revision,action=excluded.action,reason=excluded.reason,close_dialogue=excluded.close_dialogue,policy_version=excluded.policy_version,updated_at=excluded.updated_at",
        (
            task["id"],
            revision,
            value["action"],
            value["reason"],
            int(value["close_dialogue"]),
            VERSION,
            now,
        ),
    )
    conn.execute(
        "UPDATE tasks SET handled_message_ids=%s WHERE id=%s",
        (json.dumps(value["handled_message_ids"]), task["id"]),
    )
    for message in messages:
        if message.get("_input_kind") == "backfill" and message["id"] not in value["handled_message_ids"]:
            conn.execute(
                "DELETE FROM reply_input_links WHERE message_id=%s AND task_id=%s AND kind='backfill'",
                (message["id"], task["id"]),
            )


def handled_ids(conn, task):
    """读取已持久化的原文关联；历史和新任务使用同一契约。"""
    value = task.get("handled_message_ids")
    if value is None:
        row = conn.execute("SELECT handled_message_ids FROM tasks WHERE id=%s", (task["id"],)).fetchone()
        value = row[0] if row else "[]"
    return set(json.loads(value))


def apply_dialogue(conn, task, now):
    """静默完成或平台确认发送后调整本成员窗口，不覆盖随后新唤醒的窗口。"""
    from .dialogue_windows import IDLE, inputs
    from .reply_queue import members

    decision = conn.execute("SELECT * FROM reply_decisions WHERE task_id=%s", (task["id"],)).fetchone()
    if not decision:
        return
    current = inputs(conn, members(conn, task))
    if not current:
        return
    window = conn.execute(
        "SELECT * FROM dialogue_windows WHERE account_id=%s AND conversation_id=%s AND sender=%s",
        (task["account_id"], task["conversation_id"], current[0]["sender"]),
    ).fetchone()
    sources = {json.loads(m["metadata"]).get("window_source_id") for m in current}
    if (
        not window
        or window["source_mid"] not in sources
        or window["closed_reason"] not in ("", "window_expired")
    ):
        return
    args = (task["account_id"], task["conversation_id"], current[0]["sender"])
    if decision["close_dialogue"]:
        conn.execute(
            "UPDATE dialogue_windows SET closed_reason='social_close' WHERE account_id=%s AND conversation_id=%s AND sender=%s",
            args,
        )
    elif decision["action"] == "reply" and now < window["hard_deadline"]:
        conn.execute(
            "UPDATE dialogue_windows SET expires_at=%s,last_activity=%s,closed_reason='' WHERE account_id=%s AND conversation_id=%s AND sender=%s",
            (min(now + IDLE, window["hard_deadline"]), now, *args),
        )
