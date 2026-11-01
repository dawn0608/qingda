"""会话模型视图：成员归属、短期完整轮次、显式引用与素材状态。"""

import json
import re
from functools import lru_cache

from .config import ROOT
from .emoji_policy import exclusion, is_image
from .emotion_catalog import compile_catalog
from .providers import ProviderError


@lru_cache(maxsize=1)
def emotions():
    runtime = ROOT / "backend/wechat_manager/native_emotions.json"
    if runtime.is_file():
        mapping = json.loads(runtime.read_text(encoding="utf-8"))
        if (
            not isinstance(mapping, dict)
            or not mapping
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items())
        ):
            raise ProviderError("invalid_native_emotions")
        return mapping
    if (ROOT / "release.json").is_file():
        raise ProviderError("missing_native_emotions")
    # 源码开发环境直接使用权威清单；发布环境必须携带编译后的可读运行资源。
    return compile_catalog(ROOT)


def normalize_emotions(text):
    mapping = emotions()
    pattern = "|".join(re.escape(key) for key in sorted(mapping, key=len, reverse=True))
    return re.sub(pattern, lambda match: mapping[match[0]], text)


def emotion_tokens():
    return "、".join(sorted(set(emotions().values())))


def input_allowed(manager, row, conn=None):
    media = getattr(manager, "media", None)
    if exclusion(row):
        return False
    return not is_image(row) or not media or media.image_allowed(row, conn)


def answer_allowed(manager, conn, answer):
    from .reply_decisions import handled_ids

    for mid in handled_ids(conn, answer):
        original = conn.execute("SELECT * FROM messages WHERE id=%s", (mid,)).fetchone()
        if original and not input_allowed(manager, original, conn):
            return False
    return True


def item(manager, row, conn=None):
    media = getattr(manager, "media", None)
    result = (
        media.context_item(row, conn)
        if media
        else {
            "role": "assistant" if row["direction"] == "outbound" else "user",
            "content": row["text"],
        }
    )
    result.update(
        message_id=row["id"],
        sender=row.get("sender"),
        timestamp=row.get("created_at"),
        received_at=row.get("received_at"),
    )
    result["speaker_type"] = "owner" if row["direction"] == "outbound" else "member"
    if result["speaker_type"] == "owner":
        result["role"] = "user"
    metadata = json.loads(row.get("metadata", "{}"))
    result["wakeup_reason"] = metadata.get("wakeup_reason")
    original_content = result["content"]
    result["content"] = normalize_emotions(original_content)
    if result["content"] != original_content:
        result["original_content"] = original_content
    kind = json.loads(row.get("metadata", "{}")).get("message_kind", "text")
    result["message_kind"] = kind
    if kind in ("image", "voice"):
        record = media.record(row["id"], conn) if media else None
        if kind == "image":
            state = record["state"] if record else "not_prepared"
            reason = record["reason"] if record else ""
            trace = json.loads(record["trace_json"]) if record else {}
            status = (
                "low_resolution"
                if state == "ready" and trace.get("image_type") == 3
                else "ready"
                if state == "ready"
                else "preparing"
                if state == "downloading"
                else "not_prepared"
                if not record
                else "decode_failed"
                if reason
                in (
                    "invalid_image",
                    "invalid_media",
                    "unsupported_image_format",
                    "media_decode_failed",
                    "image_too_large",
                )
                else "resource_expired"
                if reason == "media_resource_expired"
                else "download_failed"
            )
            result.update(media_status=status, media_reason=reason, image_type=trace.get("image_type"))
            if state != "ready":
                result["content"] = (
                    "[图片未附带]"
                    if status == "not_prepared"
                    else "[图片准备中]"
                    if status == "preparing"
                    else "[图片未能读取]"
                )
                result.pop("images", None)
            return result
        if not record or record["state"] != "ready":
            result.update(media_status="unavailable", message_kind=kind)
            result["content"] = {
                "image": "[图片未能读取]",
                "voice": "[语音未能转写]",
            }[kind]
            result.pop("images", None)
    return result


