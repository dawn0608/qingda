"""入站素材的持久处理；只由通过自动回复检查的任务调用。"""

import asyncio
import hashlib
import io
import json
import re
import uuid
import zipfile
from contextlib import contextmanager

from PIL import Image, UnidentifiedImageError

from . import emoji_policy
from .database_async import database_call
from .media_decode import decode, prepare_voice
from .protocol import normalize
from .providers import ProviderError
from .speech import CONCURRENCY, FINGERPRINT, RESOURCE, AliyunSpeech, callback_voice, inspect_voice
from .store import string


def image_body(body):
    """表情 ZIP 仅接受一个受限大小的图像，不解压到文件系统。"""
    if body.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            files = [f for f in archive.infolist() if not f.is_dir()]
            if len(files) != 1 or files[0].file_size > 32 * 1024 * 1024:
                raise ValueError("invalid_emoji_archive")
            body = archive.read(files[0])
    return body


def image_frame_count(body):
    with Image.open(io.BytesIO(image_body(body))) as image:
        if image.format not in ("JPEG", "PNG", "GIF", "WEBP"):
            raise ValueError("unsupported_image_format")
        if image.width * image.height > 25_000_000:
            raise ValueError("image_too_large")
        return getattr(image, "n_frames", 1)


def animated_prefix(body):
    """只在解码器已找到第二帧时确认动画；截断、单帧和未知格式不推断。"""
    try:
        with Image.open(io.BytesIO(body)) as image:
            if image.format not in ("GIF", "PNG", "WEBP") or image.width * image.height > 25_000_000:
                return False
            image.seek(1)
            return image.tell() == 1
    except (OSError, ValueError, EOFError, SyntaxError, Image.DecompressionBombError):
        return False


