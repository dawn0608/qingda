"""PostgreSQL 发布入口；暂存回调、同结构恢复，保留最新业务数据。"""

import argparse
import ast
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dotenv import dotenv_values

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from layout import APP, CONFIG, DATA, ROOT, require_project_identity, service_template, unit
from proxy import Gateway, backup_state, health_url
from storage_retention import register, sha, storage_lock

UNITS = [unit("worker"), unit("web")]
SCHEMA_VERSION = 59
OPERATION_LOG = None


def ensure_source_parent(path, root=APP):
    """受限发布进程的 umask 不得使新源码目录对服务账号不可读。"""
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("source_directory_outside_app")
    missing = []
    parent = path.parent
    while not parent.exists() and parent != root:
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        directory.chmod(0o755)


def require_auth_config(values):
    required = (
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "ALIYUN_SMS_SIGN_NAME",
        "ALIYUN_SMS_TEMPLATE_CODE",
    )
    missing = [name for name in required if not str(values.get(name) or "").strip()]
    if missing:
        raise ValueError("短信认证配置缺失: " + ", ".join(missing))
    if len(values.get("ALIYUN_SMS_SCHEME_NAME") or "") > 20:
        raise ValueError("ALIYUN_SMS_SCHEME_NAME 超过 20 个字符")


def verify_api_auth(public_url):
    # 匿名只读请求须到达后端鉴权，不创建业务状态。
    try:
        urlopen(
            Request(
                public_url.rstrip("/") + "/wechat/api/v1/accounts",
                method="GET",
            ),
            timeout=15,
        )
    except HTTPError as error:
        if error.code == 401:
            return
    raise RuntimeError("api_auth_probe_failed")


def rollback_units(app=APP):
    return [unit("web"), unit("worker")]


def progress(stage):
    print(json.dumps({"stage": stage}), flush=True)


