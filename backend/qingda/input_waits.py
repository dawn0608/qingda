"""显式分条等待：只持久化原文引用，释放绑定原任务，期限不因补充或重启延长。"""

import hashlib
import json
import uuid

from .callbacks import revoked
from .database_async import database_call
from .providers import ProviderError
from .reply_decisions import original_text
from .reply_policy import WAIT_SECONDS

TOOL = {
    "type": "function",
    "function": {
        "name": "finish_input_control",
        "description": "判定等待期间本轮真实输入的控制意图，不回答业务问题。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["continue", "resume", "replace", "cancel"]},
                "message_id": {"type": "integer", "description": "resume/replace/cancel必填，本轮依据消息ID"},
                "quote": {"type": "string", "description": "resume/replace/cancel必填，该消息逐字原文"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def close(conn, row, reason):
    conn.execute(
        "UPDATE reply_input_waits SET state='closed',closed_reason=%s WHERE session_id=%s",
        (reason, row["session_id"]),
    )


def active(manager, conn, account, conv, sender, now, *, lock=False):
    row = conn.execute(
        "SELECT * FROM reply_input_waits WHERE account_id=%s AND conversation_id=%s AND sender=%s AND state<>'closed'"
        + (" FOR UPDATE" if lock else ""),
        (account["id"], conv["id"], sender),
    ).fetchone()
    if not row or row["state"] == "closed":
        return None
    reason = ""
    if (
        row["account_epoch"] != account["epoch"]
        or row["conversation_epoch"] != conv["epoch"]
        or row["config_version"] != manager.store.setting("model_revision", 0, conn)
        or row["owner_wxid"] != account["wxid"]
        or row["app_id"] != account["current_app_id"]
        or not account["enabled"]
        or not account["ai_enabled"]
        or account["archived_at"] is not None
        or not conv["allowed"]
        or conv["removed_at"] is not None
        or conv["pause_until"] > now
    ):
        reason = "rules_changed"
    elif revoked(conn, row["source_mid"]):
        reason = "wait_source_revoked"
    elif row["state"] == "waiting" and now >= row["expires_at"]:
        reason = "wait_expired"
    elif row["state"] == "releasing":
        task = conn.execute("SELECT state FROM tasks WHERE id=%s", (row["release_task_id"],)).fetchone()
        if not task or task[0] not in ("queued", "generating", "preparing", "ready"):
            reason = "release_finished"
    if reason:
        close(conn, row, reason)
        return None
    return dict(row)


def annotate(manager, conn, account, conv, msg, now):
    from .dialogue_windows import other_recipient

    meta = msg["metadata"]
    if (
        msg["outgoing"]
        or conv["kind"] != "group"
        or meta.get("wakeup_reason")
        or not meta.get("dialogue_version")
        or other_recipient(meta)
        or meta.get("message_kind") not in ("text", "image", "voice")
    ):
        return
    row = active(manager, conn, account, conv, msg["sender"], now)
    if row:
        meta.update(
            wakeup_reason="wait_continuation",
            window_source_id=row["source_mid"],
            wait_session_id=row["session_id"],
        )


def authorized(manager, conn, task, message, now):
    """仅已经原子绑定的原文可越过五分钟新消息期限；其他权限照常检查。"""
    account = manager.account(task["account_id"], conn)
    conv = manager.conversation(task["conversation_id"], conn)
    row = active(manager, conn, account, conv, message["sender"], now)
    return bool(
        row
        and row["state"] == "releasing"
        and row["release_task_id"] == task["id"]
        and message["id"] in decode(row["message_ids"])
    )


def start(manager, conn, task, decision, now):
    from .dialogue_windows import inputs
    from .reply_queue import members

    messages = inputs(conn, members(conn, task))
    source = next(m for m in messages if m["id"] == decision["wait_evidence"]["message_id"])
    account = manager.account(task["account_id"], conn)
    conv = manager.conversation(task["conversation_id"], conn)
    prior = active(manager, conn, account, conv, source["sender"], now, lock=True)
    ids = list(dict.fromkeys(m["id"] for m in messages if m["id"] in decision["handled_message_ids"]))
    # 同一次明确等待重复落库不续期；后续释放后又提出等待仍沿用原期限。
    started = prior["started_at"] if prior else now
    expires = prior["expires_at"] if prior else now + WAIT_SECONDS
    session = prior["session_id"] if prior else str(uuid.uuid4())
    conn.execute(
        "INSERT INTO reply_input_waits(account_id,conversation_id,sender,session_id,owner_wxid,app_id,"
        "account_epoch,conversation_epoch,config_version,source_mid,started_at,expires_at,state,message_ids) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'waiting',%s) "
        "ON CONFLICT(user_id,session_id) DO UPDATE SET "
        "session_id=excluded.session_id,owner_wxid=excluded.owner_wxid,app_id=excluded.app_id,"
        "account_epoch=excluded.account_epoch,conversation_epoch=excluded.conversation_epoch,"
        "config_version=excluded.config_version,source_mid=excluded.source_mid,started_at=excluded.started_at,"
        "expires_at=excluded.expires_at,state='waiting',message_ids=excluded.message_ids,"
        "release_task_id=NULL,release_revision=NULL,control='{}',closed_reason=''",
        (
            task["account_id"],
            task["conversation_id"],
            source["sender"],
            session,
            account["wxid"],
            account["current_app_id"],
            task["account_epoch"],
            task["conversation_epoch"],
            task["config_version"],
            source["id"],
            started,
            expires,
            json.dumps(ids),
        ),
    )


def validate_control(value, current):
    if (
        not isinstance(value, dict)
        or set(value) - {"action", "message_id", "quote"}
        or value.get("action") not in ("continue", "resume", "replace", "cancel")
        or ("message_id" in value and type(value["message_id"]) is not int)
        or ("quote" in value and (not isinstance(value["quote"], str) or len(value["quote"]) > 500))
    ):
        raise ProviderError("invalid_wait_control")
    if value["action"] == "continue":
        return {"action": "continue", "message_id": 0, "quote": ""}
    if set(value) != {"action", "message_id", "quote"} or not any(
        v["message_id"] == value["message_id"]
        and v["message_kind"] in ("text", "voice")
        and v.get("media_status") != "unavailable"
        and value["quote"].strip()
        and value["quote"] in original_text(v)
        for v in current
    ):
        raise ProviderError("invalid_wait_control")
    return value


async def classify(gateway, config, current, source, execution):
    from .reply_execution import DEEPSEEK_REQUEST_LIMIT
    from .reply_prompt import fixed
    from .tool_generation import add_usage, runtime_clock

    messages = [
        {
            "role": "system",
            "content": fixed("input_wait_control") + "\nruntime_clock=" + json.dumps(runtime_clock()),
        },
        {
            "role": "user",
            "content": json.dumps({"wait_source": source, "current_messages": current}, ensure_ascii=False),
        },
    ]
    usage = {}
    for attempt in range(3):

        async def call():
            from .context_capture import STAGE

            token = STAGE.set("input_control")
            try:
                return await gateway.chat(config, messages, [TOOL])
            finally:
                STAGE.reset(token)

        response = await execution.slot(
            "deepseek", DEEPSEEK_REQUEST_LIMIT, lambda: execution.run("text", call)
        )
        add_usage(usage, response.get("usage") or {})
        try:
            choice = response["choices"][0]
            calls = choice["message"].get("tool_calls", [])
            if (
                choice["finish_reason"] != "tool_calls"
                or len(calls) != 1
                or calls[0]["function"]["name"] != "finish_input_control"
            ):
                raise ProviderError("invalid_wait_control")
            result = validate_control(json.loads(calls[0]["function"]["arguments"]), current)
            execution.capture.tool(
                {
                    "call_id": calls[0].get("id") or "input-control-" + uuid.uuid4().hex,
                    "name": "finish_input_control",
                    "arguments": json.loads(calls[0]["function"]["arguments"]),
                    "result": {"ok": True, "data": result},
                },
                "input_control",
            )
            return result, usage, attempt + 1
        except (KeyError, TypeError, ValueError, IndexError, ProviderError):
            execution.capture.tool(
                {
                    "call_id": "input-control-invalid-" + uuid.uuid4().hex,
                    "name": "finish_input_control",
                    "result": {"ok": False, "error": {"code": "invalid_wait_control"}},
                },
                "input_control",
            )
            # 重试只含受限控制输入，不保留不可信协议内容、不开放业务工具。
            if attempt == 2:
                raise ProviderError("invalid_wait_control")
            messages.append({"role": "user", "content": fixed("input_wait_control")})


async def process(worker, task, batch, config, execution, commit):
    """返回 True 表示本任务只收集/取消，False 表示进入常规回复。"""
    from .dialogue_windows import inputs
    from .private_context import item
    from .private_replies import current_call
    from .reply_media import revision

    manager, now = worker.manager, worker.clock()

    def snapshot():
        with manager.store.transaction() as conn:
            if reason := commit(conn):
                raise ProviderError(reason)
            current = [m for m in inputs(conn, batch) if m.get("_input_kind") != "deferred"]
            if not current:
                return None
            row = active(
                manager,
                conn,
                manager.account(task["account_id"], conn),
                manager.conversation(task["conversation_id"], conn),
                current[0]["sender"],
                now,
                lock=True,
            )
            if not row or row["state"] == "releasing":
                if row and row["release_task_id"] != task["id"]:
                    raise ProviderError("wait_already_released")
                if row:
                    saved = decode(row["control"])
                    task["_wait_usage"] = saved.get("usage", {})
                    task["_wait_rounds"] = saved.get("model_rounds", 0)
                return None
            return row, current

    snapshot_value = await database_call(snapshot)
    if not snapshot_value:
        return False
    row, current = snapshot_value
    for message in current:
        if json.loads(message["metadata"]).get("message_kind") == "voice" and getattr(manager, "media", None):

            async def prepare():
                return await manager.media.prepare(
                    {**task, "message_id": message["id"], "_media_commit": commit},
                    worker.gateway,
                    execution.guard,
                )

            try:
                await current_call(worker, task, execution.run("media", prepare))
            except (ProviderError, OSError, ValueError):
                if reason := await database_call(execution.guard):
                    raise ProviderError(reason)

    def control_context():
        with manager.store.connect() as conn:
            values = [item(manager, m, conn) for m in current]
            # 不向控制模型提供像素、引用、历史或业务工具结果。
            values = [
                {
                    k: v[k]
                    for k in ("message_id", "message_kind", "content", "original_content", "media_status")
                    if k in v
                }
                for v in values
            ]
            original = conn.execute("SELECT * FROM messages WHERE id=%s", (row["source_mid"],)).fetchone()
            source = item(manager, original, conn) if original else {}
            return values, {"message_id": row["source_mid"], "content": source.get("content", "")}

    values, source = await database_call(control_context)
    control, usage, rounds = {"action": "continue", "message_id": 0, "quote": ""}, {}, 0
    if any(v["message_kind"] in ("text", "voice") and v.get("media_status") != "unavailable" for v in values):
        control, usage, rounds = await current_call(
            worker, task, classify(worker.gateway, config, values, source, execution)
        )

    def finish():
        with manager.store.transaction() as conn:
            if reason := commit(conn):
                raise ProviderError(reason)
            latest = active(
                manager,
                conn,
                manager.account(task["account_id"], conn),
                manager.conversation(task["conversation_id"], conn),
                current[0]["sender"],
                worker.clock(),
                lock=True,
            )
            if not latest or latest["session_id"] != row["session_id"]:
                raise ProviderError("input_wait_ended")
            if latest["state"] != "waiting":
                raise ProviderError("wait_already_released")
            buffered = decode(latest["message_ids"])
            # 撤回的材料不再提供；等待指令本身撤回则由 active 关闭整轮。
            buffered = [mid for mid in buffered if not revoked(conn, mid)]
            ids = list(dict.fromkeys([*buffered, *(m["id"] for m in current)]))
            if len(ids) > 1000:
                close(conn, latest, "wait_capacity_exceeded")
                raise ProviderError("wait_capacity_exceeded")
            mode = control["action"]
            if mode in ("resume", "replace"):
                if mode == "resume":
                    for mid in buffered:
                        if mid in {m["id"] for m in current}:
                            continue
                        conn.execute(
                            "INSERT INTO reply_input_links(message_id,task_id,kind,in_context,created_at,source_mid) "
                            "VALUES(%s,%s,'deferred',1,%s,%s) "
                            "ON CONFLICT(user_id,message_id) DO UPDATE SET task_id=excluded.task_id,"
                            "kind='deferred',in_context=1,source_mid=excluded.source_mid",
                            (mid, task["id"], worker.clock(), latest["source_mid"]),
                        )
                conn.execute(
                    "UPDATE reply_input_waits SET state='releasing',message_ids=%s,release_task_id=%s,"
                    "release_revision=%s,control=%s WHERE session_id=%s",
                    (
                        json.dumps(buffered),
                        task["id"],
                        revision(task),
                        json.dumps(
                            {
                                "action": control["action"],
                                "message_id": control["message_id"],
                                "quote_sha256": hashlib.sha256(control["quote"].encode()).hexdigest(),
                                "usage": usage,
                                "model_rounds": rounds,
                            }
                        ),
                        latest["session_id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE reply_input_waits SET message_ids=%s,state=%s,closed_reason=%s WHERE session_id=%s",
                    (
                        json.dumps(ids),
                        "closed" if mode == "cancel" else "waiting",
                        "canceled" if mode == "cancel" else "",
                        latest["session_id"],
                    ),
                )
                conn.execute(
                    "UPDATE tasks SET state='skipped',reason=%s,completed_at=%s,updated_at=%s,"
                    "usage_json=%s,trace_json=%s WHERE id=%s AND lease_owner=%s AND state='generating'",
                    (
                        "reply_wait_canceled" if mode == "cancel" else "reply_wait_collecting",
                        worker.clock(),
                        worker.clock(),
                        json.dumps(usage),
                        json.dumps(
                            {
                                "wait_session": row["session_id"],
                                "wait_control": mode,
                                "wait_model_rounds": rounds,
                                "model_rounds": rounds,
                            }
                        ),
                        task["id"],
                        worker.owner,
                    ),
                )
            return mode not in ("resume", "replace")

    result = await database_call(finish)
    task["_wait_usage"], task["_wait_rounds"] = usage, rounds
    return result
