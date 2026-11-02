"""输入修订的持久上下文清单；正文按消息引用恢复，不另存历史副本。"""

import hashlib
import json
import time
import uuid

from .context_budget import VERSION, dumps
from .providers import ProviderError

BODY = ("content", "original_content", "normalized_content")


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def pack(value):
    if isinstance(value, list):
        return [pack(v) for v in value]
    if not isinstance(value, dict):
        return value
    body = {key: value[key] for key in BODY if key in value}
    if (
        body
        and value.get("message_id") is not None
        and not value.get("unavailable")
        and not value.get("material_catalogue")
    ):
        result = {key: pack(v) for key, v in value.items() if key not in BODY}
        result["_body_fields"] = list(body)
        result["_body_hash"] = digest(body)
        return result
    return {key: pack(v) for key, v in value.items()}


def identifiers(value):
    result = set()
    if isinstance(value, list):
        for item in value:
            result.update(identifiers(item))
    elif isinstance(value, dict):
        if "_body_fields" in value:
            result.add(value["message_id"])
        for item in value.values():
            result.update(identifiers(item))
    return result


def hydrate(manager, conn, task, manifest):
    from .conversation_history import answer_item, rows_by_ids
    from .private_context import item, normalize_emotions

    ids = identifiers(manifest)
    sources = {
        mid: item(manager, row, conn)
        for mid, row in rows_by_ids(manager, conn, task, {i for i in ids if isinstance(i, int)}).items()
    }
    tids = [i for i in ids if isinstance(i, str)]
    if tids:
        for row in conn.execute(
            "SELECT t.*,m.sender AS reply_to_sender,d.close_dialogue,d.reason AS decision_reason FROM tasks t "
            "LEFT JOIN messages m ON m.id=t.message_id LEFT JOIN reply_decisions d ON d.task_id=t.id "
            "LEFT JOIN message_views v ON v.key='t:'||t.id WHERE t.id=ANY(%s) "
            "AND t.account_id=%s AND t.conversation_id=%s AND t.state='accepted' "
            "AND coalesce(v.visible,1)=1 AND coalesce(v.revoked,0)=0",
            (tids, task["account_id"], task["conversation_id"]),
        ):
            sources[row["id"]] = (
                answer_item(row)
                if row["kind"] == "auto"
                else {
                    "content": row["content"],
                    "original_content": row["content"],
                }
            )

    def restore(value):
        if isinstance(value, list):
            return [restore(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {k: restore(v) for k, v in value.items() if k not in ("_body_fields", "_body_hash")}
        if "_body_fields" in value:
            source = sources.get(value["message_id"])
            if source is None:
                raise ProviderError("history_source_changed")
            body = {k: source.get(k, source.get("content", "")) for k in value["_body_fields"]}
            # 显式引用的人工发送使用旧统一表情释义，普通 owner 历史保留原文。
            if digest(body) != value["_body_hash"] and value.get("speaker_type") == "owner":
                body["content"] = normalize_emotions(body["content"])
            if digest(body) != value["_body_hash"]:
                raise ProviderError("history_source_changed")
            result.update(body)
        return result

    return restore(manifest)


def freeze(manager, task, context):
    from .callbacks import revocation_revision
    from .reply_media import VERSION as IMAGE_POLICY
    from .reply_media import revision

    with manager.store.transaction() as conn:
        owned = conn.execute(
            "SELECT state,lease_owner FROM tasks WHERE id=%s FOR UPDATE", (task["id"],)
        ).fetchone()
        if not owned or owned["state"] != "generating" or owned["lease_owner"] != task.get("_owner"):
            raise ProviderError("task_canceled")
        account = manager.account(task["account_id"], conn)
        key = digest(
            [
                VERSION,
                IMAGE_POLICY,
                revision(task),
                task["account_epoch"],
                task["conversation_epoch"],
                task["config_version"],
                account["wxid"],
                account["current_app_id"],
                revocation_revision(conn, task["conversation_id"]),
            ]
        )
        row = conn.execute("SELECT * FROM reply_context_snapshots WHERE task_id=%s", (task["id"],)).fetchone()
        if row and row["fingerprint"] == key:
            try:
                restored = hydrate(manager, conn, task, json.loads(row["manifest"]))
                for read in json.loads(row["recall"]):
                    if read.get("result"):
                        hydrate(manager, conn, task, read["result"])
                return restored
            except ProviderError:
                # 来源发生变化时重建清单，旧检查点的上下文哈希不再匹配。
                pass
        from .reply_queue import current_start

        context[current_start(context)]["_context_snapshot_id"] = uuid.uuid4().hex
        manifest = pack(context)
        conn.execute(
            "INSERT INTO reply_context_snapshots(task_id,fingerprint,manifest,updated_at) VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,task_id) DO UPDATE "
            "SET fingerprint=excluded.fingerprint,manifest=excluded.manifest,recall='[]',updated_at=excluded.updated_at",
            (task["id"], key, dumps(manifest), time.time()),
        )
        return context


def check(manager, conn, task):
    if not task.get("_context_frozen"):
        return ""
    row = conn.execute(
        "SELECT manifest,recall FROM reply_context_snapshots WHERE task_id=%s", (task["id"],)
    ).fetchone()
    if row:
        try:
            hydrate(manager, conn, task, json.loads(row["manifest"]))
            for read in json.loads(row["recall"]):
                if read.get("result"):
                    hydrate(manager, conn, task, read["result"])
        except ProviderError:
            return "history_source_changed"
    return ""
