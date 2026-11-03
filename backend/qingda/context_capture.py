"""实际模型业务输入的旁路记录；失败只形成缺口，不阻塞回复或重放调用。"""

import asyncio
import base64
import contextvars
import hashlib
import json
import logging
import struct
import time
import uuid
from collections import deque

from .tenancy import current_user, user_scope

CURRENT = contextvars.ContextVar("reply_context_capture", default=None)
STAGE = contextvars.ContextVar("reply_context_stage", default="reply")
VERSION = 1
MAX_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 256
logger = logging.getLogger(__name__)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def checksum(value):
    # JSONB 会重排对象键；顺序仅对消息数组有意义，校验必须使用规范键序。
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def safe(value):
    """仅排除协议中的凭据和思考字段；聊天正文字符串保留原样。"""
    if isinstance(value, list):
        return [safe(item) for item in value]
    if isinstance(value, dict):
        return {
            key: safe(item)
            for key, item in value.items()
            if key.lower()
            not in {
                "reasoning_content",
                "reasoning",
                "authorization",
                "api_key",
                "model_key",
                "wechat_token",
                "aeskey",
            }
        }
    return value


def request_view(payload):
    result = safe(payload)
    for message in result.get("messages", []):
        parts = message.get("content")
        if not isinstance(parts, list):
            continue
        origin = {}
        for index, part in enumerate(parts):
            if part.get("type") == "text":
                try:
                    value = json.loads(part["text"])
                    if isinstance(value, dict) and "image_location" in value:
                        origin = value
                except (ValueError, KeyError, TypeError):
                    pass
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                image = {"type": "image_reference", **origin}
                if isinstance(url, str) and url.startswith("data:image/png;base64,"):
                    data = base64.b64decode(url.split(",", 1)[1], validate=True)
                    image.update(sha256=hashlib.sha256(data).hexdigest())
                    if data[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" and len(data) >= 24:
                        image["width"], image["height"] = struct.unpack(">II", data[16:24])
                else:
                    image["unavailable"] = True
                parts[index] = image
                origin = {}
    return result


class Writer:
    """每个 Gateway 单写者；缓冲同时限制字节和条数，数据库等待发生在后台。"""

    def __init__(self, store):
        self.store = store
        self.pending = deque()
        self.bytes = 0
        self.runner = None

    def put(self, capture, kind, details):
        try:
            body = encoded(details)
            size = len(body.encode())
            if self.bytes + size > MAX_BYTES or len(self.pending) >= MAX_EVENTS:
                capture.missing = True
                return
            self.pending.append((capture, kind, body, size))
            self.bytes += size
            if self.runner is None or self.runner.done():
                self.runner = asyncio.create_task(self.consume())
        except Exception:
            capture.missing = True
            logger.warning("reply_context_enqueue_failed")

    async def consume(self):
        while self.pending:
            capture, kind, body, size = self.pending.popleft()
            try:
                await asyncio.to_thread(self.write, (capture, kind, body))
            except Exception:
                capture.missing = True
                logger.warning("reply_context_write_failed")
            finally:
                self.bytes -= size

    def write(self, envelope):
        capture, kind, body = envelope
        with user_scope(capture.user_id), self.store.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='1000ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            # 观察写者仅与同任务的观察写者互斥，不等待或阻塞业务任务更新。
            lock_key = int.from_bytes(
                hashlib.sha256(f"context:{capture.user_id}:{capture.task_id}".encode()).digest()[:8],
                "big",
                signed=True,
            )
            if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock_key,)).fetchone()[0]:
                capture.missing = True
                return
            task = conn.execute(
                "SELECT * FROM tasks WHERE id=%s FOR KEY SHARE", (capture.task_id,)
            ).fetchone()
            if not task:
                return
            trace = json.loads(task["trace_json"])
            bound = trace.get("context_capture", {})
            if not (
                (task["state"] == "generating" and task["lease_owner"] == capture.owner)
                or capture.run_id in bound.get("runs", [])
            ):
                return
            details = json.loads(body)
            if kind == "request":
                payload = details.pop("request")
                refs = []
                for value in payload.pop("messages", []):
                    refs.append(self.block(conn, capture, value))
                details["message_refs"] = refs
                if "tools" in payload:
                    details["tools_ref"] = self.block(conn, capture, payload.pop("tools"))
                details["parameters"] = payload
            self.insert(conn, capture, "context_" + kind, details)

    @staticmethod
    def insert(conn, capture, phase, details):
        conn.execute(
            "INSERT INTO reply_execution_events(task_id,input_revision,phase,details,created_at) VALUES(%s,%s,%s,%s,%s)",
            (capture.task_id, capture.revision, phase, encoded(details), time.time()),
        )

    def block(self, conn, capture, value):
        sha = checksum(value)
        if not conn.execute(
            "SELECT 1 FROM reply_execution_events WHERE task_id=%s AND phase='context_blob' AND details->>'sha256'=%s LIMIT 1",
            (capture.task_id, sha),
        ).fetchone():
            self.insert(conn, capture, "context_blob", {"sha256": sha, "value": value})
        return sha

    async def drain(self):
        if self.runner:
            await asyncio.shield(self.runner)


