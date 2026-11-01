"""微信短语音校验与逐用户阿里百炼录音识别。"""

import asyncio
import base64
import hashlib
import io
import json
import re
import struct
import time
import wave

import httpx

from .database_async import database_call
from .providers import ProviderError

MODEL = "qwen-audio-3.1-asr-flash"
OPTIONS = {
    "format": "wav",
    "sample_rate": "16000",
    "keep_dialect": True,
    "speaker_diarization_enabled": False,
}
ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
POLICY = {
    "provider": "aliyun",
    "service": "recorded_http",
    "engine": MODEL,
    "inference_options": OPTIONS,
    "audio_processing": "wav-mono16k-s16-v1",
}
FINGERPRINT = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode()).hexdigest()
RESOURCE = "aliyun_asr"
CONCURRENCY = 2


def validate_key(value):
    key = value.strip()
    if key and (not re.fullmatch(r"sk-[A-Za-z0-9_-]{16,997}", key)):
        raise ValueError("阿里云百炼 API Key 格式不正确")
    return key


class AliyunSpeech:
    def __init__(self, manager, transport=None):
        self.manager, self.transport = manager, transport

    def require_configured(self):
        # Store 每次在当前服务端用户范围内读取；不在客户端实例缓存密钥。
        saved = self.manager.store.setting("asr_key")
        if not saved or not self.manager.vault:
            raise ProviderError("missing_asr_key")
        return self.manager.vault.decrypt(saved)

    async def transcribe(self, body, voice_format):
        from .reply_execution import AUDIT

        key = await database_call(self.require_configured)
        if voice_format != "wav" or not body or len(body) > 3 * 1024 * 1024:
            raise ProviderError("invalid_voice")
        try:
            with wave.open(io.BytesIO(body), "rb") as audio:
                if (
                    audio.getnchannels(),
                    audio.getsampwidth(),
                    audio.getframerate(),
                    audio.getcomptype(),
                ) != (1, 2, 16000, "NONE"):
                    raise ValueError("invalid_voice")
                frames = audio.getnframes()
                if len(audio.readframes(frames)) != frames * 2:
                    raise ValueError("invalid_voice")
                duration = round(frames / 16)
            if not 0 < duration <= 60000:
                raise ValueError("invalid_voice")
        except (ValueError, EOFError, wave.Error):
            raise ProviderError("invalid_voice") from None
        payload = {
            "model": MODEL,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": "data:audio/wav;base64," + base64.b64encode(body).decode("ascii")
                                },
                            }
                        ],
                    }
                ]
            },
            "parameters": OPTIONS,
        }
        started, status, code, request_id, usage = time.monotonic(), None, None, None, {}
        try:
            async with httpx.AsyncClient(transport=self.transport, follow_redirects=False) as client:
                response = await asyncio.wait_for(
                    client.post(
                        ENDPOINT,
                        json=payload,
                        headers={"Authorization": "Bearer " + key, "X-DashScope-SSE": "disable"},
                        timeout=httpx.Timeout(30, connect=5),
                    ),
                    30,
                )
            status = response.status_code
            try:
                result = response.json()
            except ValueError:
                result = None
            if isinstance(result, dict):
                rid, raw_code = result.get("request_id"), result.get("code")
                if isinstance(rid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", rid) and key not in rid:
                    request_id = rid
                if raw_code:
                    code = (
                        raw_code
                        if isinstance(raw_code, str)
                        and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", raw_code)
                        and key not in raw_code
                        else "provider_error"
                    )
            if status != 200:
                raise ProviderError("asr_http_" + str(status) + ("_" + code if code else ""))
            if not isinstance(result, dict):
                raise ProviderError("invalid_asr_result")
            if code:
                raise ProviderError("asr_code_" + code)
            if request_id is None:
                raise ProviderError("invalid_asr_result")
            output, returned_usage = result.get("output"), result.get("usage")
            if not isinstance(output, dict) or not isinstance(returned_usage, dict):
                raise ProviderError("invalid_asr_result")
            text, seconds = output.get("text"), returned_usage.get("duration")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 10000
                or type(seconds) is not int
                or not 0 < seconds <= 61
                or abs(seconds * 1000 - duration) > 1100
            ):
                raise ProviderError("invalid_asr_result")
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                value = returned_usage.get(field)
                if type(value) is not int or value < 0:
                    raise ProviderError("invalid_asr_result")
            if (
                returned_usage["total_tokens"]
                != returned_usage["input_tokens"] + returned_usage["output_tokens"]
            ):
                raise ProviderError("invalid_asr_result")
            sentence = output.get("sentence")
            if (isinstance(sentence, dict) and sentence.get("sentence_end") is False) or returned_usage[
                "output_tokens"
            ] >= 1024:
                raise ProviderError("asr_output_truncated")
            usage = {
                field: returned_usage[field]
                for field in ("duration", "input_tokens", "output_tokens", "total_tokens")
            }
            return {
                **POLICY,
                "asr_fingerprint": FINGERPRINT,
                "text": text.strip(),
                "duration_ms": duration,
                "request_id": request_id,
                "usage": usage,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except (httpx.RequestError, asyncio.TimeoutError):
            raise ProviderError("asr_outcome_unknown") from None
        except (ValueError, TypeError):
            raise ProviderError("invalid_asr_result") from None
        finally:
            key = ""
            if audit := AUDIT.get():
                await audit(
                    {
                        **POLICY,
                        "model": MODEL,
                        "requested_model": MODEL,
                        "attempt": 1,
                        "status": status,
                        "code": code,
                        "request_id": request_id,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "usage": usage,
                    }
                )


def callback_voice(event):
    """回调自带语音是历史原件；核验长度及 SILK 帧后复用，不依赖临时 URL。"""
    data = event.get("Data") or {}
    if not isinstance(data, dict) or str(data.get("MsgType")) != "34":
        return None
    blob = data.get("ImgBuf")
    if not isinstance(blob, dict):
        return None
    encoded, length = blob.get("buffer"), blob.get("iLen")
    if (
        not isinstance(encoded, str)
        or len(encoded) > 4 * 1024 * 1024
        or type(length) is not int
        or not 0 < length <= 3 * 1024 * 1024
    ):
        return None
    try:
        body = base64.b64decode(encoded, validate=True)
        if len(body) != length:
            return None
        silk_duration(body)
        return body
    except (ValueError, TypeError):
        return None


def silk_duration(body):
    header = b"#!SILK_V3"
    if body.startswith(b"\x02" + header):
        offset = len(header) + 1
    elif body.startswith(header):
        offset = len(header)
    else:
        raise ValueError("请选择有效的 SILK 语音")
    frames = 0
    while offset < len(body):
        if offset + 2 > len(body):
            raise ValueError("SILK 语音不完整")
        length = struct.unpack_from("<h", body, offset)[0]
        offset += 2
        if length == -1 and offset == len(body):
            break
        if length <= 0 or offset + length > len(body):
            raise ValueError("SILK 语音不完整")
        offset += length
        frames += 1
    duration = frames * 20
    if not 0 < duration <= 60000 or len(body) > 3 * 1024 * 1024:
        raise ValueError("语音最多 60 秒、3MB")
    return duration


async def inspect_voice(body, path, config):
    if body.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        return "silk", silk_duration(body)
    process = await asyncio.create_subprocess_exec(
        config.ffmpeg_binary,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-frames:a",
        "0",
        "-f",
        "null",
        "-",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), 15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise ProviderError("invalid_voice")
    info = stderr.decode("utf-8", errors="replace").split("Output #", 1)[0]
    if "Audio:" not in info or "Video:" in info:
        raise ProviderError("invalid_voice")
    formats = {
        "wav": "wav",
        "mp3": "mp3",
        "amr": "amr",
        "aac": "aac",
        "mov,mp4,m4a,3gp,3g2,mj2": "m4a",
        "ogg": "ogg-opus",
    }
    container = re.search(r"Input #0, (.+?), from ", info)
    format_name = container.group(1) if container else None
    if format_name not in formats or (format_name == "ogg" and "Audio: opus" not in info):
        raise ProviderError("unsupported_voice_format")
    length = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", info)
    if not length:
        raise ProviderError("invalid_voice")
    duration = round((int(length[1]) * 3600 + int(length[2]) * 60 + float(length[3])) * 1000)
    if not 0 < duration <= 60000:
        raise ProviderError("voice_too_long")
    return formats[format_name], duration
