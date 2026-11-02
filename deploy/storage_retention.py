"""发布、日备和手工运维共用的有界存储回收；命令行默认仅预览。"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values
from layout import APP, LOCK, ROOT

RELEASE_FILES = {
    "source.tar.gz",
    "manager-before.dump",
    "files/.env",
    "master.key",
    "nginx.before.conf",
    "attachments.tar.gz",
}
DAILY_FILES = {"manager.dump", "master.key", "root-env", "attachments.tar.gz"}


def sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_json(path):
    if path.is_symlink():
        raise ValueError("unsafe_metadata_link: " + path.name)
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    directory = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def register(path, kind, state, reason=""):
    if path.is_symlink() or path.resolve().parent != ROOT:
        raise ValueError("unsafe_registration_target")
    save_json(
        path / "retention.json", {"kind": kind, "state": state, "reason": reason, "updated_at": time.time()}
    )


@contextmanager
def storage_lock(path=LOCK):
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("storage_operation_in_progress") from None
        yield


class Retention:
    def __init__(
        self, root=ROOT, *, lock_path=LOCK, scan_processes=True, release_keep=3, daily_keep=7, ttl_hours=24
    ):
        self.root = Path(root)
        if self.root.is_symlink() or self.root.resolve() != self.root.absolute():
            raise ValueError("unsafe_backup_root")
        if release_keep < 0 or min(daily_keep, ttl_hours) < 1:
            raise ValueError("invalid_retention_policy")
        self.lock_path = Path(lock_path)
        self.scan_processes = scan_processes
        self.release_keep = release_keep
        self.daily_keep = daily_keep
        self.ttl = ttl_hours * 3600

    def safe(self, path):
        if path.is_symlink() or path.resolve().parent != self.root or not path.is_dir():
            raise ValueError("unsafe_cleanup_target: " + path.name)
        return path

    def busy(self, path, ignore_current=False):
        if not self.scan_processes:
            return False
        prefix = str(path) + "/"
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or (ignore_current and int(proc.name) == os.getpid()):
                continue
            try:
                links = [proc / "cwd", proc / "exe", *(proc / "fd").iterdir()]
                for link in links:
                    try:
                        value = os.readlink(link).removesuffix(" (deleted)")
                        if value == str(path) or value.startswith(prefix):
                            return True
                    except OSError:
                        pass
                args = (proc / "cmdline").read_bytes().split(b"\0")
                if any(arg.decode(errors="replace").startswith(prefix) for arg in args):
                    return True
            except (FileNotFoundError, ProcessLookupError):
                pass
        return False

    def fingerprint(self, path):
        stat = path.lstat()
        return [stat.st_dev, stat.st_ino, stat.st_mtime_ns]

    def deletion_safe(self, path, ignore_current=False):
        self.safe(path)
        # rmtree 不跟随符号链接；同时拒绝目录内的挂载点，防止跨文件系统删除。
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            mount = line.split()[4].replace("\\040", " ")
            if mount == str(path) or mount.startswith(str(path) + "/"):
                raise ValueError("mounted_cleanup_target: " + path.name)
        if self.busy(path, ignore_current):
            raise RuntimeError("cleanup_target_busy: " + path.name)

    def validate(self, path, release):
        self.safe(path)
        manifest = read_json(path / ("receipt.json" if release else "manifest.json"))
        hashes = manifest["backup_sha256"] if release else manifest
        required = RELEASE_FILES if release else DAILY_FILES - {"attachments.tar.gz"}
        if release:
            required = required | {"proxy-state.tar.gz"}
        if not required <= set(hashes):
            raise ValueError("incomplete_backup: " + path.name)
        for name, digest in hashes.items():
            source = path / name
            if source.is_symlink() or not source.resolve().is_relative_to(path) or sha(source) != digest:
                raise ValueError("backup_hash_mismatch: " + path.name + "/" + name)
        result = subprocess.run(
            ["pg_restore", "--list", str(path / ("manager-before.dump" if release else "manager.dump"))],
            capture_output=True,
        )
        if result.returncode:
            raise ValueError("backup_database_invalid: " + path.name)
        if release and not (path / "venv-before/bin/python").is_file():
            raise ValueError("backup_interpreter_missing: " + path.name)

    def plan(self, completed=()):
        groups = {"release": [], "daily": []}
        retained = []
        transient = []
        completed = {str(Path(p)) for p in completed}
        for path in sorted(self.root.iterdir()):
            if path.name.startswith("."):
                continue
            if not path.is_dir() and not path.is_symlink():
                retained.append({"path": str(path), "reason": "unclassified_file"})
                continue
            self.safe(path)
            metadata = read_json(path / "retention.json") if (path / "retention.json").is_file() else {}
            if metadata.get("state") in {"awaiting_acceptance", "switching", "needs_repair"}:
                retained.append({"path": str(path), "reason": "not_accepted_or_failed"})
            elif metadata.get("kind") == "protected":
                retained.append(
                    {"path": str(path), "reason": "explicit_protection: " + metadata.get("reason", "")}
                )
            elif (
                re.fullmatch(r"chat-release-\d{8}-\d{6}", path.name)
                and (path / "receipt.json").exists()
                and (path / "venv-before").exists()
            ):
                groups["release"].append(path)
            elif re.fullmatch(r"daily-\d{8}-\d{6}", path.name) and (path / "manifest.json").exists():
                groups["daily"].append(path)
            elif self.transient(path) and (
                str(path) in completed or time.time() - path.stat().st_mtime >= self.ttl
            ):
                transient.append({"path": str(path), "reason": "finished_or_expired_transient"})
            else:
                retained.append({"path": str(path), "reason": "protected_or_unclassified"})
        delete = transient
        counts = {}
        for kind, keep in (("release", self.release_keep), ("daily", self.daily_keep)):
            paths = sorted(groups[kind], reverse=True)
            # 任意待保留备份损坏时，在第一次删除前终止。
            for path in paths[:keep]:
                self.validate(path, kind == "release")
                retained.append({"path": str(path), "reason": kind + "_latest_valid"})
            counts[kind] = len(paths)
            for path in paths[keep:]:
                delete.append({"path": str(path), "reason": kind + "_over_limit"})
        return {
            "policy": {
                "release_keep": self.release_keep,
                "daily_keep": self.daily_keep,
                "transient_ttl_hours": self.ttl // 3600,
            },
            "counts_before": counts,
            "delete": delete,
            "retained": retained,
        }

    def transient(self, path):
        metadata = read_json(path / "retention.json") if (path / "retention.json").is_file() else {}
        if metadata.get("kind") == "protected" or metadata.get("state") in {
            "switching",
            "awaiting_acceptance",
            "needs_repair",
        }:
            return False
        if metadata.get("kind") == "transient" and metadata.get("state") in {
            "preparing",
            "check_complete",
            "failed_restored",
            "uploaded",
        }:
            return True
        if (
            metadata.get("kind") == "daily"
            and metadata.get("state") == "preparing"
            and not (path / "manifest.json").exists()
        ):
            return True
        if re.fullmatch(r"chat-release-\d{8}-\d{6}", path.name):
            if any(
                (path / name).exists()
                for name in [
                    "source.tar.gz",
                    "manager-before.dump",
                    "venv-before",
                    "master.key",
                    "files/.env",
                ]
            ):
                return False
            if (path / "spec.json").is_file() and (path / "rehearsal.json").is_file():
                return read_json(path / "spec.json").get("revision") == read_json(
                    path / "rehearsal.json"
                ).get("revision")
        if "incoming-" in path.name:
            entries = list(path.iterdir())
            allowed = {
                "release.tar.gz",
                "update.py",
                "storage_retention.py",
                "proxy.py",
                "retention.json",
            }
            cache = path / "__pycache__"
            if cache in entries:
                if cache.is_symlink() or not cache.is_dir():
                    return False
                for generated in cache.iterdir():
                    match = re.fullmatch(
                        r"(update|storage_retention|proxy)\.cpython-\d+(?:\.opt-\d+)?\.pyc", generated.name
                    )
                    if (
                        not match
                        or generated.is_symlink()
                        or not generated.is_file()
                        or not (path / (match[1] + ".py")).is_file()
                    ):
                        return False
                entries.remove(cache)
            if not entries or any(
                not p.is_file() or p.is_symlink() or p.name not in allowed for p in entries
            ):
                return False
            bundle = path / "release.tar.gz"
            if not bundle.is_file():
                return False
            with tarfile.open(bundle) as archive:
                members = archive.getmembers()
                if any(
                    not m.isfile() or m.name.startswith("/") or ".." in Path(m.name).parts or m.name == ".env"
                    for m in members
                ):
                    return False
                if "release.json" not in {m.name for m in members}:
                    return False
                spec = json.load(archive.extractfile("release.json"))
                return bool(spec.get("revision")) and isinstance(spec.get("files"), dict)
        return False

    def recoveries(self):
        result = []
        directory = self.root / ".retention-receipts"
        if directory.is_symlink():
            raise ValueError("unsafe_receipt_directory")
        if not directory.exists():
            return result
        for receipt in directory.glob("*.json"):
            data = read_json(receipt)
            for entry in data.get("delete", []):
                tomb = Path(entry.get("tombstone", "/"))
                if (
                    tomb.parent == self.root
                    and tomb.name.startswith(".deleting-" + receipt.stem + "-")
                    and tomb.exists()
                ):
                    self.safe(tomb)
                    if self.fingerprint(tomb)[:2] != entry["fingerprint"][:2]:
                        raise ValueError("recovery_target_drift")
                    result.append({"path": str(tomb), "reason": "resume_interrupted_deletion"})
        return result

    def run(self, *, apply=False, lock=True, completed=()):
        if lock:
            with storage_lock(self.lock_path):
                return self.run(apply=apply, lock=False, completed=completed)
        result = self.plan(completed)
        result["delete"] += self.recoveries()
        finished = {str(Path(path)) for path in completed}
        for entry in result["delete"]:
            path = Path(entry["path"])
            self.deletion_safe(path, str(path) in finished)
            entry["fingerprint"] = self.fingerprint(path)
        result["deleted"] = []
        if apply:
            directory = self.root / ".retention-receipts"
            directory.mkdir(mode=0o700, exist_ok=True)
            run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
            receipt = directory / (run_id + ".json")
            for entry in result["delete"]:
                entry["tombstone"] = (
                    entry["path"]
                    if entry["reason"] == "resume_interrupted_deletion"
                    else str(self.root / (".deleting-" + run_id + "-" + Path(entry["path"]).name))
                )
            result.update(
                started_at=time.time(), status="started", free_before=shutil.disk_usage(self.root).free
            )
            save_json(receipt, result)
            for entry in result["delete"]:
                path = Path(entry["path"])
                self.deletion_safe(path, str(path) in finished)
                if self.fingerprint(path) != entry["fingerprint"]:
                    raise ValueError("cleanup_target_drift: " + path.name)
                tomb = Path(entry["tombstone"])
                if path != tomb:
                    path.rename(tomb)
                shutil.rmtree(tomb)
                result["deleted"].append(entry)
                save_json(receipt, result)
            result.update(
                status="completed", finished_at=time.time(), free_after=shutil.disk_usage(self.root).free
            )
            result["counts_after"] = self.plan()["counts_before"]
            if (
                result["counts_after"]["release"] > self.release_keep
                or result["counts_after"]["daily"] > self.daily_keep
            ):
                raise RuntimeError("retention_count_exceeded")
            save_json(receipt, result)
            # 未完成的删除回执必须保留，供下一轮恢复。
            history = sorted(directory.glob("*.json"), reverse=True)
            for index, old in enumerate(history):
                data = read_json(old)
                if data.get("status") == "started" and all(
                    not Path(e["path"]).exists() and not Path(e["tombstone"]).exists()
                    for e in data.get("delete", [])
                ):
                    data.update(status="completed", recovered_at=time.time())
                    save_json(old, data)
                if (
                    old != receipt
                    and (index >= 100 or time.time() - old.stat().st_mtime > 30 * 86400)
                    and data.get("status") == "completed"
                ):
                    old.unlink()
            result["receipt"] = str(receipt)
        return result


def configured():
    values = dotenv_values(APP / ".env")
    return Retention(
        release_keep=int(values.get("BACKUP_RELEASE_KEEP") or 0),
        daily_keep=int(values.get("BACKUP_DAILY_KEEP") or 7),
        ttl_hours=int(values.get("BACKUP_TRANSIENT_TTL_HOURS") or 24),
    )


def accept_release(path, evidence):
    """生产验收后才解除本次回滚保护；不能用旧版本回执接受新发布。"""
    path, evidence = Path(path).resolve(), Path(evidence).resolve()
    configured().safe(path)
    receipt = read_json(path / "receipt.json")
    proof = read_json(evidence)
    current = read_json(APP / "release.json")["revision"]
    if receipt["revision"] != current or proof.get("revision") != current:
        raise ValueError("acceptance_revision_mismatch")
    if not all(
        proof.get(key) is True for key in ("production_verified", "business_verified", "runtimes_verified")
    ):
        raise ValueError("production_acceptance_incomplete")
    configured().validate(path, True)
    save_json(path / "acceptance.json", proof)
    register(path, "release", "completed")
    return configured().run(apply=True, lock=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--protect", type=Path, help="登记不可轮转的关键原始备份")
    parser.add_argument("--reason", help="关键原始备份的保护原因")
    parser.add_argument("--accept-release", type=Path, help="接受已完成生产验收的发布并执行保留规则")
    parser.add_argument("--evidence", type=Path, help="与当前提交匹配的脱敏生产验收JSON")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("仅限服务器 root 运维")
    os.umask(0o077)
    if args.accept_release:
        if not args.evidence or not args.apply:
            parser.error("--accept-release 必须同时提供 --evidence --apply")
        with storage_lock():
            print(json.dumps(accept_release(args.accept_release, args.evidence), ensure_ascii=False))
        return
    if args.protect:
        if not args.reason or not args.reason.strip():
            parser.error("--protect 必须同时提供 --reason")
        with storage_lock():
            configured().safe(args.protect)
            if args.apply:
                register(args.protect, "protected", "retained", args.reason.strip())
            print(
                json.dumps(
                    {"path": str(args.protect), "reason": args.reason.strip(), "applied": args.apply},
                    ensure_ascii=False,
                )
            )
        return
    print(json.dumps(configured().run(apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
