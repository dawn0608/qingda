"""按已审查文件清单回收；不以备份标签自动判定原件无用。"""

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

from layout import ROOT
from storage_retention import Retention, read_json, sha, storage_lock


def file_entry(path, reason):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not reason.strip():
        raise ValueError("cleanup_requires_regular_file_and_reason")
    value = path.stat()
    return {
        "path": str(path.absolute()),
        "reason": reason,
        "sha256": sha(path),
        "fingerprint": [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns],
    }


def validate(root, entry):
    path = Path(entry["path"])
    if (
        not path.is_absolute()
        or path == root
        or not path.is_relative_to(root)
        or path.is_symlink()
        or path.resolve() != path
        or not str(entry.get("reason", "")).strip()
    ):
        raise ValueError("unsafe_selective_cleanup_target")
    if not path.exists():
        return None
    actual = file_entry(path, entry["reason"])
    if actual["sha256"] != entry.get("sha256") or actual["fingerprint"] != entry.get("fingerprint"):
        raise ValueError("selective_cleanup_target_changed")
    if path.stat().st_dev != root.stat().st_dev or path.is_mount():
        raise ValueError("mounted_cleanup_target")
    reject_mounts(root, path)
    return path


def reject_mounts(root, path):
    info = Path("/proc/self/mountinfo")
    if info.exists():
        for line in info.read_text().splitlines():
            mount = Path(line.split()[4].replace("\\040", " ").replace("\\134", "\\"))
            if mount.is_relative_to(root) and (mount == path or mount in path.parents):
                raise ValueError("mounted_cleanup_target")


def unlink_verified(root, entry):
    """逐级固定目录 FD；后续路径替换不能将删除重定向至其他目录。"""
    path = Path(entry["path"])
    descriptors = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        parts = path.relative_to(root).parts
        for part in parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
        descriptors.append(source)
        value = os.fstat(source)
        fingerprint = [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]
        if not stat.S_ISREG(value.st_mode) or fingerprint != entry["fingerprint"]:
            raise ValueError("selective_cleanup_target_changed")
        digest = hashlib.sha256()
        while chunk := os.read(source, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != entry["sha256"]:
            raise ValueError("selective_cleanup_target_changed")
        final = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        if (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_size) != (
            value.st_dev,
            value.st_ino,
            value.st_mtime_ns,
            value.st_size,
        ):
            raise ValueError("selective_cleanup_target_changed")
        reject_mounts(root, path)
        os.unlink(parts[-1], dir_fd=current)
        return value.st_size
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def run(plan, *, apply=False, scan_processes=True):
    root = Path(plan["root"])
    if not root.is_absolute() or root.resolve() != root or root.is_symlink() or not root.is_dir():
        raise ValueError("unsafe_selective_cleanup_root")
    policy = Retention(root=root, scan_processes=scan_processes)
    entries = plan["entries"]
    if len({entry["path"] for entry in entries}) != len(entries):
        raise ValueError("duplicate_cleanup_target")
    for entry in entries:
        path = validate(root, entry)
        if path and policy.busy(path):
            raise RuntimeError("cleanup_target_busy")
    result = {"root": str(root), "candidates": entries, "deleted": [], "bytes_unlinked": 0}
    if apply:
        for entry in entries:
            path = validate(root, entry)
            if path:
                if policy.busy(path):
                    raise RuntimeError("cleanup_target_busy")
                size = unlink_verified(root, entry)
                result["deleted"].append(str(path))
                result["bytes_unlinked"] += size
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = read_json(args.manifest)
    if Path(plan["root"]) != ROOT or os.geteuid() != 0:
        raise ValueError("only_project_backup_root_allowed")
    with storage_lock():
        print(json.dumps(run(plan, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
