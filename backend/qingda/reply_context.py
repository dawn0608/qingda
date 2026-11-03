"""聊天记录中的模型输入只读投影；不调用模型、补取媒体或使用当前提示词。"""

import base64
import hashlib
import json
import os
import stat
import struct

from .chat_records import SELECT, SOURCE, conversation, decoded
from .context_capture import VERSION, checksum, safe


class ContextNotFound(Exception):
    pass


def source(conn, aid, cid, mid):
    try:
        conv = conversation(conn, aid, cid)
    except ValueError:
        raise ContextNotFound() from None
    if conn.execute(
        "SELECT 1 FROM chat_history_deletions WHERE account_id=%s AND conversation_id=%s AND state<>'complete'",
        (aid, cid),
    ).fetchone():
        raise ContextNotFound()
    message = conn.execute(
        SELECT + SOURCE + " AND m.id=%s AND m.source='auto' AND m.direction='outbound'",
        (conv["wxid"], conv["peer_id"], aid, cid, mid),
    ).fetchone()
    if not message:
        raise ContextNotFound()
    task = (
        conn.execute(
            "SELECT * FROM tasks WHERE id=%s AND account_id=%s AND conversation_id=%s AND kind='auto' AND state='accepted'",
            (message["outbound_task_id"], aid, cid),
        ).fetchone()
        if message["outbound_task_id"]
        else None
    )
    return message, task


def sections(request):
    system, data, images = [], {}, []
    for message in request.get("messages", []):
        body = message.get("content")
        if message.get("role") == "system":
            system.append(body)
        if message.get("role") != "user":
            continue
        parts = body if isinstance(body, list) else [{"type": "text", "text": body}]
        for part in parts:
            if part.get("type") == "image_reference":
                images.append(part)
            elif part.get("type") == "text":
                try:
                    value = json.loads(part.get("text", ""))
                    if isinstance(value, dict) and any(
                        key in value for key in ("current_messages", "conversation_history", "wait_source")
                    ):
                        data = value
                except (TypeError, ValueError):
                    pass
    return {"system": system, "data": data, "images": images}


def records(conn, task):
    trace = decoded(task["trace_json"])
    manifest = trace.get("context_capture", {})
    events, blocks = {}, {}
    if manifest.get("version") != VERSION:
        return manifest, events, blocks
    for row in conn.execute(
        "SELECT phase,details FROM reply_execution_events WHERE task_id=%s AND (phase='context_blob' OR (input_revision=%s AND phase IN ('context_request','context_response','context_tool'))) ORDER BY id",
        (task["id"], manifest["input_revision"]),
    ):
        value = decoded(row["details"])
        if row["phase"] == "context_blob":
            if checksum(value.get("value")) == value.get("sha256"):
                blocks[value["sha256"]] = value["value"]
        elif value.get("run_id") in manifest.get("runs", []):
            events.setdefault(value.get("call_id"), {})[row["phase"].removeprefix("context_")] = value
    return manifest, events, blocks


def request_data(value, blocks):
    if not value:
        return None
    try:
        result = {**value["parameters"], "messages": [blocks[ref] for ref in value["message_refs"]]}
        if "tools_ref" in value:
            result["tools"] = blocks[value["tools_ref"]]
        return result
    except KeyError:
        return None


def legacy(manager, conn, task):
    result = {
        "status": "unavailable",
        "missing": ["旧回复未记录完整上下文"],
        "input": None,
        "calls": [],
        "cursor": None,
        "has_more": False,
    }
    if not task:
        return result
    row = conn.execute(
        "SELECT manifest FROM reply_context_snapshots WHERE task_id=%s", (task["id"],)
    ).fetchone()
    if row:
        from .context_snapshot import hydrate
        from .providers import ProviderError

        try:
            context = hydrate(manager, conn, task, decoded(row["manifest"]))
            result["input"] = {"system": [], "data": {"legacy_context": safe(context)}, "images": []}
            result["status"] = "partial"
        except (ProviderError, ValueError, KeyError, TypeError):
            result["missing"].append("历史来源已变化，无法核验原文")
    for index, value in enumerate(decoded(task["trace_json"]).get("tool_calls", [])):
        # 旧记录中的历史正文可能是引用清单；不能把清单冒充原文。
        result["calls"].append(
            {
                "call_id": f"legacy-{index}",
                "kind": "tool",
                "stage": "reply",
                "legacy": True,
                "tool": safe(value),
            }
        )
    if result["calls"]:
        result["status"] = "partial"
    return result


