"""服务器隔离目录中的命名迁移契约，不连接业务数据库。"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY))


def test_default_deployment_uses_qingda():
    import proxy

    assert proxy.APP == Path("/opt/qingda")
    assert proxy.ACME == Path("/var/lib/qingda-acme")


def migration_module():
    assert (DEPLOY / "rename_project.py").is_file(), "缺少显式迁移入口"
    return importlib.import_module("rename_project")


def test_move_journal_recovers_interruption_without_overwriting(tmp_path):
    migration = migration_module()
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    (old / "data").write_text("original")
    journal = migration.MoveJournal(tmp_path / "journal.json", [(old, new)])
    journal.prepare()
    # 模拟 rename 已提交但完成标记尚未落盘。
    old.rename(new)
    journal.forward()
    journal.forward()
    assert (new / "data").read_text() == "original"
    journal.backward()
    journal.backward()
    assert (old / "data").read_text() == "original"
    assert not new.exists()


def test_move_journal_rejects_collision_and_symlink(tmp_path):
    migration = migration_module()
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    journal = migration.MoveJournal(tmp_path / "journal.json", [(old, new)])
    with pytest.raises(ValueError, match="collision"):
        journal.prepare()
    new.rmdir()
    new.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="collision"):
        journal.prepare()
    assert old.is_dir()


def test_move_journal_rejects_replaced_source(tmp_path):
    migration = migration_module()
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    journal = migration.MoveJournal(tmp_path / "journal.json", [(old, new)])
    journal.prepare()
    old.rename(tmp_path / "displaced")
    old.mkdir()
    with pytest.raises(ValueError, match="identity"):
        journal.forward()
    assert not new.exists()


def test_layout_requires_explicit_legacy_selection():
    code = "import layout; print(layout.APP); print(layout.unit('worker'))"
    for profile, expected in [(None, "qingda"), ("legacy", "wechat-manager")]:
        env = {**os.environ, "PYTHONPATH": str(DEPLOY)}
        env.pop("QINGDA_DEPLOY_LAYOUT", None)
        if profile:
            env["QINGDA_DEPLOY_LAYOUT"] = profile
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [f"/opt/{expected}", f"{expected}-worker"]


def test_migration_locks_exclude_independent_process(tmp_path):
    migration = migration_module()
    paths = [tmp_path / "old.lock", tmp_path / "new.lock"]
    code = (
        "from pathlib import Path; from rename_project import migration_locks; "
        f"paths = [Path(p) for p in {list(map(str, paths))!r}]; "
        "\nwith migration_locks(paths): print('acquired')"
    )
    env = {**os.environ, "PYTHONPATH": str(DEPLOY)}
    with migration.migration_locks(paths):
        child = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        assert child.returncode != 0
        assert "storage_operation_in_progress" in child.stderr
    child = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert child.returncode == 0, child.stderr


def test_environment_rewrite_only_changes_declared_paths():
    migration = migration_module()
    before = (
        "DATA_DIR=/var/lib/wechat-manager\n"
        "MASTER_KEY_FILE=/etc/wechat-manager/master.key\n"
        "PUBLIC_URL=https://unchanged.example\n"
        "API_KEY=wechat-manager-secret\n"
    )
    after = migration.rewrite_environment(before)
    assert "DATA_DIR=/var/lib/qingda\n" in after
    assert "MASTER_KEY_FILE=/etc/qingda/master.key\n" in after
    assert "PUBLIC_URL=https://unchanged.example\n" in after
    assert "API_KEY=wechat-manager-secret\n" in after


def test_legacy_service_template_keeps_scheduled_jobs_on_legacy_layout(tmp_path):
    template = tmp_path / "qingda-backup.service"
    template.write_text(
        "[Service]\nUser=qingda\nExecStart=/opt/qingda/.venv/bin/python /opt/qingda/deploy/server.py backup\n"
    )
    code = f"from layout import service_template;print(service_template({str(template)!r}))"
    env = {**os.environ, "PYTHONPATH": str(DEPLOY), "QINGDA_DEPLOY_LAYOUT": "legacy"}
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Environment=QINGDA_DEPLOY_LAYOUT=legacy" in result.stdout
    assert "ExecStart=/opt/wechat-manager/.venv/bin/python" in result.stdout
    assert "User=wechat-manager" in result.stdout


def test_partial_migration_rollback_preserves_unmoved_directory(tmp_path):
    migration = migration_module()
    pairs = [(tmp_path / f"old{i}", tmp_path / f"new{i}") for i in range(2)]
    for old, _ in pairs:
        old.mkdir()
        (old / "original").write_text(old.name)
    journal = migration.MoveJournal(tmp_path / "journal.json", pairs)
    journal.prepare()
    pairs[0][0].rename(pairs[0][1])
    journal.backward()
    assert [(old / "original").read_text() for old, _ in pairs] == ["old0", "old1"]
    assert not any(new.exists() for _, new in pairs)


def test_old_layout_backup_requires_explicit_recovery_mapping():
    import layout

    assert hasattr(layout, "check_rollback_layout"), "旧布局备份不得直接覆盖新布局"
    with pytest.raises(ValueError, match="legacy_backup_requires_layout_recovery"):
        layout.check_rollback_layout({"deploy/wechat-manager-web.service": "hash"})
    layout.check_rollback_layout({"deploy/qingda-web.service": "hash"})


def test_partial_cert_runtime_restore_keeps_unmoved_entries(tmp_path):
    migration = migration_module()
    assert hasattr(migration, "restore_runtime_entries"), "必须支持证书环境逐项搬移中断"
    target, saved, failed = [tmp_path / name for name in ("runtime", "saved", "failed")]
    target.mkdir()
    saved.mkdir()
    (target / "bin").mkdir()
    (target / "bin/certbot").write_text("original-entry")
    (target / "pyvenv.cfg").write_text("original-config")
    (target / "lib64").symlink_to("lib")
    identities = {p.name: migration.fingerprint(p) for p in target.iterdir()}
    (target / "bin").rename(saved / "bin")
    migration.restore_runtime_entries(saved, target, identities, failed)
    migration.restore_runtime_entries(saved, target, identities, failed)
    assert (target / "bin/certbot").read_text() == "original-entry"
    assert (target / "pyvenv.cfg").read_text() == "original-config"
    assert (target / "lib64").readlink() == Path("lib")


def test_active_control_mapping_does_not_rewrite_history():
    migration = migration_module()
    before = {
        "folder": "/var/backups/wechat-manager/chat-release-1",
        "domain": "same.example",
        "note": "wechat-manager",
    }
    after = migration.rewrite_control_state(before)
    assert after["folder"] == "/var/backups/qingda/chat-release-1"
    assert before["folder"] == "/var/backups/wechat-manager/chat-release-1"
    assert after["domain"] == before["domain"] and after["note"] == before["note"]
    with pytest.raises(ValueError, match="unexpected_control_folder"):
        migration.rewrite_control_state({"folder": "/var/backups/wechat-manager/../unrelated"})


def test_failed_late_rollback_keeps_callback_receiver(tmp_path, monkeypatch):
    import rename_runtime as runtime

    operation = runtime.Migration(tmp_path)
    operation.state = {"phase": "services_started", "callback_enabled": True}
    (tmp_path / "moves.json").write_text("{}")
    events = []
    monkeypatch.setattr(operation, "start_buffer", lambda: events.append("buffer_ready"))
    monkeypatch.setattr(operation, "aliases", lambda create: None)

    def unit(verb, *names, **kwargs):
        if verb == "stop" and any("web" in name for name in names):
            assert "buffer_ready" in events
        events.append((verb, names))
        return b""

    monkeypatch.setattr(operation, "unit", unit)

    def interrupted_restore(self):
        raise RuntimeError("injected_restore_interruption")

    monkeypatch.setattr(runtime.MoveJournal, "backward", interrupted_restore)
    with pytest.raises(RuntimeError, match="injected_restore_interruption"):
        operation.rollback()
    assert events[0] == "buffer_ready"
    assert not any(isinstance(event, tuple) and event == ("stop", (runtime.BUFFER,)) for event in events)


def test_paused_gateway_without_callback_does_not_drain_or_open_receiver(tmp_path, monkeypatch):
    import rename_runtime as runtime

    operation = runtime.Migration(tmp_path)
    operation.state = {"paused": True, "callback_enabled": False, "active": []}
    monkeypatch.setattr(operation, "unit", lambda *args, **kwargs: b"inactive")

    def forbidden(*args, **kwargs):
        pytest.fail("停站且无回调路由，不应开启缓冲或等待暂停任务")

    monkeypatch.setattr(operation, "start_buffer", forbidden)
    monkeypatch.setattr(runtime.update, "drain_replies", forbidden)

    def reached_move(self):
        raise RuntimeError("isolated_move_boundary")

    monkeypatch.setattr(runtime.MoveJournal, "forward", reached_move)
    with pytest.raises(RuntimeError, match="isolated_move_boundary"):
        operation.cutover()


def test_rollback_timers_restart_after_both_locks_release(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import rename_runtime as runtime

    locked = []
    calls = []

    @contextmanager
    def locks():
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()

    class Operation:
        state = {"phase": "stopping", "original_active_timers": ["wechat-manager-backup.timer"]}

        def __init__(self, folder):
            pass

        def rollback(self):
            assert locked
            self.state["phase"] = "rolled_back"

        def unit(self, verb, name):
            assert not locked, "补跑任务不得与迁移锁冲突"
            calls.append((verb, name))

    monkeypatch.setattr(runtime, "Migration", Operation)
    monkeypatch.setattr(runtime, "migration_locks", locks)
    runtime.execute(SimpleNamespace(state=tmp_path, check=False, rollback=True))
    assert calls == [("start", "wechat-manager-backup.timer")]


def test_certificate_link_restore_handles_missing_link_without_touching_key(tmp_path):
    migration = migration_module()
    key = tmp_path / "key.pem"
    key.write_bytes(b"unchanged-private-material")
    link = tmp_path / "privkey.pem"
    migration.replace_symlink(link, key.name)
    assert link.read_bytes() == b"unchanged-private-material"
    link.unlink()
    migration.replace_symlink(link, key.name)
    assert link.readlink() == Path("key.pem")
    assert key.read_bytes() == b"unchanged-private-material"
    with pytest.raises(ValueError, match="certificate_link_replaced_by_file"):
        migration.replace_symlink(key, "other")


def test_rejected_lock_has_no_timer_side_effect(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import rename_runtime as runtime

    @contextmanager
    def locks():
        raise RuntimeError("storage_operation_in_progress")
        yield

    class Operation:
        state = {"phase": "completed", "original_active_timers": ["wechat-manager-backup.timer"]}

        def __init__(self, folder):
            pass

        def unit(self, *args):
            pytest.fail("拒绝执行不得启动任何服务")

    monkeypatch.setattr(runtime, "Migration", Operation)
    monkeypatch.setattr(runtime, "migration_locks", locks)
    with pytest.raises(RuntimeError, match="storage_operation_in_progress"):
        runtime.execute(SimpleNamespace(state=tmp_path, check=False, rollback=False))


def test_buffer_intent_is_persistent_before_receiver_start(tmp_path, monkeypatch):
    import json

    import rename_runtime as runtime

    operation = runtime.Migration(tmp_path)
    operation.state = {"phase": "dependencies_ready"}
    (tmp_path / "root-env").write_text("DATABASE_URL=postgresql://isolated/test\n")
    monkeypatch.setattr(operation, "unit", lambda *args, **kwargs: b"inactive")

    def fail_start(*args, **kwargs):
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["phase"] == "buffer_starting" and state["buffer_started"] is True
        raise RuntimeError("injected_start_interruption")

    monkeypatch.setattr(operation, "run", fail_start)
    with pytest.raises(RuntimeError, match="injected_start_interruption"):
        operation.start_buffer()