class Capture:
    def __init__(self, manager, task):
        from .reply_media import revision

        self.task_id = task["id"]
        self.user_id = task.get("user_id") or current_user.get()
        self.owner = task.get("_owner")
        self.revision = revision(task)
        self.run_id = uuid.uuid4().hex
        self.runs = [self.run_id]
        self.expected = []
        self.tool_ids = set()
        self.missing = False
        self.writer = None

    def manifest(self):
        return {
            "version": VERSION,
            "input_revision": self.revision,
            "runs": list(self.runs),
            "expected": list(self.expected),
            "missing": self.missing,
        }

    def restore(self, value):
        if value and value.get("version") == VERSION and value.get("input_revision") == self.revision:
            self.runs = list(dict.fromkeys(value.get("runs", []) + [self.run_id]))
            self.expected = list(value.get("expected", [])) + self.expected
            self.tool_ids = {item["native_id"] for item in self.expected if "native_id" in item}
            self.missing = self.missing or bool(value.get("missing"))
        else:
            self.missing = True

    def emit(self, kind, details):
        if self.writer:
            self.writer.put(self, kind, {"run_id": self.run_id, **details})
        else:
            self.missing = True

    def request(self, payload, size, writer):
        call_id = uuid.uuid4().hex
        stage = STAGE.get()
        self.writer = writer
        self.expected.append({"call_id": call_id, "kind": "model", "stage": stage})
        try:
            self.emit(
                "request",
                {
                    "call_id": call_id,
                    "stage": stage,
                    "timestamp": time.time(),
                    "request": request_view(payload),
                    "size": size,
                },
            )
        except Exception:
            self.missing = True
            logger.warning("reply_context_snapshot_failed")
        return call_id

    def response(self, call_id, response, status, duration_ms):
        try:
            self.emit(
                "response",
                {
                    "call_id": call_id,
                    "response": safe(
                        {key: response[key] for key in ("id", "model", "choices", "usage") if key in response}
                    ),
                    "status": status,
                    "duration_ms": duration_ms,
                },
            )
        except Exception:
            self.missing = True
            logger.warning("reply_context_response_failed")

    def tool(self, value, stage="reply"):
        native_id = value["call_id"]
        if native_id in self.tool_ids:
            return
        self.tool_ids.add(native_id)
        call_id = uuid.uuid4().hex
        self.expected.append({"call_id": call_id, "kind": "tool", "stage": stage, "native_id": native_id})
        self.emit(
            "tool",
            {
                "call_id": call_id,
                "stage": stage,
                "timestamp": time.time(),
                **safe({key: item for key, item in value.items() if key != "call_id"}),
                "native_id": native_id,
            },
        )
