"""服务器隔离验收：按需读取、证据范围、预算及会话调度。"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from business_fixtures import allow_reply
from qingda.media import IncomingMedia
from qingda.providers import ProviderError
from qingda.reply_execution import Execution
from qingda.worker import Worker
from test_automation import NOW, deliver, setup
from test_media import MediaGateway, config_at
from test_reply_images import picture, png
from test_worker import Gateway


def environment(tmp_path):
    store, manager, account, conv = setup(tmp_path)
    manager.media = IncomingMedia(manager, config_at(tmp_path))
    return store, manager, account, conv


async def test_other_conversation_completes_while_media_is_running(tmp_path):
    store, manager, account, _ = environment(tmp_path)
    other = manager.ensure_conversation(account["id"], "friend-two")
    allow_reply(manager, other["id"], True)
    picture(store, manager, 1, group=False)
    deliver(store, manager, 2, peer="friend-two")
    started, finish = asyncio.Event(), asyncio.Event()

    async def hold(*args, **kwargs):
        started.set()
        await finish.wait()
        return png()

    gateway = MediaGateway(png())
    gateway.download_media = hold
    worker = Worker(manager, gateway, clock=lambda: NOW + 35)
    try:
        await worker.once(background=True)
        await asyncio.wait_for(started.wait(), 15)
        for _ in range(500):
            if gateway.sends:
                break
            await asyncio.sleep(0.02)
        assert gateway.sends[0][1] == "friend-two"
        assert len(gateway.sends) == 1
        finish.set()
        await asyncio.gather(*worker.running)
        assert len(gateway.sends) == 2
        assert not store.query("SELECT * FROM reply_resource_slots")
    finally:
        finish.set()
        await asyncio.gather(*worker.running, return_exceptions=True)
        worker.release()


@pytest.mark.parametrize("ending", ["cancel", "crash"])
async def test_two_process_capacity_cancel_crash_and_expiry(tmp_path, ending):
    store, manager, _, _ = environment(tmp_path)
    deliver(store, manager, 1)
    gateway = Gateway()
    actor = Path(__file__).with_name("reply_capacity_actor.py")

    async def model(config, prompt, context, **runtime):
        task = runtime["execution"].task
        env = {**os.environ, "ACTOR_DATABASE_URL": store.url, "ACTOR_TASK_ID": task["id"]}
        process = subprocess.Popen(
            [sys.executable, str(actor), "hold"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 8)
            assert line.strip() == b"occupied"

            async def probe():
                value = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, str(actor), "probe"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert value.returncode == 0, value.stderr
                return value.stdout.strip()

            assert await probe() == "busy"
            if ending == "cancel":
                process.terminate()
            else:
                process.kill()
            await asyncio.to_thread(process.communicate, timeout=10)
            if ending == "crash":
                assert await probe() == "busy"
                store.query(
                    "UPDATE reply_resource_slots SET expires_at=%s WHERE resource='test-capacity' RETURNING token",
                    (time.time() - 1,),
                )
            assert await probe() == "available"
            assert not store.query("SELECT * FROM reply_resource_slots WHERE resource='test-capacity'")
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.communicate)
        return await Gateway.model_with_tools(gateway, config, prompt, context, **runtime)

    gateway.model_with_tools = model
    worker = Worker(manager, gateway, clock=lambda: NOW + 35)
    try:
        await worker.once()
        assert len(gateway.sends) == 1, store.query("SELECT state,reason FROM tasks")
    finally:
        worker.release()


async def test_restart_budget_and_user_isolation(tmp_path):
    from qingda.tenancy import user_scope

    store, manager, _, _ = environment(tmp_path)
    deliver(store, manager, 1)
    gateway = Gateway()

    async def model(config, prompt, context, **runtime):
        execution = runtime["execution"]
        deadline = execution.task["_execution_deadline"]
        execution.phase("text", True)
        store.query(
            "UPDATE reply_execution_budgets SET phase_started=%s RETURNING task_id",
            (time.time() - config["timeout"] - 1,),
        )
        restored = Execution(manager, execution.task, execution.guard, execution.commit)
        assert restored.initialize() == deadline
        with pytest.raises(ProviderError, match="generation_budget_exceeded"):
            await restored.run("text", lambda: asyncio.sleep(0))
        with user_scope(2):
            assert not store.query("SELECT * FROM reply_execution_budgets")
            assert not store.query("SELECT * FROM reply_execution_events")
            assert not store.query("SELECT * FROM media_evidence_cache")
        return await Gateway.model_with_tools(gateway, config, prompt, context, **runtime)

    gateway.model_with_tools = model
    worker = Worker(manager, gateway, clock=lambda: NOW + 35)
    try:
        await worker.once()
        assert len(gateway.sends) == 1, store.query("SELECT state,reason FROM tasks")
    finally:
        worker.release()


async def test_two_users_processes_share_four_active_tasks(tmp_path):
    from qingda.tenancy import initialize_user, user_scope
    from test_automation import verified_fixture

    store, manager, account, _ = environment(tmp_path)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO users(id,channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url,binding_state) VALUES(2,'phone','13900000002','其他用户',0,'other-callback','encrypted-test','https://example.invalid','configured')"
        )
    for uid in (1, 2):
        with user_scope(uid):
            if uid == 2:
                with store.transaction() as conn:
                    initialize_user(conn, uid)
                account = manager.add_account("device-second")
                manager.verify_identity("device-second", {"wxid": "wxid_second", "nickName": "另一个"})
                manager.configure_model({"model": "deepseek-flash"})
                manager.configure_automation(account["id"], prompt="简洁回复", ai_enabled=True)
            for i in range(4):
                conv = manager.ensure_conversation(account["id"], f"capacity-friend-{i}")
                allow_reply(manager, conv["id"], True)
                verified_fixture(manager, conv)
                current = manager.account(account["id"])
                raw = {
                    "TypeName": "AddMsg",
                    "Appid": current["current_app_id"],
                    "Wxid": current["wxid"],
                    "Data": {
                        "NewMsgId": i + 1,
                        "MsgType": 1,
                        "FromUserName": {"string": conv["peer_id"]},
                        "ToUserName": {"string": current["wxid"]},
                        "Content": {"string": "只输出收到"},
                        "CreateTime": NOW + 1,
                    },
                }
                store.receive(json.dumps(raw).encode(), now=NOW + 2)
                manager.process_events(now=NOW + 2)
    actor = Path(__file__).with_name("active_capacity_actor.py")
    process = subprocess.Popen(
        [sys.executable, str(actor)],
        env={**os.environ, "ACTOR_DATABASE_URL": store.url, "ACTOR_USER_ID": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        counts = json.loads(await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 15))
        assert counts == [4, 0], counts
        assert len(store.query("SELECT * FROM reply_resource_slots WHERE resource='active'")) == 4
        probe = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(actor.with_name("pg_process_actor.py")), "lease"],
            env={
                **os.environ,
                "ACTOR_DATABASE_URL": store.url,
                "ACTOR_USER_ID": "2",
                "ACTOR_CLOCK": str(NOW + 35),
            },
            capture_output=True,
            timeout=10,
        )
        assert probe.returncode == 0 and probe.stdout.strip() == b"lease_busy", probe.stderr
    finally:
        process.terminate()
        outputs = await asyncio.to_thread(process.communicate, timeout=10)
        assert process.returncode == 0, outputs
    assert not store.query("SELECT * FROM reply_resource_slots")
