"""服务器隔离环境验证原生网关及验收后清理契约。"""

import importlib.util
import json
import multiprocessing
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
sys.path.insert(0, str(DEPLOY))
native = __import__("native_nginx")
proxy = __import__("proxy")
retention = __import__("storage_retention")

spec = importlib.util.spec_from_file_location("native_release_test", DEPLOY / "update.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_gateway_validates_and_reloads_nginx(tmp_path, monkeypatch):
    gateway = proxy.Gateway()
    gateway.path = tmp_path / "nginx.conf"
    gateway.path.write_bytes(b"isolated nginx config")
    calls = []
    monkeypatch.setattr(proxy.subprocess, "run", lambda args, **kwargs: calls.append(args))
    gateway.check(gateway.read_bytes())
    gateway.reload()
    assert len(calls) == 3 and all(args[0] == "nginx" for args in calls)


def test_zero_release_retention_preserves_unaccepted_and_failed(tmp_path):
    for name, state in [
        ("qingda-release-20260101-000001", "awaiting_acceptance"),
        ("qingda-release-20260101-000002", "needs_repair"),
    ]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "receipt.json").write_text("{}")
        (folder / "venv-before").mkdir()
        (folder / "retention.json").write_text(json.dumps({"kind": "release", "state": state}))
    policy = retention.Retention(root=tmp_path, scan_processes=False, release_keep=0)
    assert policy.plan()["delete"] == []
    (tmp_path / "qingda-release-20260101-000001/retention.json").write_text(
        '{"kind":"release","state":"completed","cleanup_reviewed":true}'
    )
    assert len(policy.plan()["delete"]) == 1


def test_zero_release_configuration_is_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(
        retention, "dotenv_values", lambda _: {"BACKUP_RELEASE_KEEP": "0", "BACKUP_DAILY_KEEP": "7"}
    )
    assert retention.configured().release_keep == 0
    with pytest.raises(ValueError):
        retention.Retention(root=tmp_path, release_keep=-1)
    with pytest.raises(ValueError):
        retention.Retention(root=tmp_path, daily_keep=-1)


def _lock_holder(path, event):
    with retention.storage_lock(Path(path)):
        event.set()
        import time

        time.sleep(30)


def test_independent_process_lock_crash_releases_owner(tmp_path):
    lock = tmp_path / "operation.lock"
    event = multiprocessing.Event()
    process = multiprocessing.Process(target=_lock_holder, args=(str(lock), event))
    process.start()
    assert event.wait(5)
    try:
        with pytest.raises(RuntimeError, match="storage_operation_in_progress"):
            with retention.storage_lock(lock):
                pass
    finally:
        process.terminate()
        process.join(5)
    with retention.storage_lock(lock):
        pass


def test_release_waits_for_new_nginx_worker_before_stopping_app(monkeypatch):
    responses = [None, "qingda-postgres-buffer"]

    class Response:
        status = 200

        def __enter__(self):
            self.headers = {"X-Callback-Receiver": responses.pop(0)}
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(release, "urlopen", lambda *args, **kwargs: Response())
    monkeypatch.setattr(release.time, "sleep", lambda _: None)
    release.wait_buffer_callback("https://isolated.invalid/callback")
    assert not responses
