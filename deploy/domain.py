"""备案停站及域名切换。只复用现有发布备份、运维锁和用户绑定事务。"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_nginx import CERTBOT, WEBROOT, folder_path, render_domain, run
from ops_config import environment, write
from proxy import ACME, APP, Gateway, backup_state, health_url
from storage_retention import register, save_json, sha, storage_lock

sys.path.append(str(APP / "backend"))

PAUSE = Path("/etc/wechat-manager/outbound-paused")
CURRENT = Path("/etc/wechat-manager/domain-switch.json")
DOMAIN = "example.com"
ORIGIN = "https://" + DOMAIN
LINEAGE = ACME / "certbot/live" / DOMAIN.replace(".", "-")


def query(sql, params=()):
    """受限root运维只读查看全体用户；业务RLS账号不能充当全局停站检查。"""
    database = urlsplit(dotenv_values(APP / ".env")["DATABASE_URL"]).path.lstrip("/")
    script = """import sys,json,psycopg
payload=json.load(sys.stdin)
with psycopg.connect(dbname=payload['database'],user='postgres',host='/var/run/postgresql') as c:
 with c.transaction():
  c.execute('SET TRANSACTION READ ONLY')
  result=c.execute(payload['sql'],payload['params']).fetchall()
print(json.dumps(result,default=str))
"""
    result = subprocess.run(
        ["runuser", "-u", "postgres", "--", str(APP / ".venv/bin/python"), "-c", script],
        input=json.dumps(dict(database=database, sql=sql, params=params)),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode:
        raise RuntimeError("全用户只读运维检查失败；数据库信息未输出")
    return json.loads(result.stdout)


def load(folder):
    folder = folder_path(folder)
    path = folder / "domain-switch.json"
    if path.is_symlink():
        raise ValueError("域名检查点不得为链接")
    state = json.loads(path.read_text())
    if state["domain"] != DOMAIN:
        raise ValueError("域名检查点不匹配")
    return state


def save(folder, state, stage=None):
    if stage:
        state["stage"] = stage
    state["updated_at"] = time.time()
    save_json(folder / "domain-switch.json", state)
    save_json(CURRENT, {**state, "folder": str(folder)})


def prepare(folder):
    folder = folder_path(folder)
    if (folder / "domain-switch.json").exists():
        return load(folder)
    receipt = json.loads((folder / "receipt.json").read_text())
    release = json.loads((APP / "release.json").read_text())
    if receipt["revision"] != release["revision"] or not Gateway().native:
        raise ValueError("必须使用当前原生发布的有效回滚目录")
    state = dict(
        domain=DOMAIN, stage="preparing", revision=release["revision"], users={}, prepared_at=time.time()
    )
    save(folder, state)
    register(folder, "protected", "domain_preparing", "备案及域名切换未完成，保留最新多用户数据及恢复材料")
    write(folder / "domain-root-env.before", (APP / ".env").read_bytes())
    write(folder / "domain-nginx.before", Gateway().read_bytes())
    backup_state(folder / "domain-proxy-state.tar.gz")
    from wechat_manager.migration import backup_database

    backup_database(dotenv_values(APP / ".env")["DATABASE_URL"], folder / "domain-before.dump")
    names = (
        "domain-root-env.before",
        "domain-nginx.before",
        "domain-proxy-state.tar.gz",
        "domain-before.dump",
    )
    state["backup_sha256"] = {name: sha(folder / name) for name in names}
    state["public_ip"] = socket.gethostbyname(urlsplit(dotenv_values(APP / ".env")["PUBLIC_URL"]).hostname)
    state["counts"] = query(
        "SELECT u.id,(SELECT count(*) FROM accounts a WHERE a.user_id=u.id),(SELECT count(*) FROM message_records m WHERE m.user_id=u.id) FROM users u ORDER BY u.id"
    )
    receipt["backup_sha256"].update(state["backup_sha256"])
    save_json(folder / "receipt.json", receipt)
    save(folder, state, "prepared")
    return state


def pause(folder):
    state = load(folder)
    if state["stage"] not in ("prepared", "pausing", "paused"):
        raise ValueError("当前阶段不能停站")
    if PAUSE.exists() and json.loads(PAUSE.read_text())["folder"] != str(folder):
        raise ValueError("其他运维操作持有停站标记")
    for name, digest in state["backup_sha256"].items():
        if sha(folder / name) != digest:
            raise ValueError("域名备份校验失败")
    if query("SELECT count(*) FROM tasks WHERE state IN ('generating','preparing','ready','sending')")[0][0]:
        raise RuntimeError("仍有活动发送任务，等待完成后重新停站")
    save(folder, state, "pausing")
    dropin = Path("/etc/systemd/system/wechat-manager-worker.service.d/domain-pause.conf")
    dropin.parent.mkdir(parents=True, exist_ok=True)
    write(dropin, "[Unit]\nConditionPathExists=!/etc/wechat-manager/outbound-paused\n", 0o644)
    run("systemctl", "daemon-reload")
    save_json(PAUSE, dict(folder=str(folder), domain=DOMAIN, paused_at=time.time()))
    run("systemctl", "stop", "wechat-manager-worker")
    if query("SELECT count(*) FROM tasks WHERE state IN ('generating','preparing','ready','sending')")[0][0]:
        raise RuntimeError("停站边界存在待核实任务，保持Worker停止并核对")
    Gateway().put(render_domain(DOMAIN, "paused"))
    # 首次停站终止旧Nginx Worker及其keepalive，避免旧IP路由继续承接请求。
    run("systemctl", "restart", "nginx")
    state["paused_gateway_sha256"] = hashlib.sha256(Gateway().read_bytes()).hexdigest()
    save(folder, state, "paused")
    return state


def approve(folder, evidence):
    state = load(folder)
    proof = json.loads(Path(evidence).read_text())
    if state["stage"] != "paused" or proof.get("domain") != DOMAIN or proof.get("status") != "approved":
        raise ValueError("必须先停站，并提供本站审核通过的实际证据")
    number = proof.get("icp_number", "").strip()
    if not re.fullmatch(r"[\u4e00-\u9fff]ICP备[0-9]{6,20}号(?:-[0-9]+)?", number):
        raise ValueError("备案号格式不符")
    state.update(icp_number=number, approval_evidence_sha256=sha(Path(evidence)), approved_at=time.time())
    save(folder, state, "approved")
    return state


def certificate(folder):
    state = load(folder)
    if (
        state["stage"] not in ("approved", "certificate_pending", "certificate_ready")
        or not state.get("icp_number")
        or not PAUSE.exists()
    ):
        raise ValueError("备案通过并保持Worker停站后才能签发域名证书")
    addresses = {r[4][0] for r in socket.getaddrinfo(DOMAIN, 443, type=socket.SOCK_STREAM)}
    if addresses != {state["public_ip"]}:
        raise ValueError("域名解析尚未唯一指向现有服务器")
    save(folder, state, "certificate_pending")
    Gateway().put(render_domain(DOMAIN, "challenge"))
    WEBROOT.mkdir(parents=True, exist_ok=True)
    run(
        CERTBOT,
        "certonly",
        "--webroot",
        "-w",
        WEBROOT,
        "--cert-name",
        LINEAGE.name,
        "-d",
        DOMAIN,
        "--config-dir",
        ACME / "certbot",
        "--work-dir",
        ACME / "acme-work",
        "--logs-dir",
        ACME / "acme-logs",
        "--non-interactive",
        "--keep-until-expiring",
        timeout=300,
    )
    run("openssl", "x509", "-in", LINEAGE / "fullchain.pem", "-noout", "-checkhost", DOMAIN)
    run("openssl", "x509", "-in", LINEAGE / "fullchain.pem", "-noout", "-checkend", "604800")
    state["certificate_sha256"] = sha(LINEAGE / "fullchain.pem")
    save(folder, state, "certificate_ready")
    return state


def activate(folder):
    state = load(folder)
    if state["stage"] not in ("certificate_ready", "activating", "binding") or not PAUSE.exists():
        raise ValueError("域名证书未就绪或Worker停站标记丢失")
    save(folder, state, "activating")
    environment(
        dict(
            PUBLIC_URL=ORIGIN,
            COOKIE_SECURE="true",
            ICP_FILING_NUMBER=state["icp_number"],
        )
    )
    Gateway().put(render_domain(DOMAIN))
    run("systemctl", "restart", "wechat-manager-web")
    state.setdefault("event_floor", query("SELECT coalesce(max(id),0) FROM events")[0][0])
    state.setdefault("activated_at", time.time())
    save(folder, state, "binding")
    return state


async def bind_users(folder):
    from fastapi import HTTPException
    from wechat_manager.config import Config
    from wechat_manager.profile import Wechat, bind_wechat
    from wechat_manager.providers import Gateway as Provider
    from wechat_manager.security import Vault
    from wechat_manager.service import Manager
    from wechat_manager.store import Store
    from wechat_manager.tenancy import user_scope

    state = load(folder)
    if state["stage"] not in ("binding", "bound") or not PAUSE.exists():
        raise ValueError("域名尚未激活或Worker未停站")
    config = Config.from_env()
    if config.public_url != ORIGIN:
        raise ValueError("应用源地址未切换")
    store = Store(config.database_url)
    manager = Manager(store, Vault(config.key_file))
    provider = Provider(manager)
    try:
        for row in store.query("SELECT id FROM users ORDER BY id"):
            uid = row["id"]
            with user_scope(uid):
                user = store.query("SELECT * FROM users WHERE id=%s", (uid,))[0]
                desired = ORIGIN + "/wechatapi/callback/" + manager.vault.decrypt(user["callback_key"])
                previous = state["users"].get(str(uid), {})
                if user["callback_url"] == desired and (
                    not user["wechat_token"] or user["binding_state"] == "configured"
                ):
                    state["users"][str(uid)] = dict(
                        state="configured" if user["wechat_token"] else "unconfigured",
                        revision=user["binding_revision"],
                    )
                    save(folder, state)
                    continue
                if previous.get("state") in ("calling", "uncertain") or user["binding_state"] in (
                    "binding",
                    "uncertain",
                ):
                    raise RuntimeError("用户绑定结果待核实，不得自动重复调用")
                if not user["wechat_token"]:
                    with store.transaction() as c:
                        c.execute(
                            "UPDATE users SET callback_url=%s WHERE id=%s AND wechat_token IS NULL AND binding_revision=%s",
                            (desired, uid, user["binding_revision"]),
                        )
                    state["users"][str(uid)] = dict(state="unconfigured", revision=user["binding_revision"])
                else:
                    state["users"][str(uid)] = dict(state="calling", revision=user["binding_revision"])
                    save(folder, state)
                    try:
                        answer = await bind_wechat(
                            manager,
                            config,
                            provider,
                            Wechat(revision=user["binding_revision"]),
                        )
                    except HTTPException:
                        current = store.query(
                            "SELECT binding_state,binding_revision FROM users WHERE id=%s", (uid,)
                        )[0]
                        state["users"][str(uid)] = dict(
                            state="uncertain"
                            if current["binding_state"] in ("binding", "uncertain")
                            else "failed",
                            revision=current["binding_revision"],
                        )
                        save(folder, state)
                        raise RuntimeError("官方绑定未明确成功，保持停站并核对用户配置") from None
                    state["users"][str(uid)] = dict(state="configured", revision=answer["revision"])
                save(folder, state)
        save(folder, state, "bound")
    finally:
        await provider.close()
        store.close()
    return state


def resume(folder):
    state = load(folder)
    if state["stage"] not in ("bound", "resuming", "awaiting_acceptance"):
        raise ValueError("所有用户回调尚未明确完成，禁止恢复出站")
    if query(
        "SELECT count(*) FROM users WHERE wechat_token IS NOT NULL AND (binding_state!='configured' OR callback_url NOT LIKE %s)",
        (ORIGIN + "/wechatapi/callback/%",),
    )[0][0]:
        raise ValueError("存在未绑定本站域名的用户")
    if PAUSE.exists() and json.loads(PAUSE.read_text())["folder"] != str(folder):
        raise ValueError("停站标记归属变化")
    save(folder, state, "resuming")
    PAUSE.unlink(missing_ok=True)
    run("systemctl", "start", "wechat-manager-worker")
    from update import wait_health, wait_runtimes

    release = json.loads((APP / "release.json").read_text())
    wait_health(health_url(), release["revision"])
    wait_runtimes(dotenv_values(APP / ".env")["DATABASE_URL"], release["revision"])
    save(folder, state, "awaiting_acceptance")
    return state


def evidence(folder):
    state = load(folder)
    release = json.loads((APP / "release.json").read_text())
    verified = state["stage"] in ("awaiting_acceptance", "accepted") and not PAUSE.exists()
    hashes = all(
        (APP / name).is_file() and sha(APP / name) == digest for name, digest in release["files"].items()
    )
    callbacks = query(
        "SELECT user_id,id FROM events WHERE id>%s AND received_at>%s AND origin='live' AND event_type='AddMsg' AND parse_error IS NULL",
        (state.get("event_floor", 0), state.get("activated_at", time.time())),
    )
    targets = {r[0] for r in query("SELECT id FROM users WHERE wechat_token IS NOT NULL")}
    result = dict(
        revision=release["revision"],
        source_hashes_verified=hashes,
        worker_resumed=verified,
        callback_event_ids=[r[1] for r in callbacks],
        callback_users_verified=targets.issubset({r[0] for r in callbacks}),
        business_verified=False,
    )
    save_json(folder / "domain-live-evidence.json", result)
    return {**state, "evidence": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "pause",
            "approve",
            "certificate",
            "activate",
            "bind",
            "resume",
            "evidence",
            "status",
        ),
    )
    parser.add_argument("folder", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("仅限服务器root")
    if DOMAIN == "example.com":
        parser.error("请先将 DOMAIN 和 LINEAGE 配置为本部署的真实域名及证书目录")
    os.umask(0o077)
    with storage_lock():
        folder = folder_path(args.folder)
        if args.action == "approve":
            if not args.evidence:
                parser.error("备案审批需要真实审核证据 --evidence")
            result = approve(folder, args.evidence)
        elif args.action == "bind":
            result = asyncio.run(bind_users(folder))
        elif args.action == "status":
            result = load(folder)
        else:
            result = globals()[args.action](folder)
    print(
        json.dumps(
            dict(
                stage=result["stage"],
                domain=result["domain"],
                users=result["users"],
                **({"evidence": result["evidence"]} if "evidence" in result else {}),
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
