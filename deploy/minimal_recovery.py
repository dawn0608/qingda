"""发布事务只移动已有源码；核心数据保护必须显式声明。"""

import json
import os
from pathlib import Path

from storage_retention import read_json, save_json, sha


def source_path(root, name):
    path = root / name
    if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink():
        raise ValueError("unsafe_transition_path")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("unsafe_transition_path")
    return path


def move_sources(app, stage, old):
    names = sorted(set(old["files"]) | {"release.json"})
    for name in names:
        if not source_path(app, name).is_file():
            raise ValueError("missing_transition_source")
    previous = stage / "previous-source"
    previous.mkdir(mode=0o700)
    save_json(stage / "transition.json", {"files": names, "phase": "moving"})
    for name in names:
        target = source_path(previous, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(app / name, target)
    save_json(stage / "transition.json", {"files": names, "phase": "moved"})


def restore_sources(app, stage, spec):
    record = read_json(stage / "transition.json")
    old_names = set(record["files"])
    # moving 阶段尚未安装新文件，不能删除仍在原处的旧文件。
    if record["phase"] == "moved":
        for name in set(spec["files"]) - old_names:
            source_path(app, name).unlink(missing_ok=True)
    for name in record["files"]:
        source = source_path(stage / "previous-source", name)
        if source.exists():
            target = source_path(app, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
    save_json(stage / "transition.json", {**record, "phase": "restored"})


def check_migration_review(previous, target, review_path, revision=None):
    if previous == target:
        return
    if not review_path:
        raise ValueError("migration_review_required")
    review = read_json(Path(review_path))
    if (
        review.get("from_schema") != previous
        or review.get("to_schema") != target
        or type(review.get("data_loss")) is not bool
        or not str(review.get("reason", "")).strip()
        or (revision is not None and review.get("target_revision") != revision)
    ):
        raise ValueError("migration_review_mismatch")
    if review["data_loss"]:
        from server import validate_scope

        tables, files = review.get("required_tables", []), review.get("required_files", [])
        if review.get("scope_verified") is not True or review.get("restore_verified") is not True:
            raise ValueError("critical_scope_and_restore_review_required")
        selected = validate_scope(review["reason"], tables, files, review.get("release_condition", ""))
        review["required_tables"], review["required_files"] = tables, files
        if "public.migration_inbox" in tables:
            raise ValueError("live_callback_buffer_must_not_be_destructively_migrated")
        if review.get("recovery_point"):
            point = Path(review["recovery_point"])
            manifest = read_json(point / "manifest.json")
            metadata = read_json(point / "retention.json")
            if (
                metadata.get("state") != "completed"
                or manifest.get("format") != "qingda-critical-v1"
                or not manifest.get("sha256")
                or review.get("recovery_manifest_sha256") != sha(point / "manifest.json")
                or not set(tables) <= set(manifest.get("tables", []))
                or not set(files) <= set(manifest.get("files", []))
                or (tables and "data.dump" not in manifest["sha256"])
                or not {str(relative) for _, relative in selected} <= set(manifest["sha256"])
            ):
                raise ValueError("critical_recovery_point_invalid")
            for name, digest in manifest["sha256"].items():
                if sha(source_path(point, name)) != digest:
                    raise ValueError("critical_recovery_point_drift")
    return review


def protect_migration(review):
    """只在已排空并停止业务写入后调用；旧点的哈希不能证明涵盖最后写入。"""
    if not review or not review["data_loss"]:
        return None
    from server import backup

    return backup(
        reason=review["reason"],
        tables=review["required_tables"],
        files=review["required_files"],
        release_condition=review["release_condition"],
        lock=False,
    )["backup"]


def activate_runtime(python, app, stage):
    runtime = Path(python).parent.parent
    active = app / ".venv"
    if runtime == active:
        return
    if runtime.is_symlink() or active.is_symlink():
        raise ValueError("unsafe_runtime_link")
    os.rename(active, stage / "venv-before")
    os.rename(runtime, active)
    # 回调缓冲进程仍使用候选解释器路径；切换期间只建链接，不复制依赖。
    runtime.symlink_to(active, target_is_directory=True)
    save_json(stage / "runtime-transition.json", {"alias": str(runtime)})


def restore_runtime(app, stage):
    old = stage / "venv-before"
    if not old.exists():
        return
    record = stage / "runtime-transition.json"
    if record.exists():
        alias = Path(json.loads(record.read_text())["alias"])
        if alias.is_relative_to(stage) and alias.is_symlink():
            alias.unlink()
    active = app / ".venv"
    if active.exists():
        os.rename(active, stage / "venv-incomplete")
    os.rename(old, active)
