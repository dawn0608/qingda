"""无业务库的运维契约；只在临时目录测试切换与清理。"""

import hashlib
import importlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEPLOY))
retention = importlib.import_module("storage_retention")


def test_zero_daily_retention_removes_completed_but_keeps_unique_history(tmp_path):
    for name, kind in [("daily-20260101-000001", "daily"), ("original", "protected")]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.json").write_text("{}")
        (folder / "retention.json").write_text(
            json.dumps({"kind": kind, "state": "completed", "cleanup_reviewed": True})
        )
    plan = retention.Retention(root=tmp_path, scan_processes=False, daily_keep=0).plan()
    assert [Path(item["path"]).name for item in plan["delete"]] == ["daily-20260101-000001"]
    assert [Path(item["path"]).name for item in plan["retained"]] == ["original"]


def test_move_transition_keeps_inode_and_restores_partial_switch(tmp_path):
    recovery = importlib.import_module("minimal_recovery")
    app, stage = tmp_path / "app", tmp_path / "stage"
    app.mkdir()
    stage.mkdir()
    (app / "module.py").write_text("old")
    (app / "release.json").write_text('{"revision":"old"}')
    (app / "persistent").write_text("business data")
    inode = (app / "module.py").stat().st_ino
    recovery.move_sources(app, stage, {"files": {"module.py": "unused"}})
    assert not (app / "module.py").exists()
    assert (stage / "previous-source/module.py").stat().st_ino == inode
    (app / "module.py").write_text("new")
    (app / "added.py").write_text("new")
    recovery.restore_sources(app, stage, {"files": {"module.py": "", "added.py": ""}})
    assert (app / "module.py").read_text() == "old"
    assert (app / "module.py").stat().st_ino == inode
    assert not (app / "added.py").exists()
    assert (app / "persistent").read_text() == "business data"
    assert not list(stage.glob("*.tar*"))


def test_transition_rejects_source_path_escape_before_moving(tmp_path):
    recovery = importlib.import_module("minimal_recovery")
    app, stage = tmp_path / "app", tmp_path / "stage"
    app.mkdir()
    stage.mkdir()
    (app / "release.json").write_text("{}")
    with pytest.raises(ValueError):
        recovery.move_sources(app, stage, {"files": {"../secret": ""}})
    assert (app / "release.json").exists()


def test_migration_review_does_not_require_backup_for_additive_change(tmp_path):
    recovery = importlib.import_module("minimal_recovery")
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps({"from_schema": 59, "to_schema": 60, "data_loss": False, "reason": "add index"})
    )
    recovery.check_migration_review(59, 60, review)
    recovery.check_migration_review(59, 59, None)
    with pytest.raises(ValueError):
        recovery.check_migration_review(59, 60, None)


def test_lossy_migration_requires_verified_explicit_recovery_point(tmp_path):
    recovery = importlib.import_module("minimal_recovery")
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps({"from_schema": 59, "to_schema": 60, "data_loss": True, "reason": "drop core"})
    )
    with pytest.raises(ValueError):
        recovery.check_migration_review(59, 60, review)


def test_manual_backup_requires_reason_and_explicit_scope_before_writes(tmp_path, monkeypatch):
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        server.backup(reason="", tables=[], files=[], release_condition="")
    assert list(tmp_path.iterdir()) == []


def test_selective_cleanup_dry_run_and_apply_preserve_core_file(tmp_path):
    cleanup = importlib.import_module("selective_cleanup")
    folder = tmp_path / "protected"
    folder.mkdir()
    source, data = folder / "source.tar.gz", folder / "original.dump"
    source.write_bytes(b"rebuildable code")
    data.write_bytes(b"unique original")
    plan = {"root": str(tmp_path), "entries": [cleanup.file_entry(source, "Git recoverable")]}
    assert cleanup.run(plan, apply=False, scan_processes=False)["deleted"] == []
    assert source.exists()
    result = cleanup.run(plan, apply=True, scan_processes=False)
    assert len(result["deleted"]) == 1 and not source.exists()
    assert data.read_bytes() == b"unique original"
    assert cleanup.run(plan, apply=True, scan_processes=False)["deleted"] == []


def test_selective_cleanup_validates_entire_manifest_before_deletion(tmp_path):
    cleanup = importlib.import_module("selective_cleanup")
    first, changed = tmp_path / "first", tmp_path / "changed"
    first.write_text("one")
    changed.write_text("two")
    plan = {
        "root": str(tmp_path),
        "entries": [cleanup.file_entry(first, "obsolete"), cleanup.file_entry(changed, "obsolete")],
    }
    changed.write_text("new unique data")
    with pytest.raises(ValueError):
        cleanup.run(plan, apply=True, scan_processes=False)
    assert first.exists() and changed.exists()


