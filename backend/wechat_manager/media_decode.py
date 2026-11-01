"""本机媒体解码在可取消、资源有界的子进程中执行。"""

import asyncio
import hashlib
import io
import json
import os
import signal
import sys
import tempfile
import wave
from contextlib import contextmanager
from pathlib import Path


async def run_process(*args, timeout=30):
    process = await asyncio.create_subprocess_exec(
        *map(str, args),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            from .providers import ProviderError

            raise ProviderError("media_decode_failed")
        return output
    finally:
        if process.returncode is None:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()


def save_frame(image, directory, mid, timestamp):
    if image.width * image.height > 16_000_000:
        raise ValueError("media_frame_resolution_unsupported")
    frame = image.convert("RGB" if image.mode == "RGB" and "transparency" not in image.info else "RGBA")
    output = io.BytesIO()
    # 中间缓存以解码速度为先，不改变像素；接近容量上限时才用常规压缩重试。
    frame.save(output, format="PNG", compress_level=1)
    data = output.getvalue()
    if len(data) > 8 * 1024 * 1024:
        output = io.BytesIO()
        frame.save(output, format="PNG")
        data = output.getvalue()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("media_frame_too_large")
    sha = hashlib.sha256(data).hexdigest()
    (directory / f"{mid}-{sha}.png").write_bytes(data)
    return {
        "message_id": mid,
        "sha256": sha,
        "width": frame.width,
        "height": frame.height,
        "time_seconds": round(timestamp, 3),
    }


def decode_image(path, directory, mid):
    import zipfile

    from PIL import Image

    body = path.read_bytes()
    if len(body) > 32 * 1024 * 1024:
        raise ValueError("image_too_large")
    if body.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            files = [f for f in archive.infolist() if not f.is_dir()]
            if len(files) != 1 or files[0].file_size > 32 * 1024 * 1024:
                raise ValueError("invalid_emoji_archive")
            body = archive.read(files[0])
    with Image.open(io.BytesIO(body)) as image:
        count = getattr(image, "n_frames", 1)
        if (
            image.format not in ("JPEG", "PNG", "GIF", "WEBP")
            or image.width * image.height > 25_000_000
            or count > 500
        ):
            raise ValueError("invalid_image")
        if count != 1:
            raise ValueError("animated_emoji_disabled")
        return {"frames": [save_frame(image, directory, mid, 0)], "frame_count": 1, "duration_seconds": 0}


async def decode(path, directory, mid, kind, config):
    from .providers import ProviderError

    if kind == "emoji":
        raise ProviderError("custom_emoji_disabled")
    if kind != "image":
        raise ProviderError("video_disabled" if kind in ("video", "video_check") else "unsupported_media")
    with tempfile.TemporaryDirectory(prefix=f"decode-{mid}-", dir=directory) as temporary:
        output_dir = Path(temporary)
        try:
            result = json.loads(
                await run_process(
                    sys.executable,
                    __file__,
                    kind,
                    path,
                    output_dir,
                    mid,
                    timeout=45,
                )
            )
            if result.get("error"):
                raise ProviderError(result["error"])
            for file in output_dir.glob("*.png"):
                file.replace(directory / file.name)
            return result
        except ProviderError:
            raise
        except (ValueError, OSError, asyncio.TimeoutError) as exc:
            raise ProviderError("media_decode_failed") from exc


async def prepare_voice(path, directory, config):
    """原件只读；转换产物只存于受控临时目录，取消会杀死整个解码进程组。"""
    from .providers import ProviderError
    from .speech import inspect_voice

    body = await asyncio.to_thread(path.read_bytes)
    if not body or len(body) > 3 * 1024 * 1024:
        raise ProviderError("media_too_large_or_empty")
    _, source_duration = await inspect_voice(body, path, config)
    with tempfile.TemporaryDirectory(prefix=f"voice-{path.stem}-", dir=directory) as temporary:
        output = Path(temporary) / "audio.wav"
        try:
            result = json.loads(
                await run_process(
                    sys.executable,
                    __file__,
                    "voice",
                    path,
                    output,
                    config.ffmpeg_binary,
                    timeout=15,
                )
            )
            if result.get("error"):
                raise ProviderError(result["error"])
            converted = await asyncio.to_thread(output.read_bytes)
            duration = result["duration_ms"]
            if not 0 < duration <= 60000 or abs(duration - source_duration) > 100:
                raise ValueError("invalid_voice")
            return converted, "wav", duration
        except ProviderError:
            raise
        except (ValueError, KeyError, OSError, asyncio.TimeoutError) as exc:
            raise ProviderError("media_decode_failed") from exc


def decode_voice(path, output, ffmpeg):
    import subprocess

    body = path.read_bytes()
    if not body or len(body) > 3 * 1024 * 1024:
        raise ValueError("invalid_voice")
    if body.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        import pysilk

        pcm = io.BytesIO()
        pysilk.decode(io.BytesIO(body), pcm, 16000)
        with wave.open(str(output), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(pcm.getvalue())
    else:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-t",
                "60.1",
                "-y",
                str(output),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=12,
        )
    if output.stat().st_size > 3 * 1024 * 1024:
        raise ValueError("invalid_voice")
    with wave.open(str(output), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("invalid_voice")
        frames = audio.getnframes()
        if len(audio.readframes(frames)) != frames * 2:
            raise ValueError("invalid_voice")
        duration = round(frames / 16)
    if not 0 < duration <= 60000:
        raise ValueError("invalid_voice")
    return {"duration_ms": duration}


@contextmanager
def playback_slot(directory):
    """共享目录的两路内核文件锁；无排队，进程退出或崩溃即归还，不创建业务任务。"""
    import errno
    import stat

    for index in range(2):
        fd = os.open(
            directory / f".playback-slot-{index}",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("media_decode_failed")
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                continue
            raise
        except BaseException:
            os.close(fd)
            raise
        try:
            yield
        finally:
            os.close(fd)
        return
    raise ValueError("voice_playback_busy")


def playback_voice(path, output, ffmpeg):
    from types import SimpleNamespace

    from wechat_manager.speech import inspect_voice

    with playback_slot(path.parent.parent):
        body = path.read_bytes()
        _, duration = asyncio.run(inspect_voice(body, path, SimpleNamespace(ffmpeg_binary=ffmpeg)))
        result = decode_voice(path, output, ffmpeg)
        if abs(result["duration_ms"] - duration) > 100:
            raise ValueError("invalid_voice")
        return result


def main():
    if os.name != "nt":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    kind, path, directory, mid = sys.argv[1:5]
    if kind in ("voice", "playback"):
        decoder = playback_voice if kind == "playback" else decode_voice
        print(json.dumps(decoder(Path(path), Path(directory), mid)))
        return
    args = (Path(path), Path(directory), int(mid))
    if kind != "image":
        raise ValueError("unsupported_media")
    result = decode_image(*args)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        reason = str(error)
        allowed = {
            "image_too_large",
            "invalid_emoji_archive",
            "invalid_image",
            "animated_emoji_disabled",
            "media_frame_resolution_unsupported",
            "media_frame_too_large",
            "voice_playback_busy",
        }
        print(json.dumps({"error": reason if reason in allowed else "media_decode_failed"}))
