"""服务器隔离库多用户验收；必须显式传入含multiuser的数据库，禁止生产故障注入。"""

import asyncio
import io
import json
import multiprocessing as mp
import os
import time
from urllib.parse import urlsplit

import httpx
from PIL import Image
from wechat_manager.app import create_app
from wechat_manager.auth import COOKIE
from wechat_manager.config import Config
from wechat_manager.database_async import database_call
from wechat_manager.providers import Gateway, ProviderError
from wechat_manager.security import digest
from wechat_manager.service import Manager
from wechat_manager.store import Store
from wechat_manager.tenancy import user_scope
from wechat_manager.worker import Worker

checks = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    checks.append(name)


class Sms:
    async def send(self, phone, cid):
        pass

    async def verify(self, phone, cid, code):
        return code == "123456"


class Email:
    def __init__(self):
        self.codes = {}

    async def send(self, target, code):
        self.codes[target] = code


class FakeGateway(Gateway):
    def __init__(self, manager):
        super().__init__(
            manager,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ret": 200, "data": True})),
        )
        self.mode = "success"

    async def call(self, path, payload, **kwargs):
        if path == "/login/setCallback":
            if self.mode == "uncertain":
                raise ProviderError("platform_response_unknown", True)
            if self.mode == "failure":
                return {"ret": 403}
            return {"ret": 200}
        return await super().call(path, payload, **kwargs)


def process_claim(url, cid, queue):
    store = Store(url)
    with store.transaction() as c:
        result = c.execute(
            "UPDATE auth_codes SET state='verifying' WHERE id=%s AND state='sent' RETURNING id", (cid,)
        ).fetchone()
    queue.put(bool(result))
    store.close()


def process_lease(url, uid, now, queue, crash=False):
    store = Store(url)
    from wechat_manager.security import Vault

    manager = Manager(store, Vault(Config.from_env().key_file))
    worker = Worker(manager, FakeGateway(manager), clock=lambda: now)
    try:
        with user_scope(uid):
            worker.lease()
        queue.put("acquired")
    except RuntimeError:
        queue.put("busy")
    finally:
        if not crash:
            store.close()
        else:
            queue.close()
            queue.join_thread()
            os._exit(17)


