"""服务器隔离库：验证码单入口自动注册、事务原子性及独立进程故障恢复。"""

import asyncio
import multiprocessing as mp
import os
import re

import pytest
from fastapi.testclient import TestClient
from qingda.app import create_app
from qingda.auth import COOKIE
from test_authentication import BASE, Sms, setup, sms_login

TARGET = "13900000008"


def prepare(http, target=TARGET, channel="phone"):
    result = http.post(BASE + "/code/send", json={"channel": channel, "identifier": target})
    assert result.status_code == 200, result.text
    return dict(channel=channel, identifier=target, challenge_id=result.json()["challenge_id"], code="123456")


@pytest.mark.parametrize("channel,target", [("phone", TARGET), ("email", " New@Example.com ")])
def test_first_login_creates_only_after_verification_and_keeps_nickname(tmp_path, channel, target):
    http, store, config, _, clock = setup(tmp_path)

    class Email:
        async def send(self, target, code):
            self.code = code

    config.smtp_host, config.smtp_from = "fixture.invalid", "fixture@example.com"
    http.app.state.authentication.email = email = Email()
    payload = prepare(http, target, channel)
    if channel == "email":
        payload["code"] = email.code
    assert len(store.query("SELECT * FROM users")) == 1
    assert (
        http.post(
            BASE + "/login", json={**payload, "code": "000000" if payload["code"] != "000000" else "111111"}
        ).status_code
        == 400
    )
    assert len(store.query("SELECT * FROM users")) == 1
    registered = http.post(BASE + "/login", json=payload)
    assert registered.status_code == 200, registered.text
    session = registered.json()
    assert session["user_id"] != 1 and re.fullmatch(r"用户[A-Z0-9]{6}", session["username"])
    assert session["identifier"] == target.strip().lower()
    assert http.get("/wechat/api/v1/accounts").json() == []
    assert not http.get("/wechat/api/v1/settings").json()["model_key_configured"]
    users = store.query("SELECT * FROM users ORDER BY id")
    assert users[0]["callback_key_hash"] != users[1]["callback_key_hash"]
    assert users[1]["wechat_token"] is None
    assert http.post(BASE + "/login", json=payload).status_code == 400
    clock.advance(61)
    again = prepare(http, target, channel)
    if channel == "email":
        again["code"] = email.code
    repeat = http.post(BASE + "/login", json=again).json()
    assert (repeat["user_id"], repeat["username"]) == (session["user_id"], session["username"])
    assert (
        http.patch(
            "/wechat/api/v1/profile", headers={"X-CSRF-Token": repeat["csrf"]}, json={"username": "自选昵称"}
        ).status_code
        == 200
    )
    clock.advance(61)
    again = prepare(http, target, channel)
    if channel == "email":
        again["code"] = email.code
    assert http.post(BASE + "/login", json=again).json()["username"] == "自选昵称"
    assert len(store.query("SELECT * FROM users")) == 2


def test_expired_code_and_retired_registration_protocol_cannot_create_user(tmp_path):
    http, store, _, _, clock = setup(tmp_path)
    payload = prepare(http)
    assert (
        http.post(BASE + "/code/send", json={"identifier": TARGET, "purpose": "register"}).status_code == 422
    )
    assert http.post(BASE + "/register", json=payload).status_code == 404
    assert http.post(BASE + "/login", json={**payload, "username": "伪造昵称"}).status_code == 422
    assert http.post(BASE + "/login", json={**payload, "purpose": "contact"}).status_code == 422
    clock.advance(301)
    assert http.post(BASE + "/login", json=payload).status_code == 400
    assert len(store.query("SELECT * FROM users")) == 1


def test_registration_switch_only_blocks_new_users_and_is_rechecked(tmp_path):
    http, store, config, _, clock = setup(tmp_path)
    payload = prepare(http)
    config.registration_enabled = False
    assert http.post(BASE + "/login", json=payload).status_code == 503
    clock.advance(61)
    assert http.post(BASE + "/code/send", json={"identifier": TARGET}).status_code == 503
    assert sms_login(http).status_code == 200
    assert len(store.query("SELECT * FROM users")) == 1


def process_login(config, now, payload, mode, event, queue):
    class InterruptedSms(Sms):
        async def verify(self, phone, cid, code):
            if mode == "verify-crash":
                os._exit(17)
            if mode == "verify-cancel":
                raise asyncio.CancelledError()
            return await super().verify(phone, cid, code)

    if mode.startswith("transaction-"):
        import qingda.auth as auth

        initialize = auth.initialize_user

        def interrupt(conn, uid):
            initialize(conn, uid)
            if mode == "transaction-crash":
                os._exit(17)
            raise asyncio.CancelledError()

        auth.initialize_user = interrupt
    app = create_app(config, sms_provider=InterruptedSms(), auth_clock=lambda: now)
    try:
        if event is not None:
            assert event.wait(15)
        result = TestClient(app).post(BASE + "/login", json=payload)
        queue.put((result.status_code, COOKIE in result.cookies))
    except BaseException:
        if mode.endswith("cancel"):
            queue.put("cancelled")
        else:
            raise
    finally:
        app.state.manager.store.close()


def test_two_processes_create_one_user_and_one_session(tmp_path):
    http, store, config, _, clock = setup(tmp_path)
    payload = prepare(http)
    ctx = mp.get_context("spawn")
    event, queue = ctx.Event(), ctx.Queue()
    children = [
        ctx.Process(target=process_login, args=(config, clock.value, payload, "normal", event, queue))
        for _ in range(2)
    ]
    for child in children:
        child.start()
    event.set()
    for child in children:
        child.join(25)
        assert child.exitcode == 0
    assert sorted(queue.get(timeout=3) for _ in children) == [(200, True), (400, False)]
    assert len(store.query("SELECT * FROM users WHERE identifier=%s", (TARGET,))) == 1
    assert store.query("SELECT count(*) FROM auth_sessions WHERE user_id!=1")[0]["count"] == 1
    assert (
        store.query("SELECT state FROM auth_codes WHERE id=%s", (payload["challenge_id"],))[0]["state"]
        == "consumed"
    )


@pytest.mark.parametrize("mode", ["verify-crash", "verify-cancel", "transaction-crash", "transaction-cancel"])
def test_process_interruption_does_not_create_partial_user_or_replay(tmp_path, mode):
    http, store, config, _, clock = setup(tmp_path)
    payload = prepare(http)
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    child = ctx.Process(target=process_login, args=(config, clock.value, payload, mode, None, queue))
    child.start()
    child.join(25)
    assert child.exitcode == (17 if mode.endswith("crash") else 0)
    if mode.endswith("cancel"):
        assert queue.get(timeout=3) == "cancelled"
    assert len(store.query("SELECT * FROM users")) == 1
    assert store.query("SELECT count(*) FROM user_contacts")[0]["count"] == 1
    assert store.query("SELECT count(*) FROM auth_sessions")[0]["count"] == 0
    assert (
        store.query("SELECT state FROM auth_codes WHERE id=%s", (payload["challenge_id"],))[0]["state"]
        == "verifying"
    )
    restarted = TestClient(create_app(config, sms_provider=Sms(), auth_clock=clock))
    assert restarted.post(BASE + "/login", json=payload).status_code == 400
    clock.advance(301)
    result = restarted.post(BASE + "/login", json=prepare(restarted))
    assert result.status_code == 200
    assert len(store.query("SELECT * FROM users")) == 2
    assert store.query("SELECT count(*) FROM auth_sessions")[0]["count"] == 1
