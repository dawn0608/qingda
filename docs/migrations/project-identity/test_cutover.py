"""切换窗口保持回调路径，冻结管理台，并拒绝不明网关。"""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "deploy"))


def runtime():
    assert Path(__file__).with_name("identity_cutover.py").is_file(), "缺少独立切换工具"
    return importlib.import_module("identity_cutover")


def test_gateway_freeze_preserves_callback_until_explicit_switch():
    from nginx_routes import routes

    module = runtime()
    before = routes().encode()
    frozen = module.freeze_gateway(before, paused=False)
    assert b"location ^~ /wechat/ {\n  return 503;" in frozen
    callback = frozen.split(b"location ^~ /wechatapi/callback/")[1]
    assert b"17950" in callback and b"17952" not in callback
    buffered = module.redirect_callback(frozen)
    assert b"17952" in buffered.split(b"location ^~ /wechatapi/callback/")[1]
    assert b"/wechatapi/callback/" in buffered


def test_unknown_gateway_aborts_and_paused_site_remains_paused():
    module = runtime()
    with pytest.raises(ValueError, match="gateway"):
        module.freeze_gateway(b"unknown", paused=False)
    before = b"server { return 503; }"
    assert module.freeze_gateway(before, paused=True) == before


def test_final_manifest_does_not_accept_old_identity(tmp_path):
    module = runtime()
    with pytest.raises(ValueError, match="identity"):
        module.verify_candidate({"revision": "old", "files": {}}, tmp_path)


def test_release_lock_excludes_another_process(tmp_path):
    import os
    import subprocess

    module = runtime()
    path = tmp_path / "release.lock"
    code = 'from identity_cutover import exclusive_release;from pathlib import Path;import sys\nwith exclusive_release(Path(sys.argv[1])): print("acquired")'
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}
    with module.exclusive_release(path):
        child = subprocess.run([sys.executable, "-c", code, str(path)], env=env, capture_output=True)
        assert child.returncode != 0 and b"storage_operation_in_progress" in child.stderr
    child = subprocess.run([sys.executable, "-c", code, str(path)], env=env, capture_output=True)
    assert child.returncode == 0


def test_peer_connection_restores_effective_uid():
    import os

    module = runtime()
    uid = os.geteuid()
    with module.peer(port=15432) as conn:
        assert os.geteuid() == uid
        assert conn.execute("SELECT current_user,current_database()").fetchone() == ("postgres", "postgres")
    assert os.geteuid() == uid


def test_historical_retention_protection_is_reversible_without_changing_payload(tmp_path):
    import json

    module = runtime()
    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "daily-original"
    old.mkdir()
    original = b'{"kind":"daily","state":"completed","reason":"original"}\n'
    (old / "retention.json").write_bytes(original)
    (old / "original.dump").write_bytes(b"unchanged backup")
    protected = backups / "protected"
    protected.mkdir()
    (protected / "retention.json").write_text('{"kind":"protected","state":"failed"}')
    state = tmp_path / "history.json"
    assert hasattr(module, "protect_history"), "历史备份必须退出新格式自动轮转"
    module.protect_history(backups, state)
    module.protect_history(backups, state)
    assert json.loads((old / "retention.json").read_text())["kind"] == "protected"
    assert (old / "original.dump").read_bytes() == b"unchanged backup"
    assert json.loads((protected / "retention.json").read_text())["state"] == "failed"
    module.restore_history(backups, state)
    assert (old / "retention.json").read_bytes() == original


def test_receiver_can_run_without_opening_private_backup_root(monkeypatch):
    import os
    import subprocess
    import tempfile

    module = runtime()
    with tempfile.TemporaryDirectory(prefix="qingda-receiver-permissions-", dir="/var/tmp") as temporary:
        root = Path(temporary)
        root.chmod(0o711)
        backups = root / "private"
        backups.mkdir(mode=0o700)
        data = root / "data"
        data.mkdir(mode=0o755)
        monkeypatch.setattr(module, "ROOT", backups)
        monkeypatch.setattr(module, "DATA", data, raising=False)
        cutover = module.Cutover(backups / "identity-test")
        cutover.folder.mkdir(mode=0o711)
        cutover.receiver.mkdir(parents=True, mode=0o755)
        code = cutover.receiver / "check.py"
        code.write_text('print("receiver-readable")\n')
        code.chmod(0o644)
        result = subprocess.run(
            ["runuser", "-u", "qingda", "--", "/usr/bin/python3.10", str(code)], capture_output=True
        )
        assert result.returncode == 0, result.stderr
        assert backups.stat().st_mode & 0o777 == 0o700
        assert os.geteuid() == 0