def process_tests(store, uid):
    with store.transaction() as c:
        c.execute("DELETE FROM worker_lock")
        c.execute(
            "INSERT INTO auth_codes(id,channel,identifier,purpose,created_at,expires_at,state) VALUES('multiuser-process-code','phone','13900000009','login',0,9999999999,'sent') ON CONFLICT(id) DO UPDATE SET state='sent'"
        )
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    children = [
        ctx.Process(target=process_claim, args=(store.url, "multiuser-process-code", queue)) for _ in range(2)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(20)
        check("验证码进程正常退出", child.exitcode == 0)
    check("两进程验证码只有一次领取", sorted([queue.get(timeout=3), queue.get(timeout=3)]) == [False, True])
    for now, expected in ((1000, "acquired"), (1001, "busy"), (1121, "acquired")):
        child = ctx.Process(target=process_lease, args=(store.url, uid, now, queue, True))
        child.start()
        child.join(20)
        check("Worker强制退出", child.exitcode == 17)
        check("Worker租约及崩溃过期接管", queue.get(timeout=3) == expected)
    with store.transaction() as c:
        c.execute("DELETE FROM worker_lock")


async def main():
    database = urlsplit(os.environ["DATABASE_URL"]).path
    if "multiuser" not in database or database == "/wechat_manager":
        raise ValueError("必须使用multiuser隔离数据库")
    config = Config.from_env()
    config.smtp_host = "smtp.test.invalid"
    config.smtp_from = "test@example.invalid"
    config.cookie_secure = False
    now = [time.time()]
    app = create_app(config, gateway_factory=FakeGateway, sms_provider=Sms(), auth_clock=lambda: now[0])
    email = Email()
    app.state.authentication.email = email
    store = app.state.manager.store
    gateway = app.state.gateway
    service = app.state.authentication
    prefix = "/wechat/api/v1"

    def client(ip):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 54321)),
            base_url=config.public_url,
            headers={"Origin": config.public_url},
        )

    c1, c2, c3 = client("10.20.1.1"), client("10.20.1.2"), client("10.20.1.3")
    try:
        check("匿名访问保护", (await c1.get(prefix + "/accounts")).status_code == 401)
        with store.transaction() as c:
            legacy = c.execute("SELECT * FROM users WHERE id=1").fetchone()
            issued = service.issue(c, legacy)
        c1.cookies.set(COOKIE, issued[0])
        c1.headers["X-CSRF-Token"] = issued[1]["csrf"]
        original = (await c1.get(prefix + "/accounts")).json()
        check("原微信账号保留", len(original) > 0)
        original_settings = (await c1.get(prefix + "/settings")).json()
        targets = [("phone", "13900000000", c2), ("email", " Person@Example.Invalid ", c3)]
        sessions = []
        for channel, target, client_ in targets:
            r = await client_.post(
                prefix + "/auth/code/send",
                json={"channel": channel, "identifier": target, "purpose": "login"},
            )
            check("发送注册验证码" + channel, r.status_code == 200)
            cid = r.json()["challenge_id"]
            code = "123456" if channel == "phone" else email.codes[target.strip().lower()]
            data = dict(
                channel=channel,
                identifier=target,
                challenge_id=cid,
                code=code,
            )
            r = await client_.post(prefix + "/auth/login", json=data)
            check("注册成功" + channel, r.status_code == 200)
            session = r.json()
            sessions.append(session)
            client_.headers["X-CSRF-Token"] = session["csrf"]
            check(
                "注册后会话有效" + channel, (await client_.get(prefix + "/auth/session")).status_code == 200
            )
            check("新用户不继承微信账号" + channel, (await client_.get(prefix + "/accounts")).json() == [])
            check(
                "新用户不继承模型密钥" + channel,
                not (await client_.get(prefix + "/settings")).json()["model_key_configured"],
            )
            r = await client_.post(prefix + "/auth/login", json=data)
            check("验证码不能重复注册" + channel, r.status_code == 400)
        uid2, uid3 = [s["user_id"] for s in sessions]
        check(
            "自动昵称与独立身份",
            all(s["username"].startswith("用户") and len(s["username"]) == 8 for s in sessions)
            and uid2 != uid3,
        )
        check("邮箱规范化", sessions[1]["identifier"] == "person@example.invalid")
        check(
            "越权账号操作拒绝",
            (await c2.post(prefix + f"/accounts/{original[0]['id']}/online")).status_code == 400,
        )
        check(
            "CSRF拒绝",
            (
                await c2.patch(
                    prefix + "/profile", json={"username": "test"}, headers={"X-CSRF-Token": "bad"}
                )
            ).status_code
            == 403,
        )
        check(
            "用户名修改",
            (await c2.patch(prefix + "/profile", json={"username": "新用户名"})).status_code == 200,
        )
        check(
            "空用户名拒绝", (await c2.patch(prefix + "/profile", json={"username": "  "})).status_code == 422
        )
        image = Image.new("RGB", (18, 24), "green")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        check(
            "头像上传",
            (await c2.put(prefix + "/profile/avatar", content=buffer.getvalue())).status_code == 200,
        )
        r = await c2.get(prefix + "/profile/avatar")
        check("头像规格", Image.open(io.BytesIO(r.content)).size == (512, 512))
        check("头像不跨用户", (await c3.get(prefix + "/profile/avatar")).status_code == 404)
        check(
            "伪图片拒绝",
            (await c2.put(prefix + "/profile/avatar", content=b"not an image")).status_code == 422,
        )
        check(
            "头像体积限制",
            (await c2.put(prefix + "/profile/avatar", content=b"x" * (5 * 1024 * 1024 + 1))).status_code
            == 413,
        )
        for uid in (uid2, uid3):
            with user_scope(uid):
                store.set_setting("tenant-proof", str(uid))
                check("业务视图隔离", store.query("SELECT count(*) AS n FROM messages")[0]["n"] == 0)
        for uid in (uid3, uid2):
            with user_scope(uid):
                check("连接复用不串用户", store.setting("tenant-proof") == str(uid))
        check("无用户上下文无业务数据", store.query("SELECT id FROM accounts") == [])
        with user_scope(uid2):
            import psycopg

            try:
                with store.transaction() as c:
                    c.execute("INSERT INTO settings(key,value,user_id) VALUES('forged','0',%s)", (uid3,))
                check("RLS拒绝伪造归属", False)
            except psycopg.errors.InsufficientPrivilege:
                check("RLS拒绝伪造归属", True)
        cfg = (await c2.get(prefix + "/wechat-config")).json()
        r = await c2.put(
            prefix + "/wechat-config",
            json={
                "token": "isolated-token-2",
                "callback_url": cfg["callback_url"],
                "revision": cfg["revision"],
            },
        )
        check("官方绑定明确成功", r.status_code == 200 and r.json()["state"] == "configured")
        check("配置接口不返回Token", "isolated-token-2" not in r.text)
        cfg3 = (await c3.get(prefix + "/wechat-config")).json()
        check(
            "拒绝跨用户重复Token",
            (
                await c3.put(
                    prefix + "/wechat-config",
                    json={
                        "token": "isolated-token-2",
                        "callback_url": cfg3["callback_url"],
                        "revision": cfg3["revision"],
                    },
                )
            ).status_code
            == 409,
        )
        check(
            "拒绝外站回调",
            (
                await c3.put(
                    prefix + "/wechat-config",
                    json={
                        "token": "isolated-token-3",
                        "callback_url": "https://example.invalid/callback",
                        "revision": cfg3["revision"],
                    },
                )
            ).status_code
            == 422,
        )
        gateway.mode = "uncertain"
        r = await c3.put(
            prefix + "/wechat-config",
            json={
                "token": "isolated-token-3",
                "callback_url": cfg3["callback_url"],
                "revision": cfg3["revision"],
            },
        )
        check("不确定绑定不报成功", r.status_code == 502)
        cfg3 = (await c3.get(prefix + "/wechat-config")).json()
        check("不确定状态持久化", cfg3["state"] == "uncertain")
        with user_scope(uid3):
            try:
                gateway.secret("wechat_token")
                check("不确定凭据禁止发送", False)
            except ProviderError:
                check("不确定凭据禁止发送", True)
        gateway.mode = "success"
        check(
            "人工重试确认绑定",
            (
                await c3.put(
                    prefix + "/wechat-config",
                    json={"token": "", "callback_url": cfg3["callback_url"], "revision": cfg3["revision"]},
                )
            ).status_code
            == 200,
        )
        payload = {
            "Appid": "multiuser-isolation",
            "Wxid": "wxid_test_isolated",
            "TypeName": "AddMsg",
            "Data": {
                "NewMsgId": "99999999001",
                "MsgType": 1,
                "Content": "isolated",
                "FromUserName": "wxid_test_sender",
                "ToUserName": "wxid_test_isolated",
                "IsTest": True,
            },
        }
        for client_ in (c2, c3):
            address = (await client_.get(prefix + "/wechat-config")).json()["callback_url"]
            check("专属回调接收", (await client_.post(address, json=payload)).status_code == 200)
            check("回调重复接收", (await client_.post(address, json=payload)).status_code == 200)
        for uid in (uid2, uid3):
            with user_scope(uid):
                check("相同平台消息按用户去重", store.query("SELECT count(*) AS n FROM events")[0]["n"] == 1)
        check(
            "错误回调密钥拒绝",
            (await c1.post("/wechatapi/callback/unknown", json=payload)).status_code == 404,
        )
        check("原用户模型配置保留", (await c1.get(prefix + "/settings")).json() == original_settings)
        check(
            "原用户账号保留",
            [
                {k: v for k, v in row.items() if k != "server_time"}
                for row in (await c1.get(prefix + "/accounts")).json()
            ]
            == [{k: v for k, v in row.items() if k != "server_time"} for row in original],
        )
        r = await c3.post(
            prefix + "/auth/login",
            json={
                "channel": "email",
                "identifier": "PERSON@EXAMPLE.INVALID",
                "method": "password",
                "password": "test-only-passphrase",
            },
        )
        check("邮箱密码登录", r.status_code == 200)
        c3.headers["X-CSRF-Token"] = r.json()["csrf"]
        r = await c2.post(
            prefix + "/auth/login",
            json={"method": "password", "phone": "13900000000", "password": "test-only-passphrase"},
        )
        check("旧手机号密码请求兼容", r.status_code == 200)
        c2.headers["X-CSRF-Token"] = r.json()["csrf"]
        check(
            "用户名不可登录",
            (
                await c2.post(
                    prefix + "/auth/login",
                    json={
                        "channel": "email",
                        "identifier": "同名用户",
                        "method": "password",
                        "password": "test-only-passphrase",
                    },
                )
            ).status_code
            == 422,
        )
        now[0] += 61
        for channel, target, client_ in (
            ("phone", "13900000000", c2),
            ("email", "person@example.invalid", c3),
        ):
            r = await client_.post(
                prefix + "/auth/code/send",
                json={"channel": channel, "identifier": target, "purpose": "login"},
            )
            check("发送登录验证码" + channel, r.status_code == 200)
            cid = r.json()["challenge_id"]
            code = "123456" if channel == "phone" else email.codes[target]
            r = await client_.post(
                prefix + "/auth/login",
                json={
                    "channel": channel,
                    "identifier": target,
                    "method": "code",
                    "challenge_id": cid,
                    "code": code,
                },
            )
            check("验证码登录" + channel, r.status_code == 200)
            client_.headers["X-CSRF-Token"] = r.json()["csrf"]
        check(
            "验证码登录授权改密",
            (
                await c2.put(
                    prefix + "/auth/password",
                    json={"proof": "recent_code", "new_password": "new-test-passphrase"},
                )
            ).status_code
            == 200,
        )
        check("改密撤销本用户会话", (await c2.get(prefix + "/auth/session")).status_code == 401)
        check(
            "改密不撤销他人会话",
            (await c3.get(prefix + "/auth/session")).status_code == 200
            and (await c1.get(prefix + "/auth/session")).status_code == 200,
        )
        task = asyncio.create_task(database_call(lambda: time.sleep(0.15)))
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        check("取消后连接可用", bool(await database_call(store.health)))

        def hold_connection():
            with store.connect() as c:
                time.sleep(0.2)
                return c.execute("SELECT 1").fetchone()[0]

        waiting = asyncio.create_task(database_call(hold_connection))
        await asyncio.sleep(0.02)
        waiting.cancel()
        try:
            await waiting
        except asyncio.CancelledError:
            pass
        check("取消事务归还连接", store.pool.get_stats()["pool_available"] > 0)
        from concurrent.futures import ThreadPoolExecutor

        def overload():
            try:
                return hold_connection()
            except Exception:
                return 0

        with ThreadPoolExecutor(max_workers=25) as executor:
            results = list(executor.map(lambda _: overload(), range(25)))
        check("连接池超量有界反馈", 0 in results and 1 in results)
        check("连接池未超过总容量", store.pool.get_stats()["pool_size"] <= 4)
        jobs = [asyncio.create_task(service.password_work(time.sleep, 2.3)) for _ in range(2)]
        await asyncio.sleep(0.03)
        from fastapi import HTTPException

        try:
            await service.password_work(time.sleep, 0.01)
            check("哈希并发超量有界反馈", False)
        except HTTPException as error:
            check("哈希并发超量有界反馈", error.status_code == 429)
        await asyncio.gather(*jobs)
        check("哈希额度归还", service.password_slots._value == 2)
        now[0] += 61
        r = await c3.post(
            prefix + "/auth/code/send",
            json={"channel": "email", "identifier": "person@example.invalid", "purpose": "password"},
        )
        check("发送会话绑定改密验证码", r.status_code == 200)
        cid = r.json()["challenge_id"]
        code = email.codes["person@example.invalid"]
        r = await c3.post(
            prefix + "/auth/login",
            json={
                "channel": "email",
                "identifier": "person@example.invalid",
                "method": "code",
                "challenge_id": cid,
                "code": code,
            },
        )
        check("验证码用途不可混用", r.status_code == 400)
        now[0] += 301
        r = await c3.put(
            prefix + "/auth/password",
            json={
                "proof": "code",
                "new_password": "another-test-password",
                "challenge_id": cid,
                "code": code,
            },
        )
        check("过期验证码拒绝", r.status_code == 400)
        r = await c3.post(
            prefix + "/auth/code/send",
            json={"channel": "email", "identifier": "person@example.invalid", "purpose": "login"},
        )
        check("重新发送登录验证码", r.status_code == 200)
        cid = r.json()["challenge_id"]
        for attempt in range(5):
            r = await c3.post(
                prefix + "/auth/login",
                json={
                    "channel": "email",
                    "identifier": "person@example.invalid",
                    "method": "code",
                    "challenge_id": cid,
                    "code": "000000",
                },
            )
            check("验证码错误次数限制", r.status_code == 400)
        with store.connect() as c:
            check(
                "五次失败挑战终止",
                c.execute("SELECT state FROM auth_codes WHERE id=%s", (cid,)).fetchone()[0] == "failed",
            )
        check(
            "设置API不暴露统一语音配置",
            "media_models" not in (await c3.get(prefix + "/settings")).json(),
        )
        with store.transaction() as c:
            c.execute("DELETE FROM worker_lock")
        worker = Worker(app.state.manager, gateway, clock=lambda: now[0])
        job = asyncio.create_task(worker.run())
        await asyncio.sleep(1.5)
        job.cancel()
        await job
        for uid in (1, uid2, uid3):
            with user_scope(uid):
                check(
                    "Worker用户循环无内部异常",
                    store.query("SELECT count(*) AS n FROM audit WHERE action='tenant_cycle_failed'")[0]["n"]
                    == 0,
                )
        process_tests(store, uid2)
        print(json.dumps({"passed": len(checks), "checks": checks}, ensure_ascii=False))
    finally:
        for c in (c1, c2, c3):
            await c.aclose()
        await gateway.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