def command(args, env=None, timeout=120):
    result = subprocess.run([str(v) for v in args], env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        if OPERATION_LOG:
            with OPERATION_LOG.open("ab") as log:
                log.write(result.stdout + b"\n" + result.stderr + b"\n")
            OPERATION_LOG.chmod(0o600)
        raise RuntimeError("运维命令失败：" + str(args[0]) + "；详细输出保存在受限发布目录")
    return result.stdout


def pg_url(database, role="migration"):
    values = json.loads((CONFIG / "postgres-credentials.json").read_text())
    return "postgresql://qingda_" + role + ":" + values[role] + "@127.0.0.1:5432/" + database


def environment(candidate, url):
    return {
        **os.environ,
        **{k: v for k, v in dotenv_values(APP / ".env").items() if v is not None},
        "DATABASE_URL": url,
        "APP_ROOT": str(candidate),
        "PYTHONPATH": str(candidate / "backend"),
    }


def run_python(python, candidate, url, code):
    return command([python, "-c", code], environment(candidate, url))


def grants(url, python, candidate):
    # Root local peer access configures only this project's objects and roles.
    sql = """
DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='qingda_owner') THEN CREATE ROLE qingda_owner NOLOGIN; END IF; END $$;
GRANT qingda_owner TO qingda_migration,qingda_admin;
ALTER DATABASE qingda OWNER TO qingda_owner;
ALTER SCHEMA public OWNER TO qingda_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $$ DECLARE r record; BEGIN
FOR r IN SELECT tablename AS name FROM pg_tables WHERE schemaname='public' LOOP EXECUTE format('ALTER TABLE public.%I OWNER TO qingda_owner',r.name); END LOOP;
FOR r IN SELECT viewname AS name FROM pg_views WHERE schemaname='public' LOOP EXECUTE format('ALTER VIEW public.%I OWNER TO qingda_owner',r.name); END LOOP;
END $$;
GRANT CONNECT ON DATABASE qingda TO qingda_app,qingda_admin;
GRANT USAGE ON SCHEMA public TO qingda_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO qingda_app;
GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO qingda_app;
ALTER DEFAULT PRIVILEGES FOR ROLE qingda_owner IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO qingda_app;
ALTER DEFAULT PRIVILEGES FOR ROLE qingda_owner IN SCHEMA public GRANT USAGE,SELECT,UPDATE ON SEQUENCES TO qingda_app;
"""
    result = subprocess.run(
        ["runuser", "-u", "postgres", "--", "psql", "-d", "qingda", "-v", "ON_ERROR_STOP=1", "-q"],
        input=sql,
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("PostgreSQL项目权限配置失败")


def check_sources(spec):
    require_project_identity(spec)
    if sha(APP / "release.json") != spec["expected_manifest_sha256"]:
        raise RuntimeError("线上发布版本已改变，必须重新冻结")
    old = json.loads((APP / "release.json").read_text())
    require_project_identity(old)
    for name, digest in old["files"].items():
        p = APP / name
        if p.is_symlink() or not p.is_file() or sha(p) != digest:
            raise RuntimeError("线上源码或产物发生漂移：" + name)
    for name, digest in spec["before"].items():
        p = APP / name
        if (
            not p.resolve().is_relative_to(APP)
            or p.is_symlink()
            or (sha(p) if p.exists() else None) != digest
        ):
            raise RuntimeError("目标路径发生漂移：" + name)
    return old


def extract(bundle, expected, folder):
    if sha(bundle) != expected:
        raise ValueError("发布包哈希不符")
    with tarfile.open(bundle) as archive:
        members = archive.getmembers()
        if any(
            not x.isfile() or x.name.startswith("/") or ".." in pathlib.PurePosixPath(x.name).parts
            for x in members
        ):
            raise ValueError("发布包路径非法")
        if len({x.name for x in members}) != len(members):
            raise ValueError("发布包路径重复")
        spec = json.load(archive.extractfile("release.json"))
        require_project_identity(spec)
        if {x.name for x in members} != set(spec["files"]) | {"release.json"}:
            raise ValueError("发布包清单不符")
        for name, digest in spec["files"].items():
            body = archive.extractfile(name).read()
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("产物哈希不符：" + name)
            p = folder / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
            p.chmod(0o644)
        (folder / "release.json").write_text(json.dumps(spec, indent=2))
    return spec


def prepare_runtime_permissions(venv):
    """仅公开锁定依赖的读取和执行权限，受限发布目录及凭据权限不变。"""
    venv = pathlib.Path(venv)
    if venv.is_symlink() or not venv.resolve().is_relative_to(ROOT) or not (venv / "pyvenv.cfg").is_file():
        raise ValueError("依赖环境路径不符")
    paths = [venv, *venv.rglob("*")]
    if any(path.is_symlink() and not path.resolve().is_relative_to(venv.resolve()) for path in paths):
        raise ValueError("依赖环境符号链接越界")
    for path in paths:
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode | (0o055 if path.is_dir() or mode & 0o111 else 0o044))


def dependency_runtime(candidate, prepared=None):
    if prepared:
        python = pathlib.Path(prepared) / "bin/python"
        if not python.is_file() or not python.resolve().is_relative_to(ROOT):
            raise ValueError("预构建环境路径不符")
        prepare_runtime_permissions(python.parent.parent)
        command([python, "-c", "import psycopg,psycopg_pool,fastapi,PIL"])
        return python
    venv = candidate / ".venv"
    command([APP / ".venv/bin/python", "-m", "venv", "--copies", "--without-pip", venv])
    env = {
        **os.environ,
        "PYTHONPATH": str(candidate / "deploy/pip.whl"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    command(
        [
            venv / "bin/python",
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "-r",
            candidate / "deploy/requirements.txt",
        ],
        env,
        timeout=300,
    )
    prepare_runtime_permissions(venv)
    return venv / "bin/python"


def backup_files(stage, old):
    with tarfile.open(stage / "source.tar.gz", "w:gz") as archive:
        for name in set(old["files"]) | {"release.json"}:
            archive.add(APP / name, arcname=name, recursive=False)
    (stage / "files").mkdir()
    shutil.copy2(APP / ".env", stage / "files/.env")
    (stage / "files/.env").chmod(0o600)
    values = dotenv_values(APP / ".env")
    for key, default, name in [
        ("MASTER_KEY_FILE", str(CONFIG / "master.key"), "master.key"),
    ]:
        shutil.copy2(values.get(key) or default, stage / name)
        (stage / name).chmod(0o600)
    with tarfile.open(stage / "attachments.tar.gz", "w:gz") as archive:
        data = pathlib.Path(values.get("DATA_DIR") or str(DATA))
        for name in ("attachments", "incoming-media"):
            if (data / name).exists():
                archive.add(data / name, arcname=name)
    backup_state(stage / "proxy-state.tar.gz")


def buffer_gateway(gateway, before):
    text, count = re.subn(
        r"(location\s+\^~\s+/wechatapi/callback/\s*\{[^}]*?proxy_pass\s+http://[^:;]+:)(17950)(;)",
        lambda match: match[1] + "17952" + match[3],
        before.decode(),
        flags=re.S,
    )
    if count != 1:
        raise RuntimeError("无法精确定位原生回调代理规则")
    return text.encode()


def wait_buffer_callback(callback, timeout=10):
    """reload仅提交信号；等新Worker实际承接请求后再停止应用。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(callback, timeout=3) as response:
                if (
                    response.status == 200
                    and response.headers.get("X-Callback-Receiver") == "qingda-postgres-buffer"
                ):
                    return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("公网缓冲回调未在时限内就绪")


def wait_health(url, revision=None, schema_version=SCHEMA_VERSION):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3) as response:
                body = json.load(response)
            if (
                body.get("database_backend") == "postgresql"
                and body.get("database_ready")
                and body.get("schema_version") == schema_version
                and (revision is None or body.get("revision") == revision)
            ):
                return body
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("PostgreSQL业务健康检查未通过")


def update_env(url, revision):
    path = APP / ".env"
    stat = path.stat()
    body = path.read_text()
    body = (
        re.sub(
            r"(?m)^(DATABASE_URL|APP_REVISION|QWEATHER_API_HOST|QWEATHER_API_KEY|BAILIAN_ASR_MODEL|BAILIAN_VISION_MODEL|BAILIAN_VIDEO_MODEL|FFPROBE_BINARY|TENCENT_ASR_APP_ID|TENCENT_ASR_SECRET_ID|TENCENT_ASR_SECRET_KEY|BRIDGE_[A-Z0-9_]+|CODEX_STATE_DB|ATTACHMENT_MAX_BYTES)=.*\n?",
            "",
            body,
        ).rstrip()
        + "\nDATABASE_URL="
        + url
        + "\nAPP_REVISION="
        + revision
        + "\n"
    )
    temporary = APP / ".env.pg.tmp"
    temporary.write_text(body)
    os.chmod(temporary, stat.st_mode & 0o777)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.replace(temporary, path)


def restore_environment(backup):
    # 备份刻意收紧为0600；只恢复正文，保留运行配置原有的服务组读取权限。
    shutil.copyfile(backup, APP / ".env")


def wait_runtimes(url, revision, timeout=150):
    """Web 健康不能代替 Worker 启动；核对数据库注册的版本和实际 systemd PID。"""
    # 强制停止后旧租约仍有效120秒；等待自然到期，不越权清理其他进程的锁。
    deadline = time.monotonic() + timeout
    while True:
        rows = json.loads(
            run_python(
                APP / ".venv/bin/python",
                APP,
                url,
                "import json,os,psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); print(json.dumps(c.execute('SELECT service,revision,pid FROM runtime_versions').fetchall())); c.close()",
            )
        )
        records = {name: (rev, pid) for name, rev, pid in rows}
        ready = True
        for name in ("web", "worker"):
            pid = int(command(["systemctl", "show", unit(name), "--property=MainPID", "--value"]))
            ready = ready and pid > 0 and records.get(name) == (revision, pid)
        if ready:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Web/Worker运行版本或进程核验失败")
        time.sleep(1)


def drain_replies(python, candidate, timeout=300):
    """回调已切入缓冲后排空原 Worker；超时保持原数据并取消此次切换。"""
    deadline = time.monotonic() + timeout
    while True:
        active = int(
            run_python(
                python,
                candidate,
                pg_url("qingda"),
                "import os,psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); total=0\nfor (uid,) in c.execute('SELECT id FROM users').fetchall():\n c.execute(\"SELECT set_config('app.user_id',%s,true)\",(str(uid),)); total+=c.execute(\"SELECT count(*) FROM tasks WHERE state IN ('queued','generating','preparing','ready','sending')\").fetchone()[0]\nprint(total); c.close()",
            ).strip()
        )
        if active == 0:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("在途回复尚未排空，未停止原服务或修改数据库")
        time.sleep(2)


def release(
    bundle,
    expected,
    check=False,
    prepared=None,
    rehearsal_database="qingda_rehearsal",
    rollback_schema=None,
    worker_active=None,
):
    global OPERATION_LOG
    paused = (CONFIG / "outbound-paused").exists()
    run_worker = not paused and worker_active is not False
    os.umask(63)
    stage = ROOT / ("qingda-release-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    stage.mkdir(mode=448)
    register(stage, "transient", "preparing")
    OPERATION_LOG = stage / "operations.log"
    candidate = stage / "candidate"
    candidate.mkdir()
    spec = extract(pathlib.Path(bundle), expected, candidate)
    target_schema = rollback_schema or SCHEMA_VERSION
    old = check_sources(spec)
    previous_schema = source_schema(APP)
    require_auth_config(dotenv_values(APP / ".env"))
    gateway = Gateway()
    before = gateway.read_bytes()
    callback_enabled = b"location ^~ /wechatapi/callback/" in before
    if hashlib.sha256(before).hexdigest() != spec["expected_nginx_sha256"]:
        raise RuntimeError("网关已漂移")
    python = dependency_runtime(candidate, prepared)
    url = dotenv_values(APP / ".env").get("DATABASE_URL")
    if not url:
        raise ValueError("缺少DATABASE_URL")
    progress("candidate_ready")
    if check:
        run_python(
            python,
            candidate,
            pg_url(rehearsal_database),
            "from qingda.store import Store; s=Store(__import__('os').environ['DATABASE_URL']); assert s.health()['database_ready']; s.close()",
        )
        receipt = {
            "revision": spec["revision"],
            "candidate_hashes": True,
            "postgresql_rehearsal": True,
            "prepared_python": str(python),
        }
        (stage / "rehearsal.json").write_text(json.dumps(receipt))
        register(stage, "transient", "check_complete")
        print(json.dumps({"checked": True, "stage": str(stage), "revision": spec["revision"]}), flush=True)
        return
    check_sources(spec)
    backup_files(stage, old)
    (stage / "nginx.before.conf").write_bytes(before)
    register(stage, "transient", "switching")
    switched = False
    buffered = False
    source_mutating = False
    try:
        if not paused or callback_enabled:
            envfile = CONFIG / "migration-buffer.env"
            envfile.write_text("DATABASE_URL=" + url + "\n")
            envfile.chmod(384)
            command(
                [
                    "systemd-run",
                    "--unit=" + unit("migration-inbox"),
                    "--collect",
                    "--property=EnvironmentFile=" + str(envfile),
                    "--property=TimeoutStopSec=30",
                    "--setenv=APP_ROOT=" + str(APP),
                    "--setenv=PYTHONPATH=" + str(candidate / "backend"),
                    python,
                    "-m",
                    "qingda.callback_buffer",
                ]
            )
            values = dotenv_values(APP / ".env")
            key = (
                run_python(
                    python,
                    candidate,
                    url,
                    "import os,psycopg; from qingda.security import Vault; from qingda.config import Config; c=psycopg.connect(os.environ['DATABASE_URL']); value=c.execute('SELECT callback_key FROM users ORDER BY id LIMIT 1').fetchone(); print(Vault(Config.from_env().key_file).decrypt(value[0])); c.close()",
                )
                .decode()
                .strip()
            )
            callback = values["PUBLIC_URL"] + "/wechatapi/callback/" + key
            time.sleep(1)
            with urlopen(
                "http://" + values["WEB_BIND_HOST"] + ":17952/wechatapi/callback/" + key, timeout=5
            ) as response:
                if response.status != 200:
                    raise RuntimeError("缓冲接收器未就绪")
            gateway.put(buffer_gateway(gateway, before))
            buffered = True
            wait_buffer_callback(callback)
            progress("callback_buffering")
        if previous_schema != target_schema:
            drain_replies(python, candidate)
            progress("replies_drained")
        command(["systemctl", "stop", *UNITS], timeout=90)
        for service in UNITS:
            if command(["systemctl", "show", service, "--property=MainPID", "--value"]).strip() != b"0":
                raise RuntimeError("业务进程尚未退出")
        progress("services_stopped")
        run_python(
            python,
            candidate,
            url,
            "from qingda.migration import backup_database;backup_database(__import__('os').environ['DATABASE_URL'],"
            + repr(str(stage / "qingda-before.dump"))
            + ")",
        )
        if previous_schema != target_schema:
            register(stage, "protected", "pre_migration", "结构升级前原始数据及配置；禁止旧结构回退")
        if rollback_schema:
            align_schema(python, candidate, pg_url("qingda"), rollback_schema)
        run_python(
            python,
            candidate,
            pg_url("qingda"),
            "from qingda.migration import migrate_database;migrate_database(__import__('os').environ['DATABASE_URL'])",
        )
        if buffered:
            run_python(
                python,
                candidate,
                url,
                "from qingda.store import Store;from qingda.callback_buffer import replay;s=Store(__import__('os').environ['DATABASE_URL']);replay(s);s.close()",
            )
        old_venv = APP / ".venv"
        if old_venv.is_symlink():
            raise RuntimeError("未支持的现有venv符号链接")
        source_mutating = True
        os.rename(old_venv, stage / "venv-before")
        shutil.copytree(python.parent.parent, old_venv, symlinks=False)
        for name in set(old["files"]) - set(spec["files"]):
            p = APP / name
            if p.is_file():
                p.unlink()
        for name in spec["files"]:
            source = candidate / name
            target = APP / name
            ensure_source_parent(target)
            shutil.copyfile(source, target)
            target.chmod(420)
        (APP / "release.json").write_text(
            json.dumps(
                {
                    "project_identity": "qingda",
                    "revision": spec["revision"],
                    "files": spec["files"],
                    "tracked": spec.get("tracked", {}),
                    "previous_revision": old["revision"],
                },
                indent=2,
            )
        )
        (APP / "release.json").chmod(420)
        update_env(url, spec["revision"])
        for source in (APP / "deploy").glob("qingda-*"):
            if source.suffix in {".service", ".timer"}:
                target = pathlib.Path("/etc/systemd/system") / unit(source.name.removeprefix("qingda-"))
                target.write_text(service_template(source))
                target.chmod(0o644)
        command(["systemctl", "daemon-reload"])
        switched = False
        command(["systemctl", "start", unit("web")])
        wait_health(health_url(), spec["revision"], target_schema)
        if buffered:
            gateway.put(before)
            command(["systemctl", "stop", unit("migration-inbox")], timeout=40)
            run_python(
                APP / ".venv/bin/python",
                APP,
                url,
                "from qingda.store import Store;from qingda.callback_buffer import replay;s=Store(__import__('os').environ['DATABASE_URL']);print(replay(s));assert not s.query('SELECT 1 FROM migration_inbox WHERE imported=0');s.close()",
            )
            buffered = False
        if run_worker:
            command(["systemctl", "start", unit("worker")])
            wait_runtimes(url, spec["revision"])
        elif command(["systemctl", "show", unit("worker"), "--property=MainPID", "--value"]).strip() != b"0":
            raise RuntimeError("停站期间Worker不得运行")
        wait_health(health_url(), spec["revision"], target_schema)
        if buffered is False and callback_enabled:
            run_python(
                APP / ".venv/bin/python",
                APP,
                url,
                "from qingda.store import Store;s=Store(__import__('os').environ['DATABASE_URL']);assert not s.query('SELECT 1 FROM migration_inbox WHERE imported=0');s.query('DELETE FROM migration_inbox WHERE imported=1 RETURNING id');s.close()",
            )
            (CONFIG / "migration-buffer.env").unlink(missing_ok=True)
        verify_api_auth(
            health_url().removesuffix("/internal/health")
            if paused
            else dotenv_values(APP / ".env")["PUBLIC_URL"]
        )
        if gateway.read_bytes() != before:
            raise RuntimeError("回调网关未恢复原配置")
        for name, digest in spec["files"].items():
            if sha(APP / name) != digest:
                raise RuntimeError("生产产物哈希不符：" + name)
        hashes = {
            name: sha(stage / name)
            for name in (
                "source.tar.gz",
                "qingda-before.dump",
                "files/.env",
                "master.key",
                "nginx.before.conf",
                "attachments.tar.gz",
                "proxy-state.tar.gz",
            )
        }
        receipt = {
            "revision": spec["revision"],
            "outbound_paused": paused,
            "previous_revision": old["revision"],
            "backup_sha256": hashes,
            "database_backend": "postgresql",
            "schema_version": target_schema,
            "gateway_backend": gateway.backend,
            "rollback": "仅允许同结构修复，保留最新 PostgreSQL 数据",
        }
        (stage / "receipt.json").write_text(json.dumps(receipt, indent=2))
        switched = True
        register(stage, "release", "awaiting_acceptance")
        print(json.dumps({"released": True, "revision": spec["revision"], "backup": str(stage)}), flush=True)
    except BaseException:
        if previous_schema != target_schema:
            try:
                actual = run_python(
                    python,
                    candidate,
                    pg_url("qingda"),
                    "import os,psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); print(c.execute('SELECT max(version) FROM schema_versions').fetchone()[0]); c.close()",
                )
                changed = int(actual.strip()) != previous_schema
            except BaseException:
                changed = True
            if changed:
                register(
                    stage,
                    "protected",
                    "needs_repair",
                    "结构已提交或状态不确定，保留最新数据及回调缓冲，必须同结构修复",
                )
                progress("schema_repair_required_callback_buffer_retained")
                raise
        restored = False
        if not switched:
            try:
                if source_mutating:
                    command(["systemctl", "stop", *UNITS])
                    if (stage / "venv-before").exists():
                        if (APP / ".venv").exists():
                            shutil.move(APP / ".venv", stage / "venv-incomplete")
                        os.rename(stage / "venv-before", APP / ".venv")
                    for name in set(spec["files"]) - set(old["files"]):
                        (APP / name).unlink(missing_ok=True)
                    with tarfile.open(stage / "source.tar.gz") as archive:
                        archive.extractall(APP, filter="data")
                    restore_environment(stage / "files/.env")
                    for service in UNITS:
                        source = APP / "deploy" / (service + ".service")
                        if not source.exists():
                            source = APP / "deploy" / ("qingda-" + service.split("-", 2)[-1] + ".service")
                        target = pathlib.Path("/etc/systemd/system") / (service + ".service")
                        target.write_text(service_template(source))
                        target.chmod(0o644)
                    command(["systemctl", "daemon-reload"])
                align_schema(APP / ".venv/bin/python", APP, pg_url("qingda"), source_schema(APP))
                if buffered:
                    command(["systemctl", "start", unit("web")])
                    gateway.put(before)
                    command(["systemctl", "stop", unit("migration-inbox")], timeout=40)
                    run_python(
                        APP / ".venv/bin/python",
                        APP,
                        url,
                        "from qingda.store import Store;from qingda.callback_buffer import replay;s=Store(__import__('os').environ['DATABASE_URL']);replay(s);s.close()",
                    )
                    (CONFIG / "migration-buffer.env").unlink(missing_ok=True)
                    buffered = False
                command(["systemctl", "start", *([unit("web")] if not run_worker else rollback_units())])
                progress("failed_before_switch_original_service_restored")
                restored = True
            except BaseException:
                progress("failed_before_switch_recovery_needs_repair")
        register(
            stage,
            "protected",
            "failed_restored" if restored else "needs_repair",
            "发布失败，恢复材料继续保护",
        )
        raise


def source_schema(folder):
    tree = ast.parse((folder / "backend/qingda/store.py").read_text())
    return next(
        n.value.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SCHEMA_VERSION" for t in n.targets)
    )


def align_schema(python, candidate, url, target):
    """恢复仅允许同结构版本，始终保留最新数据。"""
    actual = run_python(
        python,
        candidate,
        url,
        "import os,psycopg; c=psycopg.connect(os.environ['DATABASE_URL']); print(c.execute('SELECT max(version) FROM schema_versions').fetchone()[0]); c.close()",
    )
    if int(actual.strip()) != target:
        raise ValueError("数据库结构已改变，必须使用同结构修复版本")


def rollback_release(backup):
    backup = pathlib.Path(backup).resolve()
    if backup.parent != ROOT.resolve() or backup.is_symlink():
        raise ValueError("回退目录不属于本项目")
    receipt = json.loads((backup / "receipt.json").read_text())
    for name, digest in receipt["backup_sha256"].items():
        if sha(backup / name) != digest:
            raise ValueError("回退备份哈希不符")
    current = json.loads((APP / "release.json").read_text())
    require_project_identity(current)
    if current["revision"] != receipt["revision"]:
        raise ValueError("当前版本已经改变，不能跨发布覆盖")
    stage = ROOT / ("rollback-upload-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    stage.mkdir(mode=0o700)
    register(stage, "transient", "rollback_preparing")
    with tarfile.open(backup / "source.tar.gz") as archive:
        old = json.load(archive.extractfile("release.json"))
        require_project_identity(old)
        spec = {
            **old,
            "expected_manifest_sha256": sha(APP / "release.json"),
            "expected_nginx_sha256": hashlib.sha256(Gateway().read_bytes()).hexdigest(),
            "before": {name: sha(APP / name) if (APP / name).is_file() else None for name in old["files"]},
        }
        folder = stage / "source"
        folder.mkdir()
        for name, digest in old["files"].items():
            if pathlib.PurePosixPath(name).is_absolute() or ".." in pathlib.PurePosixPath(name).parts:
                raise ValueError("回退路径非法")
            body = archive.extractfile(name).read()
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("回退产物哈希不符")
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        (folder / "release.json").write_text(json.dumps(spec))
    target_schema = source_schema(folder)
    if target_schema != source_schema(APP) or target_schema != SCHEMA_VERSION:
        raise ValueError("只允许同结构恢复；保留最新数据并使用修复版本")
    bundle = stage / "rollback.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        for name in set(spec["files"]) | {"release.json"}:
            archive.add(folder / name, arcname=name, recursive=False)
    release(bundle, sha(bundle), prepared=backup / "venv-before", rollback_schema=target_schema)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("bundle", nargs="?")
    p.add_argument("sha256", nargs="?")
    p.add_argument("--rollback", type=pathlib.Path)
    p.add_argument("--check", action="store_true")
    p.add_argument("--prepared", type=pathlib.Path)
    p.add_argument("--rehearsal-database", default="qingda_rehearsal")
    args = p.parse_args()
    with storage_lock():
        if args.rollback:
            rollback_release(args.rollback)
            return
        if not args.bundle or not args.sha256:
            p.error("需要发布包及SHA256，或--rollback目录")
        release(
            args.bundle,
            args.sha256,
            args.check,
            args.prepared,
            args.rehearsal_database,
        )


if __name__ == "__main__":
    main()