def reference(manager, conn, row):
    reference_id = json.loads(row["metadata"]).get("reference_id")
    if not reference_id:
        return None
    from .reply_references import assistant_reply

    answer = assistant_reply(
        conn, row["account_id"], row["conversation_id"], reference_id, row["received_at"]
    )
    if answer:
        if not answer_allowed(manager, conn, dict(answer)):
            return None
        return {
            "message_id": answer["id"],
            "content": normalize_emotions(answer["content"]),
            "role": "assistant",
            "speaker_type": "assistant",
            "timestamp": answer["completed_at"],
        }
    from .reply_media import original, visible

    target = original(conn, row)
    if target and visible(conn, row, target["id"]):
        if not input_allowed(manager, target, conn):
            return None
        return item(manager, dict(target), conn)
    sent = conn.execute(
        "SELECT t.id,t.content,t.completed_at,t.kind FROM tasks t LEFT JOIN message_views v ON v.key='t:'||t.id WHERE t.account_id=%s AND t.conversation_id=%s AND t.kind='manual' AND t.state='accepted' AND t.completed_at<=%s AND CAST((t.response_json #>> '{data,newMsgId}') AS TEXT)=%s AND coalesce(v.visible,1)=1 AND coalesce(v.revoked,0)=0 LIMIT 1",
        (row["account_id"], row["conversation_id"], row["received_at"], reference_id),
    ).fetchone()
    if sent:
        return {
            "message_id": sent["id"],
            "content": normalize_emotions(sent["content"]),
            "role": "assistant" if sent["kind"] == "auto" else "user",
            "speaker_type": "assistant" if sent["kind"] == "auto" else "owner",
            "timestamp": sent["completed_at"],
        }
    return {"reference_id": reference_id, "content": "引用内容不可取得或已撤回", "unavailable": True}


def build(manager, task, batch):
    from .dialogue_windows import inputs

    with manager.store.connect() as conn:
        targets = [row for row in inputs(conn, batch) if input_allowed(manager, row, conn)]
        if not targets:
            return []
        group = manager.conversation(task["conversation_id"], conn)["kind"] == "group"
        first = min(targets, key=lambda row: (row["received_at"], row["id"]))
        boundary = max(targets, key=lambda row: (row["received_at"], row["id"])) if group else first
        reply_target = first["sender"]
        from .conversation_history import build as history_view

        history, history_selection = history_view(manager, conn, task, targets, group)
        current = []
        referenced_images = set()
        for target in targets:
            value = {**item(manager, target, conn), "current": True}
            value.update(
                conversation_kind="group" if group else "private",
                reply_target=reply_target,
                context_scope="current",
                input_scope=(
                    "pending"
                    if target.get("_input_kind") == "backfill"
                    else "deferred"
                    if target.get("_input_kind") == "deferred"
                    else "current"
                ),
            )
            quote = reference(manager, conn, target)
            if quote:
                value["reference"] = {k: v for k, v in quote.items() if k != "images"}
                if quote.get("images") and quote.get("message_kind") != "image":
                    value["images"] = value.get("images", []) + quote["images"]
                    referenced_images.add(quote["message_id"])
            current.append(value)
        from .reply_media import attach

        referenced_images.update(attach(manager, conn, task, current, targets, group))
        from .reply_profiles import VERSION, context_profiles

        current[0]["participant_profiles"] = context_profiles(manager, conn, task, current)
        current[0]["profile_schema_version"] = VERSION
        current[0]["history_selection"] = history_selection
        current[0]["presence_suppressed"] = bool(
            conn.execute(
                "SELECT 1 FROM tasks t JOIN messages m ON m.id=t.message_id LEFT JOIN message_views v ON v.key='t:'||t.id WHERE t.account_id=%s AND t.conversation_id=%s AND m.sender=%s AND t.kind='auto' AND t.state='accepted' AND t.completed_at>=%s AND t.account_epoch=%s AND t.conversation_epoch=%s AND coalesce(v.revoked,0)=0 LIMIT 1",
                (
                    task["account_id"],
                    task["conversation_id"],
                    reply_target,
                    boundary["received_at"] - 120,
                    task["account_epoch"],
                    task["conversation_epoch"],
                ),
            ).fetchone()
        )
        if group:
            owner = manager.account(task["account_id"], conn)["wxid"]
            names = {}
            for value in history:
                member = value.get("sender")
                if value.get("context_scope") != "background" or value.get("speaker_type") != "member":
                    continue
                if member not in names:
                    cached = conn.execute(
                        "SELECT profile_json,group_name FROM reply_participant_profiles WHERE account_id=%s AND owner_wxid=%s AND conversation_id=%s AND member_id=%s",
                        (task["account_id"], owner, task["conversation_id"], member),
                    ).fetchone()
                    names[member] = (
                        (cached["group_name"] or json.loads(cached["profile_json"]).get("nickname"))
                        if cached
                        else None
                    )
                if names[member]:
                    value["speaker_name"] = names[member]
        # 历史仅提供文本来源；原图由本轮输入、真实引用或明确承接关系负责附带。
        allowed = 2
        for value in reversed(history):
            if value.get("images"):
                if value["message_id"] in referenced_images or allowed <= 0:
                    value.pop("images")
                    value["media_status"] = "historical_image_omitted"
                else:
                    allowed -= 1
        return history + current