def history_context(manager, aid, cid, mid, call_id=None, cursor=None, limit=50):
    with manager.store.connect(snapshot=True) as conn:
        _, task = source(conn, aid, cid, mid)
        manifest, events, blocks = records(conn, task) if task else ({}, {}, {})
        if manifest.get("version") != VERSION:
            if call_id or cursor:
                raise ContextNotFound()
            return legacy(manager, conn, task)
        expected = manifest.get("expected", [])
        missing = ["部分生成记录保存失败"] if manifest.get("missing") else []
        calls, initial = [], None
        for item in expected:
            event = events.get(item["call_id"], {})
            request = request_data(event.get("request"), blocks)
            if item["kind"] == "model":
                if not request or "response" not in event:
                    missing.append("模型请求或响应记录缺失")
                if initial is None and item.get("stage") == "reply" and request:
                    initial = {**sections(request), "size": event["request"].get("size", {})}
                summary = {
                    **item,
                    "timestamp": event.get("request", {}).get("timestamp"),
                    "model": (request or {}).get("model"),
                    **{key: event.get("response", {}).get(key) for key in ("status", "duration_ms")},
                    "available": bool(request),
                }
            else:
                tool = event.get("tool")
                if not tool:
                    missing.append("工具执行记录缺失")
                summary = {
                    **item,
                    "timestamp": (tool or {}).get("timestamp"),
                    "name": (tool or {}).get("name"),
                    "available": bool(tool),
                }
            calls.append(summary)
        if not expected:
            missing.append("生成链路记录缺失")
        if initial is None:
            missing.append("首次回复输入缺失")
        result = {
            "status": "partial" if missing else "complete",
            "missing": list(dict.fromkeys(missing)),
            "input": initial,
            "calls": [],
            "cursor": None,
            "has_more": False,
            "omitted": ["凭据", "媒体编码字节", "思考正文"],
        }
        if call_id:
            item = next((item for item in expected if item["call_id"] == call_id), None)
            if not item:
                raise ContextNotFound()
            event = events.get(call_id, {})
            return {
                "call_id": call_id,
                "kind": item["kind"],
                "request": request_data(event.get("request"), blocks),
                "response": event.get("response"),
                "tool": event.get("tool"),
                "omitted": result["omitted"],
            }
        start = 0
        identity = checksum([aid, cid, mid, manifest])
        if cursor:
            try:
                bound, start = json.loads(base64.urlsafe_b64decode(cursor))
                if bound != identity or type(start) is not int or not 0 <= start <= len(calls):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError):
                raise ContextNotFound() from None
        result["calls"] = calls[start : start + limit]
        if start + limit < len(calls):
            result.update(
                has_more=True,
                cursor=base64.urlsafe_b64encode(json.dumps([identity, start + limit]).encode()).decode(),
            )
        return result


def context_image(manager, aid, cid, mid, sha):
    with manager.store.connect(snapshot=True) as conn:
        _, task = source(conn, aid, cid, mid)
        if not task:
            raise ContextNotFound()
        manifest, events, blocks = records(conn, task)
        frame = None
        for item in manifest.get("expected", []):
            request = request_data(events.get(item["call_id"], {}).get("request"), blocks)
            if request:
                frame = next(
                    (image for image in sections(request)["images"] if image.get("sha256") == sha), frame
                )
        if not frame or not frame.get("message_id") or not frame.get("width") or not frame.get("height"):
            raise ContextNotFound()
        # 快照只能引用本账号、会话中仍存在的原消息；不接受客户端文件路径。
        if not conn.execute(
            "SELECT 1 FROM messages WHERE id=%s AND account_id=%s AND conversation_id=%s",
            (frame["message_id"], aid, cid),
        ).fetchone():
            raise ContextNotFound()
    try:
        path = manager.media.path(frame)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 32 <= info.st_size <= 8 * 1024 * 1024:
                raise ContextNotFound()
            body = stream.read(8 * 1024 * 1024 + 1)
        if (
            len(body) != info.st_size
            or hashlib.sha256(body).hexdigest() != sha
            or body[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            or struct.unpack(">II", body[16:24]) != (frame["width"], frame["height"])
        ):
            raise ContextNotFound()
        return body
    except (OSError, ValueError, AttributeError):
        raise ContextNotFound() from None
