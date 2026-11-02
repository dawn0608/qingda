"""已冻结发布包的一次性项目身份切换；正常发布不得调用此入口。"""

import argparse
import base64
import hashlib
import importlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import urlopen

import psycopg
from dotenv import dotenv_values
from identity_database import RenameJournal, save
from identity_spool import SCHEMA, prepare_receipts, replay, sync_directory
from psycopg import sql

APP = Path("/opt/qingda")
CONFIG = Path("/etc/qingda")
ROOT = Path("/var/backups/qingda")
DATA = Path("/var/lib/qingda-identity")
OLD = "wechat_manager"
NEW = "qingda"
TIMERS = ["qingda-backup.timer", "qingda-certificate-renew.timer"]
SERVICES = ["qingda-web.service", "qingda-worker.service"]
BUFFER = "qingda-identity-buffer.service"


@contextmanager
def peer(database="postgres", port=5432):
    before = os.geteuid()
    try:
        os.seteuid(pwd.getpwnam("postgres").pw_uid)
        conn = psycopg.connect(
            dbname=database, user="postgres", host="/var/run/postgresql", port=port, autocommit=True
        )
    finally:
        os.seteuid(before)
    try:
        yield conn
    finally:
        conn.close()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze_gateway(before, paused):
    body, count = re.subn(rb"(location\s+\^~\s+/wechat/\s*\{)", rb"\1\n  return 503;", before)
    if count != 1 and not (paused and count == 0):
        raise ValueError("unknown_gateway_layout")
    return body


def redirect_callback(before):
    body, count = re.subn(
        rb"(location\s+\^~\s+/wechatapi/callback/\s*\{[^}]*?proxy_pass\s+http://[^:;]+:)(17950)(;)",
        rb"\g<1>17952\3",
        before,
        flags=re.S,
    )
    if count != 1:
        raise ValueError("unknown_callback_gateway")
    return body


def verify_candidate(spec, candidate):
    if spec.get("project_identity") != NEW or not spec.get("revision"):
        raise ValueError("project_identity_mismatch")
    for name, digest in spec["files"].items():
        path = candidate / name
        if path.is_symlink() or not path.is_file() or sha(path) != digest:
            raise ValueError("candidate_checksum_mismatch")


def renamed_url(url):
    parsed = urlsplit(url)
    if unquote(parsed.username or "") != OLD + "_app" or parsed.path != "/" + OLD:
        raise ValueError("unexpected_original_database_url")
    auth, host = parsed.netloc.rsplit("@", 1)
    _, password = auth.split(":", 1)
    return urlunsplit(parsed._replace(netloc="qingda_app:" + password + "@" + host, path="/qingda"))


