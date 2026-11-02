"""命名迁移执行器：同版本旧布局引导发布、离线重建、可核对的失败恢复。"""

import grp
import json
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import update
from dotenv import dotenv_values
from ops_config import write
from proxy import Gateway
from rename_project import (
    PATHS,
    MoveJournal,
    fingerprint,
    migration_locks,
    replace_symlink,
    restore_runtime_entries,
    rewrite_control_state,
    rewrite_environment,
    sync_tree,
)
from storage_retention import save_json, sha

OLD = Path("/opt/wechat-manager")
NEW = Path("/opt/qingda")
SYSTEMD = Path("/etc/systemd/system")
SERVICES = (
    "web.service",
    "worker.service",
    "backup.service",
    "backup.timer",
    "certificate-renew.service",
    "certificate-renew.timer",
)
TIMERS = ("backup.timer", "certificate-renew.timer")
BUFFER = "qingda-name-migration-inbox"


class Migration:
    def __init__(self, folder):
        self.folder = Path(folder)
        if not self.folder.is_absolute() or self.folder.resolve() != self.folder or self.folder.is_symlink():
            raise ValueError("unsafe_migration_state_path")
        if any(self.folder.is_relative_to(p) for pair in PATHS for p in pair):
            raise ValueError("migration_state_must_not_move_with_project")
        self.state_file = self.folder / "state.json"
        if self.folder.exists() and (self.folder.stat().st_uid != 0 or self.folder.stat().st_mode & 0o077):
            raise ValueError("migration_state_permissions")
        self.state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.log = self.folder / "operations.log"

    def phase(self, phase, **values):
        self.state.update(values, phase=phase)
        save_json(self.state_file, self.state)
        print(json.dumps({"migration_phase": phase}), flush=True)

    def run(self, args, *, env=None, timeout=300, check=True):
        result = subprocess.run(list(map(str, args)), env=env, capture_output=True, timeout=timeout)
        with self.log.open("ab") as log:
            log.write(result.stdout + result.stderr)
        self.log.chmod(0o600)
        if check and result.returncode:
            raise RuntimeError(
                "migration_command_failed: " + str(args[0]) + "; inspect restricted operations.log"
            )
        return result.stdout

    def unit(self, verb, *names, check=True):
        return self.run(["systemctl", verb, *names], check=check)

    def preflight(self):
        for old, new in PATHS:
            if new.exists() or new.is_symlink():
                raise ValueError("target_collision: " + str(new))
            if old.is_symlink() or not old.is_dir():
                raise ValueError("source_identity_invalid: " + str(old))
        for lookup in (pwd.getpwnam, grp.getgrnam):
            try:
                lookup("qingda")
            except KeyError:
                pass
            else:
                raise ValueError("target_account_collision")
        if list(SYSTEMD.glob("qingda-*")):
            raise ValueError("target_unit_collision")
        for key, expected in (
            ("DATA_DIR", "/var/lib/wechat-manager"),
            ("MASTER_KEY_FILE", "/etc/wechat-manager/master.key"),
        ):
            if dotenv_values(OLD / ".env").get(key) != expected:
                raise ValueError("unexpected_path_configuration: " + key)
        if shutil.disk_usage(OLD).free < 2 * 1024**3:
            raise ValueError("insufficient_migration_space")
        if update.source_schema(OLD) != update.SCHEMA_VERSION:
            raise ValueError("naming_migration_requires_same_schema")
        return pwd.getpwnam("wechat-manager"), grp.getgrnam("wechat-manager")

    def snapshot(self):
        shutil.copy2(OLD / ".env", self.folder / "root-env")
        shutil.copy2("/etc/wechat-manager/master.key", self.folder / "master.key")
        shutil.copy2("/etc/nginx/nginx.conf", self.folder / "nginx.before.conf")
        units = self.folder / "units"
        units.mkdir()
        enabled, active = [], []
        for p in SYSTEMD.glob("wechat-manager-*"):
            if p.is_symlink():
                raise ValueError("unexpected_unit_symlink")
            if p.is_dir():
                shutil.copytree(p, units / p.name, symlinks=True)
            else:
                shutil.copy2(p, units / p.name)
            if p.suffix in {".service", ".timer"}:
                if self.unit("is-enabled", p.name, check=False).strip() == b"enabled":
                    enabled.append(p.name)
                if self.unit("is-active", p.name, check=False).strip() == b"active":
                    active.append(p.name)
        shutil.copytree("/var/lib/wechat-manager-acme/certbot/renewal", self.folder / "renewal")
        controls = self.folder / "controls"
        controls.mkdir()
        for name in ("outbound-paused", "domain-switch.json"):
            source = Path("/etc/wechat-manager") / name
            if source.exists():
                shutil.copy2(source, controls / name)
        base = Path("/var/lib/wechat-manager-acme/certbot")
        save_json(
            self.folder / "certificate-links.json",
            {str(p.relative_to(base)): str(p.readlink()) for p in base.rglob("*") if p.is_symlink()},
        )
        active = sorted(set(active + self.state.get("original_active_timers", [])))
        self.phase(
            "snapshotted",
            enabled=self.state.get("original_enabled", enabled),
            active=self.state.get("original_active", active),
            callback_enabled=b"location ^~ /wechatapi/callback/"
            in (self.folder / "nginx.before.conf").read_bytes(),
        )

    def pip(self, python, *args):
        return self.run(
            [python, "-m", "pip", *args],
            env={
                **os.environ,
                "PYTHONPATH": str(self.folder / "pip.whl"),
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            },
        )

    def prepare_dependencies(self):
        shutil.copy2(OLD / "deploy/pip.whl", self.folder / "pip.whl")
        shutil.copy2(OLD / "deploy/requirements.txt", self.folder / "app-requirements.txt")
        cert_python = Path("/opt/wechat-manager-acme/bin/python")
        packages = json.loads(self.pip(cert_python, "list", "--format=json"))
        (self.folder / "cert-requirements.txt").write_text(
            "".join(f"{p['name']}=={p['version']}\n" for p in packages)
        )
        for kind, python in (("app", OLD / ".venv/bin/python"), ("cert", cert_python)):
            base = self.run([python, "-c", "import sys;print(sys._base_executable)"]).decode().strip()
            self.state[kind + "_base"] = base
            self.pip(
                python,
                "download",
                "--dest",
                self.folder / (kind + "-wheels"),
                "-r",
                self.folder / (kind + "-requirements.txt"),
                *(["--require-hashes"] if kind == "app" else []),
            )
        self.build_runtime(self.folder / "buffer-venv", "app")
        shutil.copytree(
            OLD / "backend", self.folder / "backend", ignore=shutil.ignore_patterns("__pycache__", "tests")
        )
        shutil.copy2(self.folder / "root-env", self.folder / ".env")
        self.phase("dependencies_ready")
        sync_tree(self.folder)

    def build_runtime(self, path, kind):
        self.run([self.state[kind + "_base"], "-m", "venv", "--copies", "--without-pip", path])
        self.pip(
            Path(path) / "bin/python",
            "install",
            "--no-index",
            "--find-links",
            self.folder / (kind + "-wheels"),
            "-r",
            self.folder / (kind + "-requirements.txt"),
            *(["--require-hashes"] if kind == "app" else []),
        )

    def start_buffer(self):
        # 意图必须先于启动接收器/切换网关落盘；公网探针失败也要走完整恢复和回放。
        self.phase("buffer_starting", buffer_started=True)
        values = dotenv_values(self.folder / "root-env")
        python = self.folder / "buffer-venv/bin/python"
        if self.unit("is-active", BUFFER, check=False).strip() != b"active":
            self.run(
                [
                    "systemd-run",
                    "--unit=" + BUFFER,
                    "--collect",
                    "--property=TimeoutStopSec=30",
                    "--setenv=APP_ROOT=" + str(self.folder),
                    "--setenv=PYTHONPATH=" + str(self.folder / "backend"),
                    python,
                    "-m",
                    "wechat_manager.callback_buffer",
                ]
            )
        # 凭据只在受限子进程与内存中传递，绝不写入运维日志。
        code = "import os,psycopg;from wechat_manager.security import Vault;c=psycopg.connect(os.environ['DATABASE_URL']);v=c.execute('SELECT callback_key FROM users ORDER BY id LIMIT 1').fetchone();print(Vault(os.environ['MASTER_KEY_FILE']).decrypt(v[0]));c.close()"
        env = {
            **os.environ,
            **{k: v for k, v in values.items() if v is not None},
            "APP_ROOT": str(self.folder),
            "PYTHONPATH": str(self.folder / "backend"),
            "MASTER_KEY_FILE": str(self.folder / "master.key"),
        }
        result = subprocess.run([python, "-c", code], env=env, capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError("migration_callback_probe_unavailable")
        key = result.stdout.decode().strip()
        local = "http://" + values["WEB_BIND_HOST"] + ":17952/wechatapi/callback/" + key
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(local, timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("migration_buffer_not_ready") from None
                time.sleep(0.5)
        gateway = Gateway()
        current = gateway.read_bytes()
        if b"proxy_pass http://127.0.0.1:17952;" not in current:
            gateway.put(update.buffer_gateway(gateway, current))
        update.wait_buffer_callback(values["PUBLIC_URL"] + "/wechatapi/callback/" + key)
        self.phase("buffering", buffer_started=True)

    def aliases(self, create):
        for old, new in PATHS:
            if create:
                if old.exists() or old.is_symlink():
                    raise ValueError("alias_collision")
                old.symlink_to(new, target_is_directory=True)
            elif old.is_symlink():
                if old.readlink() != new:
                    raise ValueError("alias_identity_changed")
                old.unlink()

    def install_units(self):
        for suffix in SERVICES:
            source = NEW / "deploy" / ("qingda-" + suffix)
            shutil.copy2(source, SYSTEMD / source.name)
        old_drop = SYSTEMD / "wechat-manager-worker.service.d"
        if old_drop.exists():
            new_drop = SYSTEMD / "qingda-worker.service.d"
            new_drop.mkdir()
            for p in old_drop.iterdir():
                if not p.is_file() or p.is_symlink():
                    raise ValueError("unsafe_worker_dropin")
                (new_drop / p.name).write_text(p.read_text().replace("wechat-manager", "qingda"))
        for p in list(SYSTEMD.glob("wechat-manager-*")):
            if p.suffix in {".service", ".timer"}:
                self.unit("disable", p.name, check=False)
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        self.unit("daemon-reload")
        self.run(["systemd-analyze", "verify", *[SYSTEMD / ("qingda-" + name) for name in SERVICES]])

    def run_new(self, code):
        values = dotenv_values(NEW / ".env")
        env = {
            **os.environ,
            **{k: v for k, v in values.items() if v is not None},
            "APP_ROOT": str(NEW),
            "PYTHONPATH": str(NEW / "backend") + ":" + str(NEW / "deploy"),
            "QINGDA_DEPLOY_LAYOUT": "qingda",
        }
        return self.run([NEW / ".venv/bin/python", "-c", code], env=env)

    def cutover(self):
        for suffix in TIMERS:
            self.unit("stop", "wechat-manager-" + suffix)
        for suffix in ("backup.service", "certificate-renew.service"):
            if self.unit("is-active", "wechat-manager-" + suffix, check=False).strip() == b"active":
                raise RuntimeError("scheduled_operation_still_active")
        if self.state["callback_enabled"]:
            self.start_buffer()
        if not self.state["paused"] and "wechat-manager-worker.service" in self.state["active"]:
            update.drain_replies(OLD / ".venv/bin/python", OLD)
        self.phase("stopping")
        self.unit("stop", "wechat-manager-worker", "wechat-manager-web")
        self.phase("moving")
        MoveJournal(self.folder / "moves.json", PATHS).forward()
        self.aliases(True)
        self.run(["groupmod", "-n", "qingda", "wechat-manager"])
        self.run(["usermod", "-l", "qingda", "-d", "/var/lib/qingda", "wechat-manager"])
        self.phase("resources_moved")
        write(NEW / ".env", rewrite_environment((self.folder / "root-env").read_text()))
        for source in (self.folder / "controls").iterdir():
            state = json.loads(source.read_text())
            save_json(Path("/etc/qingda") / source.name, rewrite_control_state(state))
        base = Path("/var/lib/qingda-acme/certbot")
        for name, target in json.loads((self.folder / "certificate-links.json").read_text()).items():
            revised = target.replace("/var/lib/wechat-manager-acme/", "/var/lib/qingda-acme/")
            if revised != target:
                path = base / name
                replace_symlink(path, revised)
        renewal = Path("/var/lib/qingda-acme/certbot/renewal")
        for p in renewal.glob("*.conf"):
            write(p, p.read_text().replace("/var/lib/wechat-manager-acme", "/var/lib/qingda-acme"))
        # 原环境保留到验收；最终路径全新安装，避免旧 shebang 和解释器引用。
        (NEW / ".venv").rename(self.folder / "app-venv-before")
        self.build_runtime(NEW / ".venv", "app")
        cert = Path("/opt/qingda-acme")
        before = self.folder / "cert-runtime-before"
        before.mkdir()
        self.phase("building_cert_runtime", cert_entries={p.name: fingerprint(p) for p in cert.iterdir()})
        for p in list(cert.iterdir()):
            p.rename(before / p.name)
        self.build_runtime(cert, "cert")
        for path in (NEW / ".venv", cert):
            for p in [path, *path.rglob("*")]:
                if not p.is_symlink():
                    mode = p.stat().st_mode & 0o777
                    p.chmod(mode | (0o055 if p.is_dir() or mode & 0o111 else 0o044))
        self.install_units()
        self.phase("runtime_ready")
        self.unit("start", "qingda-web")
        self.run_new(
            "from proxy import health_url;from update import wait_health;wait_health(health_url(),"
            + repr(self.state["revision"])
            + ","
            + str(update.SCHEMA_VERSION)
            + ")"
        )
        before = (self.folder / "nginx.before.conf").read_text()
        Gateway().put(
            before.replace("/var/lib/wechat-manager-acme", "/var/lib/qingda-acme")
            .replace("wechat_manager_safe", "qingda_safe")
            .replace("/var/log/nginx/wechat-manager", "/var/log/nginx/qingda")
            .encode()
        )
        if self.state.get("buffer_started"):
            self.unit("stop", BUFFER)
            self.run_new(
                "import os;from wechat_manager.store import Store;from wechat_manager.callback_buffer import replay;s=Store(os.environ['DATABASE_URL']);replay(s);assert not s.query('SELECT 1 FROM migration_inbox WHERE imported=0');s.close()"
            )
        if not self.state["paused"] and "wechat-manager-worker.service" in self.state["active"]:
            self.unit("start", "qingda-worker")
            self.run_new(
                "import os;from update import wait_runtimes;wait_runtimes(os.environ['DATABASE_URL'],"
                + repr(self.state["revision"])
                + ")"
            )
        for name in self.state["enabled"]:
            if name.removeprefix("wechat-manager-") in SERVICES:
                self.unit("enable", name.replace("wechat-manager-", "qingda-", 1))
        if "wechat-manager-web.service" not in self.state["active"]:
            self.unit("stop", "qingda-web")
        self.phase("services_started")
        self.aliases(False)
        manifest = json.loads((NEW / "release.json").read_text())
        if manifest["revision"] != self.state["revision"] or any(
            sha(NEW / n) != h for n, h in manifest["files"].items()
        ):
            raise RuntimeError("final_release_hash_mismatch")
        if (
            pwd.getpwnam("qingda").pw_uid != self.state["uid"]
            or grp.getgrnam("qingda").gr_gid != self.state["gid"]
        ):
            raise RuntimeError("account_identity_changed")
        self.phase("completed", final_manifest_sha256=sha(NEW / "release.json"))
        save_json(
            Path("/etc/qingda/name-migration.json"),
            {"revision": self.state["revision"], "state": str(self.folder), "completed": True},
        )

    def rollback(self):
        if not (self.folder / "moves.json").exists():
            if any(
                not old.is_dir() or old.is_symlink() or new.exists() or new.is_symlink() for old, new in PATHS
            ):
                raise ValueError("missing_journal_with_resource_changes")
            current = json.loads((OLD / "release.json").read_text())
            update.wait_health(
                "http://127.0.0.1:17950/internal/health", current["revision"], update.SCHEMA_VERSION
            )
            if "wechat-manager-web.service" not in self.state.get("original_active", []):
                self.unit("stop", "wechat-manager-web")
            self.phase("rolled_back_before_resource_changes")
            return
        if self.state["phase"] in {"snapshotted", "dependencies_ready"}:
            Gateway().put((self.folder / "nginx.before.conf").read_bytes())
            self.unit("stop", BUFFER, check=False)
            if "wechat-manager-web.service" not in self.state["active"]:
                self.unit("stop", "wechat-manager-web")
            self.phase("rolled_back_before_resource_changes")
            return
        if self.state.get("callback_enabled"):
            self.start_buffer()
        for prefix in ("qingda", "wechat-manager"):
            self.unit(
                "stop",
                *[prefix + "-" + suffix for suffix in (*TIMERS, "worker.service", "web.service")],
                check=False,
            )
        self.phase("restoring")
        self.aliases(False)
        # 用 inode 日志恢复目录，即使进程在某次 rename 之后被中断。
        MoveJournal(self.folder / "moves.json", PATHS).backward()
        try:
            account = pwd.getpwnam("qingda")
        except KeyError:
            account = None
        if account:
            if account.pw_uid != self.state["uid"]:
                raise ValueError("account_identity_changed")
            self.run(["usermod", "-l", "wechat-manager", "-d", "/var/lib/wechat-manager", "qingda"])
        try:
            group = grp.getgrnam("qingda")
        except KeyError:
            group = None
        if group:
            if group.gr_gid != self.state["gid"]:
                raise ValueError("group_identity_changed")
            self.run(["groupmod", "-n", "wechat-manager", "qingda"])
        for saved, target in ((self.folder / "app-venv-before", OLD / ".venv"),):
            if saved.exists():
                if target.exists():
                    target.rename(self.folder / ("failed-runtime-" + str(time.time_ns())))
                saved.rename(target)
        cert_saved = self.folder / "cert-runtime-before"
        if cert_saved.exists():
            cert = Path("/opt/wechat-manager-acme")
            failed = self.folder / ("failed-cert-runtime-" + str(time.time_ns()))
            restore_runtime_entries(cert_saved, cert, self.state.get("cert_entries", {}), failed)
        shutil.copy2(self.folder / "root-env", OLD / ".env")
        for p in (self.folder / "controls").iterdir():
            shutil.copy2(p, Path("/etc/wechat-manager") / p.name)
        base = Path("/var/lib/wechat-manager-acme/certbot")
        for name, target in json.loads((self.folder / "certificate-links.json").read_text()).items():
            path = base / name
            if not path.is_symlink() or path.readlink() != Path(target):
                replace_symlink(path, target)
        for p in (self.folder / "renewal").iterdir():
            shutil.copy2(p, Path("/var/lib/wechat-manager-acme/certbot/renewal") / p.name)
        for p in list(SYSTEMD.glob("qingda-*")):
            if p.suffix in {".service", ".timer"}:
                self.unit("disable", p.name, check=False)
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        for p in (self.folder / "units").iterdir():
            target = SYSTEMD / p.name
            if p.is_dir():
                shutil.copytree(p, target, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(p, target)
        self.unit("daemon-reload")
        self.unit("start", "wechat-manager-web")
        update.wait_health(
            "http://127.0.0.1:17950/internal/health", self.state["revision"], update.SCHEMA_VERSION
        )
        Gateway().put((self.folder / "nginx.before.conf").read_bytes())
        self.unit("stop", BUFFER, check=False)
        values = dotenv_values(OLD / ".env")
        update.run_python(
            OLD / ".venv/bin/python",
            OLD,
            values["DATABASE_URL"],
            "import os;from wechat_manager.store import Store;from wechat_manager.callback_buffer import replay;s=Store(os.environ['DATABASE_URL']);replay(s);s.close()",
        )
        for name in self.state["enabled"]:
            self.unit("enable", name)
        for name in self.state["active"]:
            if not name.endswith(".timer"):
                self.unit("start", name)
        if "wechat-manager-web.service" not in self.state["active"]:
            self.unit("stop", "wechat-manager-web")
        self.phase("rolled_back")


def execute(args):
    migration = Migration(args.state)
    if args.check:
        migration.preflight()
        print(json.dumps({"preflight": True, "resource_pairs": len(PATHS)}))
        return
    restore_timers = False
    try:
        with migration_locks():
            if args.rollback:
                restore_timers = True
                try:
                    migration.rollback()
                except BaseException:
                    migration.phase("needs_repair")
                    raise
                return
            if migration.state:
                if migration.state.get("phase") == "completed":
                    if sha(NEW / "release.json") != migration.state["final_manifest_sha256"]:
                        raise ValueError("completed_migration_revision_drift")
                    print(json.dumps({"already_completed": True, "revision": migration.state["revision"]}))
                    return
                raise ValueError("interrupted_migration: inspect state and use --rollback before retry")
            user, group = migration.preflight()
            if not args.bundle or not args.sha256 or sha(args.bundle) != args.sha256:
                raise ValueError("verified_bundle_required")
            args.state.mkdir(mode=0o700, parents=True, exist_ok=False)
            os.umask(0o077)
            migration.phase(
                "bootstrap_starting",
                uid=user.pw_uid,
                gid=group.gr_gid,
                paused=Path("/etc/wechat-manager/outbound-paused").exists(),
            )
            original_enabled, original_active = [], []
            for suffix in SERVICES:
                name = "wechat-manager-" + suffix
                if migration.unit("is-enabled", name, check=False).strip() == b"enabled":
                    original_enabled.append(name)
                if migration.unit("is-active", name, check=False).strip() == b"active":
                    original_active.append(name)
            migration.phase(
                "bootstrap_starting", original_enabled=original_enabled, original_active=original_active
            )
            active_timers = [
                "wechat-manager-" + suffix
                for suffix in TIMERS
                if migration.unit("is-active", "wechat-manager-" + suffix, check=False).strip() == b"active"
            ]
            migration.phase("bootstrap_starting", original_active_timers=active_timers)
            restore_timers = True
            for name in active_timers:
                migration.unit("stop", name)
            try:
                update.release(
                    args.bundle, args.sha256, worker_active="wechat-manager-worker.service" in original_active
                )
                revision = json.loads((OLD / "release.json").read_text())["revision"]
                migration.phase("bootstrap_complete", revision=revision)
                migration.snapshot()
                MoveJournal(args.state / "moves.json", PATHS).prepare()
                migration.prepare_dependencies()
                migration.cutover()
            except BaseException:
                try:
                    migration.rollback()
                except BaseException:
                    migration.phase("needs_repair")
                raise
    finally:
        if restore_timers and migration.state.get("phase") in {
            "completed",
            "rolled_back",
            "rolled_back_before_resource_changes",
        }:
            prefix = "qingda-" if migration.state["phase"] == "completed" else "wechat-manager-"
            for name in migration.state.get("original_active_timers", []):
                migration.unit("start", name.replace("wechat-manager-", prefix, 1))