def test_selective_cleanup_rejects_symlink_and_outside_root(tmp_path):
    cleanup = importlib.import_module("selective_cleanup")
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("unique")
    entry = cleanup.file_entry(outside, "test")
    with pytest.raises(ValueError):
        cleanup.run({"root": str(root), "entries": [entry]}, apply=True, scan_processes=False)
    link = root / "link"
    link.symlink_to(outside)
    entry["path"] = str(link)
    with pytest.raises(ValueError):
        cleanup.run({"root": str(root), "entries": [entry]}, apply=True, scan_processes=False)
    assert outside.read_text() == "unique"


def test_default_zero_retention_does_not_delete_unreviewed_historical_daily(tmp_path):
    folder = tmp_path / "daily-20260101-000001"
    folder.mkdir()
    (folder / "manifest.json").write_text("{}")
    (folder / "retention.json").write_text('{"kind":"daily","state":"completed"}')
    plan = retention.Retention(root=tmp_path, scan_processes=False).plan()
    assert plan["delete"] == []


def test_zero_retention_preserves_unreviewed_legacy_release(tmp_path):
    folder = tmp_path / "qingda-release-20260101-000001"
    folder.mkdir()
    (folder / "venv-before").mkdir()
    (folder / "receipt.json").write_text("{}")
    (folder / "qingda-before.dump").write_text("unique core data")
    (folder / "retention.json").write_text('{"kind":"release","state":"completed"}')
    assert retention.Retention(root=tmp_path, scan_processes=False).plan()["delete"] == []


def test_selective_cleanup_rejects_parent_swap_during_process_check(tmp_path, monkeypatch):
    cleanup = importlib.import_module("selective_cleanup")
    root, outside = tmp_path / "root", tmp_path / "outside"
    parent = root / "folder"
    parent.mkdir(parents=True)
    outside.mkdir()
    target = parent / "same"
    target.write_text("obsolete")
    (outside / "same").write_text("unique outside")
    plan = {"root": str(root), "entries": [cleanup.file_entry(target, "obsolete")]}
    calls = []

    def swap(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            parent.rename(root / "original")
            parent.symlink_to(outside, target_is_directory=True)
        return False

    monkeypatch.setattr(retention.Retention, "busy", swap)
    with pytest.raises((ValueError, OSError)):
        cleanup.run(plan, apply=True)
    assert (outside / "same").read_text() == "unique outside"
    assert (root / "original/same").read_text() == "obsolete"


def test_selective_cleanup_refuses_ancestor_bind_mount(tmp_path, monkeypatch):
    cleanup = importlib.import_module("selective_cleanup")
    root = tmp_path / "root"
    mounted = root / "mounted"
    mounted.mkdir(parents=True)
    target = mounted / "same"
    target.write_text("unique mount source")
    plan = {"root": str(root), "entries": [cleanup.file_entry(target, "obsolete")]}
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return f"1 2 0:1 / {mounted} rw - ext4 /dev/test rw\n"
        return original(path, *args, **kwargs)

    original_exists = Path.exists
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(
        Path, "exists", lambda path: True if str(path) == "/proc/self/mountinfo" else original_exists(path)
    )
    with pytest.raises(ValueError):
        cleanup.run(plan, apply=True, scan_processes=False)
    assert target.exists()


def test_busy_detects_exact_script_argument(tmp_path, monkeypatch):
    fake_proc = tmp_path / "proc"
    proc = fake_proc / "999999"
    (proc / "fd").mkdir(parents=True)
    script = tmp_path / "running.py"
    script.write_text("running")
    (proc / "cmdline").write_bytes(b"python\0" + str(script).encode() + b"\0")
    original = retention.Path
    monkeypatch.setattr(retention, "Path", lambda p: fake_proc if str(p) == "/proc" else original(p))
    assert retention.Retention(root=tmp_path).busy(script)


def test_lossy_review_rejects_unrelated_failed_recovery_point(tmp_path):
    recovery = importlib.import_module("minimal_recovery")
    point = tmp_path / "point"
    point.mkdir()
    (point / "unrelated").write_text("not core data")
    (point / "manifest.json").write_text(
        json.dumps(
            {"format": "qingda-critical-v1", "sha256": {"unrelated": retention.sha(point / "unrelated")}}
        )
    )
    (point / "retention.json").write_text('{"kind":"protected","state":"needs_repair"}')
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "from_schema": 59,
                "to_schema": 60,
                "data_loss": True,
                "reason": "drop core",
                "recovery_point": str(point),
            }
        )
    )
    with pytest.raises(ValueError):
        recovery.check_migration_review(59, 60, review)


