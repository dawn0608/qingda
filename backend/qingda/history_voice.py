"""聊天记录语音播放：只读已有原件，按点击临时转换，不进入自动回复链路。"""

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from .chat_records import SELECT, SOURCE, conversation
from .database_async import database_call
from .media_access import history_media_access
from .media_decode import run_process
from .providers import ProviderError


def voice_source(manager, aid, cid, mid):
    with manager.store.connect(snapshot=True) as conn:
        try:
            conv = conversation(conn, aid, cid)
        except ValueError:
            return None
        row = conn.execute(
            SELECT + SOURCE + " AND m.id=%s AND m.msg_type='34'",
            (conv["wxid"], conv["peer_id"], aid, cid, mid),
        ).fetchone()
    if (
        not row
        or row["media_kind"] != "voice"
        or not re.fullmatch(r"[a-f0-9]{64}", row["media_sha256"] or "")
    ):
        return None
    try:
        fd = os.open(manager.media.raw_path(mid), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 3 * 1024 * 1024:
                return None
            body = source.read(3 * 1024 * 1024 + 1)
        if len(body) == info.st_size and hashlib.sha256(body).hexdigest() == row["media_sha256"]:
            return body
    except OSError:
        pass
    return None


async def voice_content(manager, config, aid, cid, mid, request):
    with history_media_access(manager.media.directory, cid) as acquired:
        if not acquired:
            raise ProviderError("voice_playback_busy")
        return await read_voice(manager, config, aid, cid, mid, request)


async def read_voice(manager, config, aid, cid, mid, request):
    body = await database_call(voice_source, manager, aid, cid, mid)
    if body is None:
        return None
    # 转码仅接触已校验副本；临时 WAV 不进入媒体记录、不保留在生产缓存中。
    with tempfile.TemporaryDirectory(prefix=f"playback-{mid}-", dir=manager.media.directory) as directory:
        source, output = Path(directory) / "source.bin", Path(directory) / "audio.wav"
        await database_call(source.write_bytes, body)

        async def convert():
            result = json.loads(
                await run_process(
                    sys.executable,
                    Path(__file__).with_name("media_decode.py"),
                    "playback",
                    source,
                    output,
                    config.ffmpeg_binary,
                    timeout=30,
                )
            )
            if result.get("error"):
                raise ProviderError(result["error"])
            return await database_call(output.read_bytes)

        async def disconnected():
            while not await request.is_disconnected():
                await asyncio.sleep(0.2)

        conversion, disconnect = asyncio.create_task(convert()), asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((conversion, disconnect), return_when=asyncio.FIRST_COMPLETED)
            result = await conversion if conversion in done else None
            return (
                result
                if result is not None and await database_call(voice_source, manager, aid, cid, mid)
                else None
            )
        finally:
            for task in (conversion, disconnect):
                task.cancel()
            await asyncio.gather(conversion, disconnect, return_exceptions=True)
