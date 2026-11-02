"""服务器隔离：已有语音的可播放响应、权限、无业务副作用及跨进程解码容量。"""

import asyncio
import hashlib
import io
import subprocess
import sys
import wave

from contact_fixtures import setup_directory
from qingda.media_decode import playback_slot, run_process
from test_authentication import code_login
from test_chat_records import endpoint, incoming


def test_cached_voice_playback_independent_of_asr_and_scoped(tmp_path):
    http, store, manager, aid, _ = setup_directory(tmp_path)
    row = incoming(store, manager, 9991, "34", '<msg><voicemsg voicelength="1000"/></msg>')
    mid, cid = row["id"], row["conversation_id"]
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 16000)
    body = output.getvalue()
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO incoming_media(message_id,state,kind,sha256,reason,transcript,updated_at) VALUES(%s,'failed','voice',%s,'asr_http_400_CLIENT_ERROR','已有原文转写',0)",
            (mid, hashlib.sha256(body).hexdigest()),
        )
    target = manager.media.raw_path(mid)
    target.write_bytes(body)
    path = endpoint(aid, cid)
    before = {
        table: store.query(f"SELECT * FROM {table}")
        for table in ("tasks", "incoming_media", "media_resources", "message_records")
    }
    calls = list(http.app.state.gateway.calls)
    voice = http.get(path).json()["items"][0]["voice"]
    assert voice == dict(url=f"{path}/{mid}/voice", duration_ms=1000, transcript="已有原文转写")
    response = http.get(voice["url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    with wave.open(io.BytesIO(response.content)) as audio:
        assert (audio.getnchannels(), audio.getframerate(), audio.getnframes()) == (1, 16000, 16000)
    assert not list(manager.media.directory.glob("playback-*"))
    assert target.read_bytes() == body
    with playback_slot(manager.media.directory):
        with playback_slot(manager.media.directory):
            assert http.get(voice["url"]).status_code == 503
    assert not list(manager.media.directory.glob("playback-*"))
    other = manager.ensure_conversation(aid, "bob")
    assert http.get(f"{endpoint(aid, other['id'])}/{mid}/voice").status_code == 404
    account = manager.add_account("another-voice-account")
    assert http.get(f"{endpoint(account['id'], cid)}/{mid}/voice").status_code == 404
    for bad in (b"corrupted", b""):
        target.write_bytes(bad)
        assert http.get(voice["url"]).status_code == 404
    target.unlink()
    assert http.get(voice["url"]).status_code == 404
    outside = tmp_path / "outside.wav"
    outside.write_bytes(body)
    target.symlink_to(outside)
    assert http.get(voice["url"]).status_code == 404
    target.unlink()
    target.write_bytes(body)
    assert before == {table: store.query(f"SELECT * FROM {table}") for table in before}
    assert http.app.state.gateway.calls == calls
    http.cookies.clear()
    assert http.get(voice["url"]).status_code == 401
    assert code_login(http, target="13900000001").status_code == 200
    assert http.get(voice["url"]).status_code == 404


def test_playback_capacity_shared_across_processes_and_recovers_on_crash(tmp_path):
    code = "from pathlib import Path; from qingda.media_decode import playback_slot; import sys\nwith playback_slot(Path(sys.argv[1])):\n print('held',flush=True)\n input()\n"
    children = []
    try:
        for _ in range(2):
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(tmp_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            children.append(child)
            assert child.stdout.readline().strip() == "held"
        import pytest

        with pytest.raises(ValueError, match="voice_playback_busy"):
            with playback_slot(tmp_path):
                pass
        children[0].kill()
        children[0].wait(timeout=5)
        with playback_slot(tmp_path):
            pass
        children[1].stdin.write("\n")
        children[1].stdin.flush()
        children[1].wait(timeout=5)
        with playback_slot(tmp_path):
            with playback_slot(tmp_path):
                pass
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_cancelled_playback_kills_decoder_and_releases_slot(tmp_path):
    ready = tmp_path / "held"
    code = "from pathlib import Path; from qingda.media_decode import playback_slot; import time,sys\nwith playback_slot(Path(sys.argv[1])):\n Path(sys.argv[2]).touch()\n time.sleep(60)\n"

    async def check():
        task = asyncio.create_task(run_process(sys.executable, "-c", code, tmp_path, ready))
        try:
            for _ in range(100):
                if ready.exists():
                    break
                await asyncio.sleep(0.05)
            assert ready.exists()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with playback_slot(tmp_path):
            with playback_slot(tmp_path):
                pass

    asyncio.run(check())