def image_frames(body):
    """按真实图像内容解码；只允许静态图片，自定义表情不调用本入口。"""
    body = image_body(body)
    try:
        with Image.open(io.BytesIO(body)) as image:
            if image.format not in ("JPEG", "PNG", "GIF", "WEBP"):
                raise ValueError("unsupported_image_format")
            if image.width * image.height > 25_000_000:
                raise ValueError("image_too_large")
            count = getattr(image, "n_frames", 1)
            if count != 1:
                raise ProviderError(emoji_policy.ANIMATED)
            frames = []
            for index in (0,):
                image.seek(index)
                frame = image.convert("RGBA")
                frame.thumbnail((8192, 8192))
                while True:
                    output = io.BytesIO()
                    frame.save(output, format="PNG")
                    data = output.getvalue()
                    if len(data) <= 8 * 1024 * 1024:
                        break
                    frame.thumbnail((max(1, frame.width * 3 // 4), max(1, frame.height * 3 // 4)))
                digest = hashlib.sha256(data).hexdigest()
                if not any(f[0] == digest for f in frames):
                    frames.append((digest, data, frame.width, frame.height))
            return frames
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, EOFError) as exc:
        raise ValueError("invalid_image") from exc


class IncomingMedia:
    def __init__(self, manager, config, transcriber=None):
        self.manager, self.store, self.config = manager, manager.store, config
        self.directory = config.data_dir / "incoming-media"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.transcriber = transcriber or AliyunSpeech(manager)

    def raw_path(self, mid):
        return self.directory / f"{int(mid)}.bin"

    def path(self, reference):
        mid, sha = reference["message_id"], reference["sha256"]
        if not isinstance(mid, int) or mid < 1 or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ProviderError("invalid_media_reference")
        return self.directory / f"{mid}-{sha}.png"

    def record(self, mid, conn=None):
        if conn is not None:
            return conn.execute("SELECT * FROM incoming_media WHERE message_id=%s", (mid,)).fetchone()
        rows = self.store.query("SELECT * FROM incoming_media WHERE message_id=%s", (mid,))
        return rows[0] if rows else None

    def mark_image(self, mid, *, frame_count=None, reason="", frame_count_exact=True, task=None):
        with self.writing(task or {}) as conn:
            emoji_policy.mark(
                conn,
                mid,
                self.manager.clock(),
                frame_count=frame_count,
                reason=reason,
                frame_count_exact=frame_count_exact,
            )

    def image_allowed(self, message, conn=None):
        """自定义表情始终排除；旧图片只读已有原件判定，不下载历史素材。"""
        if emoji_policy.is_emoji(message):
            return False
        if not emoji_policy.is_image(message):
            return True
        record = self.record(message["id"], conn)
        if not record:
            return True
        trace = json.loads(record["trace_json"])
        count = trace.get("frame_count")
        if trace.get("animated") or (isinstance(count, int) and count > 1) or record["state"] == "skipped":
            return False
        if record["state"] != "ready":
            return True
        if trace.get("visual_policy") == emoji_policy.VERSION and count == 1:
            return not record["reason"]
        try:
            body = self.raw_path(message["id"]).read_bytes()
            return bool(
                record["sha256"]
                and len(body) <= 32 * 1024 * 1024
                and hashlib.sha256(body).hexdigest() == record["sha256"]
                and image_frame_count(body) == 1
            )
        except (
            OSError,
            ValueError,
            EOFError,
            RuntimeError,
            zipfile.BadZipFile,
            Image.DecompressionBombError,
        ):
            return False

    def frames(self, mid, conn=None):
        row = self.record(mid, conn)
        if not row or row["kind"] != "image" or not self.image_allowed({"id": mid, "msg_type": "3"}, conn):
            return []
        return json.loads(row["frames_json"]) if row and row["state"] == "ready" else []

    def context_item(self, row, conn=None):
        metadata = json.loads(row.get("metadata", "{}"))
        media = self.record(row["id"], conn)
        text = row["text"]
        if media and media["kind"] == "voice" and media["state"] == "ready" and media["transcript"]:
            text = "[语音转写] " + media["transcript"]
        item = {
            "role": "assistant" if row["direction"] == "outbound" else "user",
            "content": text or "[" + metadata.get("type_name", "非文本消息") + "]",
        }
        if row["direction"] == "inbound" or metadata.get("message_kind") == "image":
            frames = self.frames(row["id"], conn)
            if frames:
                item["images"] = frames
        return item

    def fit_context(self, context):
        from .reply_queue import fit_context

        return fit_context(context, self)

    @contextmanager
    def writing(self, task):
        with self.store.transaction() as conn:
            guard = task.get("_media_commit")
            if guard and (reason := guard(conn)):
                raise ProviderError(reason)
            yield conn

    async def prepare(self, task, gateway, check=None):
        if check and (reason := await database_call(check)):
            raise ProviderError(reason)
        if not task["message_id"]:
            return
        rows = await database_call(
            self.store.query,
            "SELECT * FROM messages WHERE id=%s AND account_id=%s AND conversation_id=%s",
            (task["message_id"], task["account_id"], task["conversation_id"]),
        )
        if not rows:
            raise ProviderError("message_missing")
        message = rows[0]
        kind = json.loads(message["metadata"]).get("message_kind")
        if kind == "video":
            raise ProviderError("video_disabled")
        if kind not in ("image", "voice"):
            return
        cached = await database_call(self.record, message["id"])
        cache_version = json.loads(cached["trace_json"]) if cached else {}
        usable_cache = cached and cached["state"] == "ready"
        if usable_cache and kind == "image" and not json.loads(cached["frames_json"]):
            usable_cache = False
        if usable_cache and kind == "image" and not await database_call(self.image_allowed, message):
            usable_cache = False
        if (
            kind == "voice"
            and isinstance(self.transcriber, AliyunSpeech)
            and cache_version.get("asr_fingerprint") != FINGERPRINT
        ):
            usable_cache = False
        if kind == "image" and (
            cache_version.get("animated")
            or (isinstance(cache_version.get("frame_count"), int) and cache_version["frame_count"] > 1)
        ):
            await database_call(
                self.mark_image,
                message["id"],
                frame_count=cache_version.get("frame_count", 2),
                reason=emoji_policy.ANIMATED,
                task=task,
            )
            return
        if usable_cache:
            try:
                for frame in await database_call(self.frames, message["id"]):
                    data = self.path(frame).read_bytes()
                    if hashlib.sha256(data).hexdigest() != frame["sha256"]:
                        raise ProviderError("media_cache_changed")
            except (OSError, ProviderError) as exc:

                def _database_prepare_122():
                    with self.writing(task) as conn:
                        conn.execute(
                            "UPDATE incoming_media SET state='failed',reason='media_cache_changed',updated_at=%s WHERE message_id=%s",
                            (self.manager.clock(), message["id"]),
                        )
                    return ("normal", None, locals())

                _database_result = await database_call(_database_prepare_122)
                raise ProviderError("media_cache_changed") from exc
            return
        if (
            kind == "voice"
            and isinstance(self.transcriber, AliyunSpeech)
            and cache_version.get("asr_attempt")
        ):
            raise ProviderError("asr_outcome_unknown")
        now = self.manager.clock()
        preparation = uuid.uuid4().hex

        def _database_prepare_130():
            with self.writing(task) as conn:
                conn.execute(
                    "INSERT INTO incoming_media(message_id,state,kind,updated_at,trace_json) VALUES(%s,'downloading',%s,%s,%s) ON CONFLICT(user_id,message_id) DO UPDATE SET state='downloading',reason='',updated_at=excluded.updated_at,trace_json=incoming_media.trace_json||excluded.trace_json",
                    (message["id"], kind, now, json.dumps({"preparation": preparation})),
                )
            return ("normal", None, locals())

        _database_result = await database_call(_database_prepare_130)
        try:
            event = await database_call(self.store.event, message["event_id"])
            normalized = normalize(event)
            # XML 始终来自原始回调，群聊发送人前缀由 normalize 去除。
            xml = string(event.get("Data", {}).get("Content")) or ""
            if normalized["kind"] == "group" and ":\n" in xml:
                xml = xml.split(":\n", 1)[1]
            metadata = normalized["metadata"]
            if kind == "voice" and hasattr(self.transcriber, "require_configured"):
                await database_call(self.transcriber.require_configured)
            raw_path = self.raw_path(message["id"])
            body = (
                await asyncio.to_thread(raw_path.read_bytes)
                if cached and cached["sha256"] and raw_path.is_file()
                else None
            )
            if (
                body is not None
                and cached
                and cached["sha256"]
                and hashlib.sha256(body).hexdigest() != cached["sha256"]
            ):
                raise ProviderError("media_cache_changed")
            if body is None:
                body = callback_voice(event) if kind == "voice" else None
                if body is None:
                    download = (
                        gateway.download_image(
                            event["Appid"],
                            xml,
                            message_id=message["id"],
                            check=check,
                            commit_guard=task.get("_media_commit"),
                        )
                        if kind == "image" and hasattr(gateway, "download_image")
                        else gateway.download_media(
                            event["Appid"],
                            kind,
                            xml,
                            event.get("Data", {}).get("MsgId"),
                            metadata.get("emoji_md5"),
                            message_id=message["id"],
                        )
                    )
                    execution = task.get("_execution")
                    body = (
                        await execution.measure("download", lambda: download, source_message_id=message["id"])
                        if execution
                        else await download
                    )
                if not body or len(body) > (3 if kind == "voice" else 32) * 1024 * 1024:
                    raise ProviderError("media_too_large_or_empty")
                if kind == "image" and await asyncio.to_thread(animated_prefix, image_body(body)):
                    await database_call(
                        self.mark_image,
                        message["id"],
                        frame_count=2,
                        reason=emoji_policy.ANIMATED,
                        frame_count_exact=False,
                        task=task,
                    )
                    return
                if check and (reason := await database_call(check)):
                    raise ProviderError(reason)
                temporary = raw_path.with_suffix("." + uuid.uuid4().hex + ".tmp")
                try:
                    await asyncio.to_thread(temporary.write_bytes, body)
                    temporary.replace(raw_path)
                finally:
                    temporary.unlink(missing_ok=True)

                def _database_prepare_177():
                    with self.writing(task) as conn:
                        conn.execute(
                            "UPDATE incoming_media SET sha256=%s WHERE message_id=%s",
                            (hashlib.sha256(body).hexdigest(), message["id"]),
                        )
                    return ("normal", None, locals())

                _database_result = await database_call(_database_prepare_177)
            if check and (reason := await database_call(check)):
                raise ProviderError(reason)
            if kind == "voice":

                def _database_prepare_185():
                    with self.writing(task) as conn:
                        conn.execute(
                            "UPDATE incoming_media SET sha256=%s WHERE message_id=%s",
                            (hashlib.sha256(body).hexdigest(), message["id"]),
                        )
                    return ("normal", None, locals())

                _database_result = await database_call(_database_prepare_185)
                execution = task.get("_execution")

                async def inspect_audio():
                    if isinstance(self.transcriber, AliyunSpeech):
                        return await prepare_voice(raw_path, self.directory, self.config)
                    kind, milliseconds = await inspect_voice(body, raw_path, self.config)
                    return body, kind, milliseconds

                body, voice_format, duration = (
                    await execution.slot(
                        "decode",
                        1,
                        lambda: execution.measure("decode", inspect_audio, source_message_id=message["id"]),
                    )
                    if execution
                    else await inspect_audio()
                )
                if check and (reason := await database_call(check)):
                    raise ProviderError(reason)

                def begin_transcription():
                    with self.writing(task) as conn:
                        row = conn.execute(
                            "SELECT trace_json FROM incoming_media WHERE message_id=%s FOR UPDATE",
                            (message["id"],),
                        ).fetchone()
                        trace = json.loads(row[0])
                        if trace.get("preparation") != preparation:
                            raise ProviderError("media_preparation_replaced")
                        if isinstance(self.transcriber, AliyunSpeech):
                            if trace.get("asr_attempt"):
                                raise ProviderError("asr_outcome_unknown")
                            trace.update(
                                asr_fingerprint=FINGERPRINT,
                                asr_attempt=preparation,
                                asr_started_at=self.manager.clock(),
                            )
                        conn.execute(
                            "UPDATE incoming_media SET state='transcribing',trace_json=%s WHERE message_id=%s",
                            (json.dumps(trace), message["id"]),
                        )

                async def transcribe():
                    await database_call(begin_transcription)
                    return await self.transcriber.transcribe(body, voice_format)

                result = (
                    await execution.slot(RESOURCE, CONCURRENCY, transcribe)
                    if execution
                    else await transcribe()
                )
                text = result.get("text")
                recognized_duration = result.get("duration_ms")
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or len(text) > 10000
                    or not isinstance(recognized_duration, int)
                    or isinstance(recognized_duration, bool)
                    or not 0 < recognized_duration <= 60000
                ):
                    raise ProviderError("invalid_asr_result")
                trace = {
                    "provider": result.get("provider", "injected"),
                    "engine": result.get("engine", "injected"),
                    "request_id": result.get("request_id"),
                    "duration_ms": recognized_duration,
                    "source_duration_ms": duration,
                    "usage": result.get("usage", {}),
                    "latency_ms": result.get("latency_ms"),
                    **{
                        key: result[key]
                        for key in ("service", "inference_options", "asr_fingerprint")
                        if key in result
                    },
                }

                def _database_prepare_216():
                    with self.writing(task) as conn:
                        conn.execute(
                            "UPDATE incoming_media SET state='ready',transcript=%s,trace_json=%s,updated_at=%s WHERE message_id=%s",
                            (text.strip(), json.dumps(trace), self.manager.clock(), message["id"]),
                        )
                    return ("normal", None, locals())

                _database_result = await database_call(_database_prepare_216)
                return
            if not body or len(body) > 32 * 1024 * 1024:
                raise ProviderError("media_too_large_or_empty")
            execution = task.get("_execution")

            def decode_call():
                return decode(raw_path, self.directory, message["id"], kind, self.config)

            decoded = (
                await execution.slot(
                    "decode",
                    1,
                    lambda: execution.measure("decode", decode_call, source_message_id=message["id"]),
                )
                if execution
                else await decode_call()
            )
            if check and (reason := await database_call(check)):
                raise ProviderError(reason)
            if kind == "image":
                await database_call(
                    self.mark_image, message["id"], frame_count=decoded["frame_count"], task=task
                )
            frames = decoded["frames"]

            if check and (reason := await database_call(check)):
                raise ProviderError(reason)

            def _database_prepare_235():
                with self.writing(task) as conn:
                    conn.execute(
                        "UPDATE incoming_media SET state='ready',sha256=%s,frames_json=%s,updated_at=%s,trace_json=trace_json||%s::jsonb WHERE message_id=%s",
                        (
                            hashlib.sha256(body).hexdigest(),
                            json.dumps(frames),
                            self.manager.clock(),
                            json.dumps(
                                {
                                    **{k: v for k, v in decoded.items() if k != "frames"},
                                    "image_type": getattr(
                                        body,
                                        "image_type",
                                        json.loads(cached["trace_json"]).get("image_type", 2)
                                        if cached
                                        else 2,
                                    ),
                                }
                            ),
                            message["id"],
                        ),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_prepare_235)
        except Exception as exc:
            reason = exc.reason if isinstance(exc, ProviderError) else "invalid_media"
            if reason in ("media_preparation_replaced", "asr_outcome_unknown"):
                # 已提交或其他准备者持有的请求不能重新识别、覆盖状态。
                raise ProviderError(reason) from None
            if check and (invalid := await database_call(check)):
                # 过时任务不得将共享素材标记为失败，也不得提交可用结果。
                raise ProviderError(invalid) from exc
            if emoji_policy.is_image(message) and reason == emoji_policy.ANIMATED:
                await database_call(
                    self.mark_image,
                    message["id"],
                    frame_count=2,
                    reason=reason,
                    frame_count_exact=False,
                    task=task,
                )
                return

            def _database_prepare_247():
                with self.writing(task) as conn:
                    conn.execute(
                        "UPDATE incoming_media SET state='failed',reason=%s,updated_at=%s WHERE message_id=%s",
                        (reason, self.manager.clock(), message["id"]),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_prepare_247)

            def _database_prepare_252():
                with self.writing(task) as conn:
                    conn.execute(
                        "UPDATE media_resources SET state='unavailable',reason=%s WHERE message_id=%s",
                        (reason, message["id"]),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_prepare_252)
            raise ProviderError(reason) from exc
        finally:
            # 取消只释放自己留下的准备状态；不能覆盖接管进程的新缓存。
            def release_preparation():
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE incoming_media SET state='failed',reason='media_preparation_interrupted',updated_at=%s WHERE message_id=%s AND state='downloading' AND trace_json->>'preparation'=%s",
                        (self.manager.clock(), message["id"], preparation),
                    )
                    conn.execute(
                        "UPDATE incoming_media SET state='failed',reason='asr_outcome_unknown',updated_at=%s WHERE message_id=%s AND state='transcribing' AND trace_json->>'asr_attempt'=%s",
                        (self.manager.clock(), message["id"], preparation),
                    )

            await database_call(release_preparation)
