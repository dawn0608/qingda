"""显式 wechat-manager → qingda 运维迁移；业务模块、数据库及协议保持兼容。"""

import json
import os
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from storage_retention import save_json, storage_lock

PATHS = [
    (Path(parent) / "wechat-manager", Path(parent) / "qingda")
    for parent in ("/opt", "/etc", "/var/lib", "/var/backups")
] + [(Path(parent) / "wechat-manager-acme", Path(parent) / "qingda-acme") for parent in ("/opt", "/var/lib")]
LOCKS = [Path("/run/lock") / (name + "-release.lock") for name in ("wechat-manager", "qingda")]


def fingerprint(path):
    value = path.stat(follow_symlinks=False)
    return [value.st_dev, value.st_ino]


class MoveJournal:
    """先持久化 inode 身份，再 rename；崩溃后按身份判定已完成的方向。"""

    def __init__(self, path, pairs):
        self.path = Path(path)
        self.pairs = [(Path(old), Path(new)) for old, new in pairs]

    def prepare(self):
        if self.path.exists():
            raise ValueError("journal_already_exists")
        rows = []
        for old, new in self.pairs:
            if new.exists() or new.is_symlink():
                raise ValueError("target_collision: " + str(new))
            if old.is_symlink() or not old.is_dir():
                raise ValueError("source_identity_invalid: " + str(old))
            if old.stat().st_dev != new.parent.stat().st_dev:
                raise ValueError("cross_device_migration_not_supported")
            rows.append({"old": str(old), "new": str(new), "identity": fingerprint(old)})
        save_json(self.path, {"moves": rows, "direction": "prepared"})

    def move(self, reverse=False):
        state = json.loads(self.path.read_text())
        rows = list(reversed(state["moves"])) if reverse else state["moves"]
        for row in rows:
            source, target = (Path(row[k]) for k in (("new", "old") if reverse else ("old", "new")))
            if source.is_symlink() or target.is_symlink():
                raise ValueError("path_identity_invalid")
            present = [p for p in (source, target) if p.exists()]
            if len(present) != 1 or fingerprint(present[0]) != row["identity"]:
                raise ValueError("path_identity_changed")
            if present[0] == source:
                source.rename(target)
                for parent in {source.parent, target.parent}:
                    fd = os.open(parent, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            state["direction"] = "backward" if reverse else "forward"
            save_json(self.path, state)

    def forward(self):
        self.move()

    def backward(self):
        self.move(reverse=True)


@contextmanager
def migration_locks(paths=LOCKS):
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(storage_lock(path))
        yield


def rewrite_environment(text):
    allowed = {"APP_ROOT", "DATA_DIR", "MASTER_KEY_FILE", "OUTBOUND_PAUSE_FILE"}
    lines = []
    for line in text.splitlines(keepends=True):
        if line.split("=", 1)[0] in allowed:
            for old, new in PATHS:
                line = line.replace(str(old), str(new))
        lines.append(line)
    return "".join(lines)


def restore_runtime_entries(saved, target, identities, failed):
    """逐项核对原 inode，既支持搬移中断，也支持恢复自身再次中断。"""
    saved, target, failed = Path(saved), Path(target), Path(failed)
    failed.mkdir(exist_ok=True)
    for name, identity in identities.items():
        if Path(name).name != name:
            raise ValueError("unsafe_runtime_entry")
        source, destination = saved / name, target / name
        if source.exists() or source.is_symlink():
            if fingerprint(source) != identity:
                raise ValueError("runtime_identity_changed")
            if destination.exists() or destination.is_symlink():
                destination.rename(failed / (name + "." + str(time.time_ns())))
            source.rename(destination)
        elif not (destination.exists() or destination.is_symlink()) or fingerprint(destination) != identity:
            raise ValueError("runtime_original_missing")


def rewrite_control_state(state):
    """只改活动控制记录的备份目录引用，不改历史证据或任意字符串。"""
    result = dict(state)
    folder = result.get("folder")
    old, new = Path("/var/backups/wechat-manager"), Path("/var/backups/qingda")
    if folder is not None:
        path = Path(folder)
        if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(old):
            raise ValueError("unexpected_control_folder")
        result["folder"] = str(new / path.relative_to(old))
    return result


def sync_tree(folder):
    """先持久化恢复材料及目录项，再进入资源切换。"""
    folder = Path(folder)
    for path in [*folder.rglob("*"), folder, folder.parent]:
        if path.is_symlink():
            continue
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def replace_symlink(path, target):
    """证书链接原子替换；可恢复缺失链接，不覆盖意外出现的普通文件。"""
    path = Path(path)
    if path.exists() and not path.is_symlink():
        raise ValueError("certificate_link_replaced_by_file")
    temporary = path.with_name(path.name + ".migration-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate-project-name", action="store_true")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--state", type=Path, required=True, help="生产源码外的受限迁移记录目录")
    parser.add_argument("--check", action="store_true", help="仅检查资源冲突，不修改线上")
    parser.add_argument("--rollback", action="store_true", help="中断后恢复旧布局，保留最新数据库")
    args = parser.parse_args()
    if not args.migrate_project_name or os.geteuid() != 0:
        parser.error("仅限 root 显式 --migrate-project-name")
    if os.environ.get("QINGDA_DEPLOY_LAYOUT") != "legacy":
        os.execve(
            sys.executable, [sys.executable, *sys.argv], {**os.environ, "QINGDA_DEPLOY_LAYOUT": "legacy"}
        )
    from rename_runtime import execute

    execute(args)


if __name__ == "__main__":
    main()