def test_domain_prepare_does_not_copy_database_or_config(tmp_path, monkeypatch):
    domain = importlib.import_module("domain")
    app, folder = tmp_path / "app", tmp_path / "operation"
    app.mkdir()
    folder.mkdir()
    (app / "release.json").write_text('{"revision":"test"}')
    (app / ".env").write_text("PUBLIC_URL=http://127.0.0.1\nSECRET=must-not-copy\n")
    (folder / "receipt.json").write_text('{"revision":"test","backup_sha256":{}}')
    monkeypatch.setattr(domain, "APP", app)
    monkeypatch.setattr(domain, "CURRENT", tmp_path / "current.json")
    monkeypatch.setattr(domain, "folder_path", lambda value: value)
    monkeypatch.setattr(domain, "register", lambda *args: None)
    monkeypatch.setattr(domain, "query", lambda *args: [])
    monkeypatch.setattr(
        domain,
        "Gateway",
        lambda: type("Gateway", (), {"native": True, "read_bytes": lambda self: b"gateway"})(),
    )

    def forbidden(*args):
        pytest.fail("ordinary domain prepare must not archive proxy state")

    monkeypatch.setattr(domain, "backup_state", forbidden, raising=False)
    domain.prepare(folder)
    assert not list(folder.glob("*.dump"))
    assert not list(folder.glob("*.tar.gz"))
    assert not list(folder.glob("*.before"))
    assert "must-not-copy" not in (folder / "domain-switch.json").read_text()


def test_core_backup_copies_only_explicit_file_and_records_condition(tmp_path, monkeypatch):
    server = importlib.import_module("server")
    data, root = tmp_path / "data", tmp_path / "backups"
    data.mkdir()
    root.mkdir()
    (data / "core").write_text("unique")
    (data / "unrelated").write_text("must not be copied")
    monkeypatch.setattr(server, "DATA", data)
    monkeypatch.setattr(server, "ROOT", root)
    monkeypatch.setattr(retention, "ROOT", root)
    monkeypatch.setattr(server, "storage_lock", lambda: retention.storage_lock(tmp_path / "lock"))
    result = server.backup(
        reason="irreversible core replacement",
        tables=[],
        files=[str(data / "core")],
        release_condition="replacement verified",
    )
    folder = Path(result["backup"])
    assert (folder / "files/data/core").read_text() == "unique"
    assert not (folder / "files/data/unrelated").exists()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["release_condition"] == "replacement verified"
    assert result["restore_check"] is False


@pytest.mark.parametrize("table", ["*", "public.*", "public.foo;DROP TABLE users", "users"])
def test_core_backup_rejects_wildcard_or_ambiguous_table_scope(table):
    server = importlib.import_module("server")
    with pytest.raises(ValueError):
        server.validate_scope("core", [table], [], "after verified")