def ensure_receipt_access(conn, role, schema=SCHEMA):
    # 建表与授权同事务；每次恢复都修复部分 DDL 的旧中断状态。
    with conn.transaction():
        prepare_receipts(conn, schema)
        conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role))
        )
        conn.execute(
            sql.SQL("GRANT SELECT,INSERT ON {}.callback_imports TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )


def protect_history(root, manifest):
    if manifest.exists():
        entries = json.loads(manifest.read_text())
    else:
        entries = []
        for directory in sorted(root.iterdir()):
            metadata = directory / "retention.json"
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not metadata.is_file()
                or metadata.is_symlink()
            ):
                continue
            body = metadata.read_bytes()
            value = json.loads(body)
            if value.get("kind") not in {"daily", "release", "transient"}:
                continue
            updated = {
                **value,
                "kind": "protected",
                "reason": "旧身份历史原件，退出新格式轮转；原保留元数据见独立迁移清单",
            }
            entries.append(
                {"name": directory.name, "before": base64.b64encode(body).decode(), "after": updated}
            )
        save(manifest, entries)
    for item in entries:
        metadata = root / item["name"] / "retention.json"
        current = metadata.read_bytes()
        if current != base64.b64decode(item["before"]) and json.loads(current) != item["after"]:
            raise ValueError("historical_retention_changed")
        save(metadata, item["after"])


def restore_history(root, manifest):
    if not manifest.exists():
        return
    for item in json.loads(manifest.read_text()):
        path = root / item["name"] / "retention.json"
        original = base64.b64decode(item["before"])
        if path.read_bytes() == original:
            continue
        if json.loads(path.read_bytes()) != item["after"]:
            raise ValueError("historical_retention_changed")
        temporary = path.with_suffix(".identity-tmp")
        with temporary.open("wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        sync_directory(path.parent)


class Cutover:
    def __init__(self, folder):
        self.folder = Path(folder).absolute()
        if self.folder.parent != ROOT or self.folder.is_symlink() or self.folder != self.folder.resolve():
            raise ValueError("state_must_be_direct_project_backup_child")
        self.private = self.folder / "private"
        self.state_path = self.folder / "state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.candidate = self.folder / "candidate"
        self.receiver = DATA / self.folder.name
        self.spool = self.receiver / "spool"
        self.log = self.folder / "operations.log"

    def run(self, args, *, env=None, data=None, timeout=120, check=True):
        result = subprocess.run(
            list(map(str, args)), env=env, input=data, capture_output=True, timeout=timeout
        )
        if result.returncode and check:
            with self.log.open("ab") as stream:
                stream.write(result.stdout + b"\n" + result.stderr + b"\n")
            self.log.chmod(0o600)
            raise RuntimeError("operation_failed:" + str(args[0]))
        return result

    def unit(self, action, *names, check=True):
        return self.run(["systemctl", action, *names], check=check).stdout.strip()

    def stage(self, phase, **values):
        self.state.update(phase=phase, **values)
        save(self.state_path, self.state)
        print(json.dumps({"phase": phase}), flush=True)

    def check_original(self):
        before = self.state["before"]
        if (
            sha(APP / "release.json") != before["manifest_sha256"]
            or sha(APP / ".env") != before["env_sha256"]
        ):
            raise ValueError("production_baseline_changed")
        for name, digest in before["files"].items():
            p = APP / name
            if p.is_symlink() or not p.is_file() or sha(p) != digest:
                raise ValueError("production_source_changed")
        for name, digest in self.state["candidate"]["before"].items():
            target = APP / name
            if target.is_symlink() or not target.resolve().is_relative_to(APP):
                raise ValueError("unsafe_candidate_target")
            if (sha(target) if target.is_file() else None) != digest:
                raise ValueError("candidate_target_changed")
        if sha("/etc/nginx/nginx.conf") != before["nginx_sha256"]:
            raise ValueError("production_gateway_changed")

    def prepare(self, bundle, digest):
        if self.state:
            if self.state["phase"] == "prepared":
                self.check_original()
                verify_candidate(self.state["candidate"], self.candidate)
                return
            raise ValueError("existing_cutover_state")
        self.folder.mkdir(mode=0o700)
        self.private.mkdir(mode=0o700)
        save(
            self.folder / "retention.json",
            {"kind": "protected", "state": "preparing", "reason": "项目身份切换及独立恢复材料"},
        )
        self.candidate.mkdir(mode=0o755)
        if sha(bundle) != digest:
            raise ValueError("bundle_checksum")
        with tarfile.open(bundle) as archive:
            members = archive.getmembers()
            if any(
                not m.isfile() or Path(m.name).is_absolute() or ".." in Path(m.name).parts for m in members
            ):
                raise ValueError("unsafe_bundle")
            if len({m.name for m in members}) != len(members):
                raise ValueError("duplicate_bundle_path")
            spec = json.load(archive.extractfile("release.json"))
            if {m.name for m in members} != set(spec["files"]) | {"release.json"}:
                raise ValueError("bundle_manifest")
            archive.extractall(self.candidate)
        verify_candidate(spec, self.candidate)
        old = json.loads((APP / "release.json").read_text())
        if old.get("project_identity"):
            raise ValueError("production_already_named")
        self.state = {
            "candidate": spec,
            "batch": self.folder.name,
            "before": {
                "revision": old["revision"],
                "files": old["files"],
                "manifest_sha256": sha(APP / "release.json"),
                "env_sha256": sha(APP / ".env"),
                "nginx_sha256": sha("/etc/nginx/nginx.conf"),
            },
        }
        if spec.get("expected_manifest_sha256") != self.state["before"]["manifest_sha256"]:
            raise ValueError("candidate_baseline_changed")
        self.check_original()
        if self.unit("show", BUFFER, "--property=LoadState", "--value", check=False) not in (
            b"not-found",
            b"",
        ):
            raise ValueError("buffer_unit_collision")
        version_code = "import json,sys;print(json.dumps(list(sys.version_info[:3])))"
        if (
            self.run([APP / ".venv/bin/python", "-c", version_code]).stdout
            != self.run(["/usr/bin/python3.10", "-c", version_code]).stdout
        ):
            raise ValueError("python_runtime_version_changed")
        if not DATA.exists():
            DATA.mkdir(mode=0o750)
            os.chown(DATA, 0, pwd.getpwnam("qingda").pw_gid)
            DATA.chmod(0o750)
        if DATA.is_symlink() or DATA.stat().st_uid != 0 or DATA.stat().st_mode & 0o022:
            raise ValueError("unsafe_receiver_parent")
        self.receiver.mkdir(mode=0o755)
        self.spool.mkdir(mode=0o700)
        uid = pwd.getpwnam("qingda").pw_uid
        gid = pwd.getpwnam("qingda").pw_gid
        os.chown(self.spool, uid, gid)
        tools = self.folder / "tools"
        tools.mkdir(mode=0o755)
        tools.chmod(0o755)
        for name in ["identity_cutover.py", "identity_database.py", "identity_spool.py"]:
            shutil.copy2(Path(__file__).with_name(name), tools / name)
            (tools / name).chmod(0o644)
        shutil.copytree(APP, self.private / "app-before", ignore=shutil.ignore_patterns(".venv"))
        shutil.copytree(CONFIG, self.private / "config-before")
        shutil.copy2(APP / ".env", self.private / "root-env")
        shutil.copy2("/etc/nginx/nginx.conf", self.private / "nginx.before.conf")
        (self.private / "units").mkdir()
        for name in SERVICES + TIMERS:
            shutil.copy2(Path("/etc/systemd/system") / name, self.private / "units" / name)
        with peer() as c:
            roles = c.execute(
                "SELECT oid,rolname FROM pg_roles WHERE starts_with(rolname,%s) ORDER BY rolname",
                (OLD + "_",),
            ).fetchall()
            if {r[1] for r in roles} != {
                OLD + "_" + suffix for suffix in ["app", "admin", "migration", "owner"]
            }:
                raise ValueError("unexpected_project_roles")
            dbs = c.execute(
                "SELECT oid,datname,datdba FROM pg_database WHERE datname=%s OR starts_with(datname,%s) ORDER BY datname",
                (OLD, OLD + "_"),
            ).fetchall()
            if not dbs or any(d[2] not in [r[0] for r in roles] for d in dbs):
                raise ValueError("database_ownership_mismatch")
            if c.execute(
                "SELECT 1 FROM pg_db_role_setting WHERE setrole=ANY(%s) AND array_to_string(setconfig,',') LIKE %s",
                ([r[0] for r in roles], "%" + OLD + "%"),
            ).fetchone():
                raise ValueError("role_setting_requires_explicit_review")
            self.state["database_pairs"] = [(d[1], d[1].replace(OLD, NEW, 1)) for d in dbs]
            self.state["role_pairs"] = [(r[1], r[1].replace(OLD, NEW, 1)) for r in roles]
            journal = self.journal(c)
            journal.prepare()
            self.state["memberships"] = c.execute(
                "SELECT roleid,member,grantor,admin_option,inherit_option,set_option FROM pg_auth_members WHERE roleid=ANY(%s) OR member=ANY(%s) ORDER BY roleid,member",
                ([r[0] for r in roles], [r[0] for r in roles]),
            ).fetchall()
        self.run(
            ["/usr/bin/python3.10", "-m", "venv", "--without-pip", "--copies", self.folder / "build-venv"]
        )
        (self.folder / "wheels").mkdir()
        self.run(
            [
                self.folder / "build-venv/bin/python",
                "-m",
                "pip",
                "wheel",
                "--index-url",
                "https://pypi.org/simple",
                "--require-hashes",
                "--no-deps",
                "-r",
                self.candidate / "deploy/requirements.txt",
                "--wheel-dir",
                self.folder / "wheels",
            ],
            env=self.pip_env(),
            timeout=600,
        )
        # 离线清单使用绝对路径；环境均在最终使用路径创建。
        (self.folder / "offline.txt").write_text(
            "".join(
                str(p) + " --hash=sha256:" + sha(p) + "\n"
                for p in sorted((self.folder / "wheels").glob("*.whl"))
            )
        )
        shutil.copy2(tools / "identity_spool.py", self.receiver / "identity_spool.py")
        self.install_runtime(self.receiver / "venv")
        packages_code = "import importlib.metadata as m,json;print(json.dumps(sorted((d.metadata['Name'].lower().replace('_','-'),d.version) for d in m.distributions() if d.metadata['Name'].lower().replace('_','-') not in {'qingda','wechat-manager'})))"
        if (
            self.run([APP / ".venv/bin/python", "-c", packages_code]).stdout
            != self.run([self.receiver / "venv/bin/python", "-c", packages_code]).stdout
        ):
            raise ValueError("locked_dependency_versions_changed")
        for path in [self.receiver, *self.receiver.rglob("*")]:
            if path == self.spool or path.is_symlink():
                continue
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        for path in [self.folder, self.candidate, *self.candidate.rglob("*")]:
            if path == self.folder:
                continue
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        for name, digest in self.state["before"]["files"].items():
            if sha(self.private / "app-before" / name) != digest:
                raise ValueError("frozen_original_source_changed")
        if sha(self.private / "root-env") != self.state["before"]["env_sha256"]:
            raise ValueError("frozen_original_environment_changed")
        self.state["tools_hashes"] = {p.name: sha(p) for p in tools.glob("*.py")}
        self.run(
            [
                "runuser",
                "-u",
                "qingda",
                "--",
                self.receiver / "venv/bin/python",
                "-c",
                "import os,sys,psycopg;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import identity_spool;assert os.access(sys.argv[2],os.W_OK)",
                str(self.receiver),
                str(self.spool),
            ]
        )
        self.state["receiver_path"] = str(self.receiver)
        self.stage("prepared")

    def journal(self, conn):
        return RenameJournal(
            conn, self.private / "names.json", self.state["database_pairs"], self.state["role_pairs"]
        )

    def admin_action(self, action):
        if action in {"forward", "connections", "backward"}:
            with peer() as c:
                journal = self.journal(c)
                getattr(journal, {"connections": "restore_connections"}.get(action, action))()
                role_oids = [x["oid"] for x in journal.state["roles"]]
                members = c.execute(
                    "SELECT roleid,member,grantor,admin_option,inherit_option,set_option FROM pg_auth_members WHERE roleid=ANY(%s) OR member=ANY(%s) ORDER BY roleid,member",
                    (role_oids, role_oids),
                ).fetchall()
                if [list(r) for r in members] != [list(r) for r in self.state["memberships"]]:
                    raise ValueError("role_membership_changed")
            return
        if action == "allowlist":
            with peer(OLD) as c:
                rows = c.execute("SELECT callback_key_hash,id FROM users ORDER BY id").fetchall()
            path = self.receiver / "allowlist.json"
            path.write_text(json.dumps(dict(rows)))
            path.chmod(0o640)
            os.chown(path, 0, pwd.getpwnam("qingda").pw_gid)
            return
        if action == "backup":
            target = self.private / "qingda-before.dump"
            with target.open("wb") as output:
                subprocess.run(
                    [
                        "runuser",
                        "-u",
                        "postgres",
                        "--",
                        "pg_dump",
                        "-d",
                        OLD,
                        "--format=custom",
                        "--no-owner",
                        "--no-acl",
                    ],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                output.flush()
                os.fsync(output.fileno())
            target.chmod(0o600)
            self.run(["pg_restore", "--list", target])
            with peer(OLD) as c:
                ensure_receipt_access(c, OLD + "_migration")
            return
        if action == "replay":
            restoring = self.state.get("restoring", False)
            package = OLD if restoring else NEW
            source = self.private / "app-before" if restoring else APP
            sys.path.insert(0, str(source / "backend"))
            module = importlib.import_module(package + ".store")
            values = json.loads((CONFIG / "postgres-credentials.json").read_text())
            url = f"postgresql://{package}_migration:{quote(values['migration'], safe='')}@127.0.0.1:5432/{package}"
            with peer(package) as admin:
                ensure_receipt_access(admin, package + "_migration")
            store = module.Store(url)
            try:
                with psycopg.connect(url, autocommit=True, row_factory=module.record_factory) as c:
                    module.configure(c)

                    def receive(conn, record):
                        conn.execute("SELECT set_config('app.user_id',%s,true)", (str(record["user_id"]),))
                        store.receive(
                            base64.b64decode(record["raw"]),
                            now=record["received_at"],
                            conn=conn,
                            raw_digest=record["sha256"],
                        )

                    replay(self.spool, c, self.state["batch"], receive)
            finally:
                store.close()
            return
        raise ValueError("unknown_admin_action")

    def pip_env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
        env.update(
            PIP_CONFIG_FILE="/dev/null",
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            PYTHONPATH=str(self.candidate / "deploy/pip.whl"),
        )
        return env

    def install_runtime(self, path):
        self.run(["/usr/bin/python3.10", "-m", "venv", "--without-pip", "--copies", path])
        self.run(
            [
                path / "bin/python",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--require-hashes",
                "--find-links",
                self.folder / "wheels",
                "-r",
                self.folder / "offline.txt",
            ],
            env=self.pip_env(),
            timeout=300,
        )
        self.run([path / "bin/python", "-m", "pip", "check"], env=self.pip_env())

        for item in [path, *path.rglob("*")]:
            if not item.is_symlink():
                mode = item.stat().st_mode & 0o777
                item.chmod(mode | (0o055 if item.is_dir() or mode & 0o111 else 0o044))

    def gateway(self, body):
        p = Path("/etc/nginx/nginx.conf")
        old = p.read_bytes()
        workers = self.run(["pgrep", "-f", "^nginx: worker process"], check=False).stdout.split()

        def put(data):
            with p.open("wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            self.run(["nginx", "-t"])
            self.run(["nginx", "-s", "reload"])

        try:
            put(body)
        except Exception:
            put(old)
            raise
        deadline = time.monotonic() + 90
        while any((Path("/proc") / pid.decode()).exists() for pid in workers):
            if time.monotonic() > deadline:
                raise RuntimeError("nginx_inflight_requests_not_drained")
            time.sleep(0.1)

    def freeze(self):
        self.state["active"] = [
            u for u in SERVICES + TIMERS if self.unit("is-active", u, check=False) == b"active"
        ]
        self.state["paused"] = (CONFIG / "outbound-paused").exists()
        self.state["callback_enabled"] = (
            b"location ^~ /wechatapi/callback/" in (self.private / "nginx.before.conf").read_bytes()
        )
        self.stage("freezing")
        protect_history(ROOT, self.private / "historical-retention.json")
        self.unit("stop", *TIMERS)
        self.gateway(freeze_gateway((self.private / "nginx.before.conf").read_bytes(), self.state["paused"]))
        self.stage("writes_frozen")

    def start_buffer(self):
        if not self.state["callback_enabled"]:
            return
        self.admin_action("allowlist")
        self.stage("buffer_intent")
        self.run(
            [
                "systemd-run",
                "--collect",
                "--unit=" + BUFFER,
                "--property=User=qingda",
                "--property=Group=qingda",
                "--property=UMask=0077",
                "--property=KillMode=mixed",
                "--property=TimeoutStopSec=40",
                "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes",
                self.receiver / "venv/bin/python",
                self.receiver / "identity_spool.py",
                self.spool,
                self.receiver / "allowlist.json",
            ]
        )
        self.wait_callback("http://127.0.0.1:17952", "qingda-cutover-buffer")
        self.gateway(
            redirect_callback(
                freeze_gateway((self.private / "nginx.before.conf").read_bytes(), self.state["paused"])
            )
        )
        self.wait_callback(dotenv_values(APP / ".env")["PUBLIC_URL"], "qingda-cutover-buffer")
        self.stage("buffering")

    def wait_callback(self, base, header):
        with peer(OLD if not self.state.get("names_new") else NEW) as c:
            row = c.execute("SELECT callback_key FROM users ORDER BY id LIMIT 1").fetchone()
        from cryptography.fernet import Fernet

        key = Fernet((CONFIG / "master.key").read_bytes()).decrypt(row[0].encode()).decode() if row else ""
        if not key:
            return
        url = base.rstrip("/") + "/wechatapi/callback/" + key
        end = time.monotonic() + 12
        while time.monotonic() < end:
            try:
                with urlopen(url, timeout=3) as r:
                    if r.status == 200 and r.headers.get("X-Callback-Receiver") == header:
                        return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError("callback_probe_failed")

    def finish_buffer(self):
        if self.state["callback_enabled"]:
            self.unit("stop", BUFFER)
            if self.unit("show", BUFFER, "--property=MainPID", "--value", check=False) not in (b"0", b""):
                raise RuntimeError("buffer_not_stopped")
            self.admin_action("replay")

    def apply(self):
        if self.state.get("phase") == "completed":
            verify_candidate(self.state["candidate"], APP)
            return
        if self.state.get("phase") != "prepared":
            raise ValueError("recover_interrupted_cutover_first")
        self.check_original()
        self.freeze()
        self.start_buffer()
        if not self.state["paused"] and "qingda-worker.service" in self.state["active"]:
            code = (
                "import sys;sys.path.insert(0,"
                + repr(str(self.private / "app-before/deploy"))
                + ");from update import drain_replies;from pathlib import Path;drain_replies(Path('/opt/qingda/.venv/bin/python'),Path('/opt/qingda'))"
            )
            self.run([APP / ".venv/bin/python", "-c", code], timeout=330)
        self.stage("stopping")
        self.unit("stop", *SERVICES)
        self.admin_action("backup")
        self.stage("renaming")
        self.admin_action("forward")
        self.state["names_new"] = True
        self.stage("installing")
        self.install_app()
        self.admin_action("connections")
        self.stage("starting")
        self.unit("start", "qingda-web.service")
        self.health()
        self.admin_action("replay")
        self.gateway((self.private / "nginx.before.conf").read_bytes().replace(b"wechat-manager", b"qingda"))
        self.finish_buffer()
        if not self.state["paused"] and "qingda-worker.service" in self.state["active"]:
            self.unit("start", "qingda-worker.service")
        if "qingda-web.service" not in self.state["active"]:
            self.unit("stop", "qingda-web.service")
        self.stage("completed")

    def health(self):
        end = time.monotonic() + 45
        while time.monotonic() < end:
            try:
                with urlopen("http://127.0.0.1:17950/internal/health", timeout=3) as response:
                    body = json.load(response)
                if body.get("database_ready") and body.get("revision") == self.state["candidate"]["revision"]:
                    return
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError("new_web_health_failed")

    def install_app(self):
        old_venv = APP / ".venv"
        if old_venv.is_symlink():
            raise ValueError("unsafe_runtime")
        if not (self.private / "venv-before").exists():
            os.rename(old_venv, self.private / "venv-before")
            sync_directory(APP)
            sync_directory(self.private)
        old_backend = APP / "backend" / OLD
        if old_backend.exists():
            os.rename(old_backend, self.private / "backend-before")
            sync_directory(APP / "backend")
            sync_directory(self.private)
        self.install_runtime(old_venv)
        spec = self.state["candidate"]
        for name in set(self.state["before"]["files"]) - set(spec["files"]):
            p = APP / name
            if p.is_file():
                p.unlink()
        for name in spec["files"]:
            source = self.candidate / name
            target = APP / name
            missing = []
            parent = target.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            for directory in missing:
                directory.chmod(0o755)
            shutil.copyfile(source, target)
            target.chmod(0o644)
        # 清除已退役运行入口的字节码；源码原件已冻结。
        for p in (APP / "deploy").rglob("__pycache__"):
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
        before = dotenv_values(self.private / "root-env")
        body = (self.private / "root-env").read_text()
        body = re.sub(
            r"(?m)^DATABASE_URL=.*$", lambda _: "DATABASE_URL=" + renamed_url(before["DATABASE_URL"]), body
        )
        body = re.sub(r"(?m)^APP_REVISION=.*$", lambda _: "APP_REVISION=" + spec["revision"], body)
        self.atomic(APP / ".env", body.encode())
        self.atomic(
            APP / "release.json",
            json.dumps({**spec, "previous_revision": self.state["before"]["revision"]}, indent=2).encode(),
            0o644,
        )
        for name in SERVICES:
            self.atomic(
                Path("/etc/systemd/system") / name, (self.candidate / "deploy" / name).read_bytes(), 0o644
            )
        self.unit("daemon-reload")

    def atomic(self, path, body, mode=None):
        previous = path.stat() if path.exists() else None
        tmp = path.with_name(path.name + ".identity-tmp")
        with tmp.open("wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        tmp.chmod(mode if mode is not None else previous.st_mode & 0o777)
        if previous:
            os.chown(tmp, previous.st_uid, previous.st_gid)
        os.replace(tmp, path)
        sync_directory(path.parent)

    def restore(self):
        if self.state.get("phase") == "prepared":
            self.check_original()
            self.stage("cancelled_before_switch")
            return
        if not self.state.get("active") and "active" not in self.state:
            raise ValueError("missing_original_service_state")
        self.state["restoring"] = True
        self.stage("restoring")
        self.unit("stop", *TIMERS)
        # 在数据库可用时恢复缓冲；缓冲已在运行则保持原进程及文件。
        if self.state["callback_enabled"]:
            if self.unit("is-active", BUFFER, check=False) != b"active":
                if not (self.receiver / "allowlist.json").exists():
                    self.admin_action("allowlist")
                self.unit("reset-failed", BUFFER, check=False)
                self.run(
                    [
                        "systemd-run",
                        "--collect",
                        "--unit=" + BUFFER,
                        "--property=User=qingda",
                        "--property=Group=qingda",
                        "--property=UMask=0077",
                        "--property=KillMode=mixed",
                        "--property=TimeoutStopSec=40",
                        self.receiver / "venv/bin/python",
                        self.receiver / "identity_spool.py",
                        self.spool,
                        self.receiver / "allowlist.json",
                    ]
                )
            self.gateway(
                redirect_callback(
                    freeze_gateway((self.private / "nginx.before.conf").read_bytes(), self.state["paused"])
                )
            )
        self.unit("stop", *SERVICES)
        self.admin_action("backward")
        self.state["names_new"] = False
        old_env = self.private / "venv-before"
        if old_env.exists():
            if (APP / ".venv").exists():
                failed = self.private / ("failed-runtime-" + str(time.time_ns()))
                os.rename(APP / ".venv", failed)
            os.rename(old_env, APP / ".venv")
            sync_directory(APP)
            sync_directory(self.private)
        if (self.private / "backend-before").exists():
            if (APP / "backend" / NEW).exists():
                os.rename(APP / "backend" / NEW, self.private / ("failed-backend-" + str(time.time_ns())))
            if (APP / "backend" / OLD).exists():
                raise ValueError("restore_backend_collision")
            os.rename(self.private / "backend-before", APP / "backend" / OLD)
            sync_directory(APP / "backend")
            sync_directory(self.private)
        source = self.private / "app-before"
        for name in set(self.state["candidate"]["files"]) - set(self.state["before"]["files"]):
            path = APP / name
            if path.is_file():
                path.unlink()
        for name in set(self.state["before"]["files"]) | {".env", "release.json"}:
            target = APP / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        for name in SERVICES + TIMERS:
            shutil.copy2(self.private / "units" / name, Path("/etc/systemd/system") / name)
        self.unit("daemon-reload")
        self.unit("start", "qingda-web.service")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                with urlopen("http://127.0.0.1:17950/internal/health", timeout=3) as response:
                    body = json.load(response)
                if body.get("database_ready") and body.get("revision") == self.state["before"]["revision"]:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("restored_web_health_failed")
        self.admin_action("replay")
        self.gateway((self.private / "nginx.before.conf").read_bytes())
        self.finish_buffer()
        if not self.state["paused"] and "qingda-worker.service" in self.state["active"]:
            self.unit("start", "qingda-worker.service")
        if "qingda-web.service" not in self.state["active"]:
            self.unit("stop", "qingda-web.service")
        restore_history(ROOT, self.private / "historical-retention.json")
        self.stage("reversed")


@contextmanager
def exclusive_release(path=Path("/run/lock/qingda-release.lock")):
    import fcntl

    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("storage_operation_in_progress") from None
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--apply", action="store_true")
    actions.add_argument("--rollback", action="store_true")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("root_operations_only")
    os.umask(0o077)
    cutover = Cutover(args.state)
    locked = False
    try:
        with exclusive_release():
            locked = True
            if args.prepare:
                if not args.bundle or not args.sha256:
                    raise ValueError("bundle_required")
                cutover.prepare(args.bundle, args.sha256)
            else:
                if Path(__file__).resolve().parent != cutover.folder / "tools":
                    raise ValueError("use_frozen_tool_copy")
                if not Path(sys.executable).absolute().is_relative_to(cutover.receiver / "venv"):
                    raise ValueError("use_independent_runtime")
                for name, digest in cutover.state["tools_hashes"].items():
                    if sha(cutover.folder / "tools" / name) != digest:
                        raise ValueError("frozen_tool_changed")
                if (
                    sha(cutover.receiver / "identity_spool.py")
                    != cutover.state["tools_hashes"]["identity_spool.py"]
                ):
                    raise ValueError("frozen_receiver_changed")
                if args.rollback:
                    cutover.restore()
                else:
                    try:
                        cutover.apply()
                    except Exception:
                        if cutover.state.get("phase") == "prepared":
                            raise
                        import traceback

                        with cutover.log.open("a") as stream:
                            traceback.print_exc(file=stream)
                        try:
                            cutover.restore()
                        except Exception:
                            cutover.stage("needs_repair")
                            raise RuntimeError("cutover_and_recovery_failed_keep_buffer") from None
                        raise RuntimeError("cutover_failed_restored") from None
    finally:
        # Persistent 定时器只能在锁释放后恢复，拒绝并发请求不能启动它们。
        if locked and cutover.state.get("phase") in {"completed", "reversed"}:
            for name in TIMERS:
                if name in cutover.state.get("active", []):
                    cutover.unit("start", name)


if __name__ == "__main__":
    main()