@pytest.fixture
def release_environment(tmp_path, monkeypatch):
    update = importlib.import_module("update")
    app, root, config = [tmp_path / name for name in ("app", "backups", "config")]
    for p in (app / "backend/qingda", app / ".venv/bin", app / "deploy", root, config):
        p.mkdir(parents=True, exist_ok=True)
    (app / "backend/qingda/store.py").write_text("SCHEMA_VERSION = 59\n")
    (app / "app.py").write_text("old source")
    (app / ".venv/bin/python").write_text("runtime")
    env = "DATABASE_URL=postgresql://fixture:fixture@localhost/fixture\nAPP_REVISION=old\n"
    for key in (
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "ALIYUN_SMS_SIGN_NAME",
        "ALIYUN_SMS_TEMPLATE_CODE",
    ):
        env += key + "=fixture\n"
    (app / ".env").write_text(env)
    (config / "outbound-paused").touch()
    old = {
        "project_identity": "qingda",
        "revision": "old",
        "files": {name: retention.sha(app / name) for name in ("backend/qingda/store.py", "app.py")},
    }
    (app / "release.json").write_text(json.dumps(old))
    payload = {"backend/qingda/store.py": b"SCHEMA_VERSION = 59\n", "app.py": b"new source"}
    spec = {
        "project_identity": "qingda",
        "revision": "new",
        "files": {name: hashlib.sha256(body).hexdigest() for name, body in payload.items()},
        "before": old["files"],
        "expected_manifest_sha256": retention.sha(app / "release.json"),
        "expected_nginx_sha256": hashlib.sha256(b"nginx").hexdigest(),
    }
    payload["release.json"] = json.dumps(spec).encode()
    bundle = tmp_path / "release.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for name, body in payload.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    for module in (update, retention):
        monkeypatch.setattr(module, "APP", app)
        monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(update, "CONFIG", config)
    monkeypatch.setattr(update, "dependency_runtime", lambda *args: app / ".venv/bin/python")
    monkeypatch.setattr(update, "pg_url", lambda *args: "fixture")
    monkeypatch.setattr(update, "command", lambda args, **kw: b"0")
    monkeypatch.setattr(update, "install_units", lambda: None)
    monkeypatch.setattr(update, "wait_health", lambda *args: None)
    monkeypatch.setattr(update, "verify_api_auth", lambda *args: None)
    monkeypatch.setattr(
        update,
        "Gateway",
        lambda: type("Gateway", (), {"backend": "native", "read_bytes": lambda self: b"nginx"})(),
    )

    def no_backup(*args):
        pytest.fail("ordinary release must not archive files")

    monkeypatch.setattr(update, "backup_files", no_backup, raising=False)

    def run_python(python, candidate, url, code):
        assert "backup_database" not in code
        return b"59"

    monkeypatch.setattr(update, "run_python", run_python)
    return update, app, root, bundle


def test_ordinary_release_uses_no_data_or_source_backup(release_environment):
    update, app, root, bundle = release_environment
    update.release(bundle, retention.sha(bundle))
    assert (app / "app.py").read_text() == "new source"
    assert not list(root.rglob("*.dump"))
    assert not list(root.rglob("*.tar.gz"))
    assert not list(root.rglob("venv-before"))
    assert not list(root.rglob("previous-source"))
    receipt = json.loads(next(root.glob("*/receipt.json")).read_text())
    assert receipt["runtime_verified"] is True


def test_failed_release_restores_moved_source_without_old_database(release_environment, monkeypatch):
    update, app, root, bundle = release_environment

    def fail_health(*args):
        raise RuntimeError("injected health failure")

    monkeypatch.setattr(update, "wait_health", fail_health)
    with pytest.raises(RuntimeError, match="injected health failure"):
        update.release(bundle, retention.sha(bundle))
    assert (app / "app.py").read_text() == "old source"
    assert json.loads((app / "release.json").read_text())["revision"] == "old"
    assert "APP_REVISION=old" in (app / ".env").read_text()
    assert not list(root.rglob("*.dump"))


def test_lossy_release_captures_final_core_state_after_stopping_writes(
    release_environment, monkeypatch, tmp_path
):
    update, app, root, bundle = release_environment
    server = importlib.import_module("server")
    data = tmp_path / "core-data"
    data.mkdir()
    core = data / "original"
    core.write_text("before final write")
    for name, value in (("APP", app), ("ROOT", root), ("DATA", data)):
        monkeypatch.setattr(server, name, value)
    with tarfile.open(bundle) as archive:
        payload = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
    spec = json.loads(payload["release.json"])
    payload["backend/qingda/store.py"] = b"SCHEMA_VERSION = 60\n"
    spec["files"]["backend/qingda/store.py"] = hashlib.sha256(payload["backend/qingda/store.py"]).hexdigest()
    payload["release.json"] = json.dumps(spec).encode()
    with tarfile.open(bundle, "w:gz") as archive:
        for name, body in payload.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "from_schema": 59,
                "to_schema": 60,
                "target_revision": "new",
                "data_loss": True,
                "reason": "irreversible core change",
                "required_tables": [],
                "required_files": [str(core)],
                "scope_verified": True,
                "restore_verified": True,
                "release_condition": "migration verified",
            }
        )
    )

    def command(args, **kwargs):
        if args[:2] == ["systemctl", "stop"]:
            core.write_text("last committed state at write freeze")
        return b"0"

    monkeypatch.setattr(update, "command", command)
    monkeypatch.setattr(update, "drain_replies", lambda *args: None)
    update.release(bundle, retention.sha(bundle), migration_review=review)
    snapshots = list(root.glob("critical-*/files/data/original"))
    assert len(snapshots) == 1
    assert snapshots[0].read_text() == "last committed state at write freeze"
    assert not list(root.rglob("*.tar.gz"))
