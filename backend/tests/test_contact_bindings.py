"""服务器隔离库换绑契约；短信/邮件替身不证明真实收信。"""

import asyncio
import multiprocessing as mp
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_authentication import BASE, PHONE, Sms, code_login, setup, sms_login
from wechat_manager.app import create_app
from wechat_manager.auth import COOKIE


def authenticated(tmp_path):
    http, store, config, sms, clock = setup(tmp_path)
    session = sms_login(http).json()
    return http, store, config, sms, clock, session


def send_contact(http, session, target="13900000009", channel="phone", **extra):
    return http.post(
        BASE + "/contacts/code/send",
        headers={"X-CSRF-Token": session["csrf"]},
        json={"channel": channel, "identifier": target, **extra},
    )


def prepare(http, session, target="13900000009", channel="phone"):
    sent = send_contact(http, session, target, channel)
    assert sent.status_code == 200, sent.text
    return dict(
        channel=channel,
        identifier=target,
        challenge_id=sent.json()["challenge_id"],
        code="123456",
    )


def change(http, session, payload):
    return http.put(BASE + "/contacts", headers={"X-CSRF-Token": session["csrf"]}, json=payload)


def test_replace_phone_preserves_identity_and_data_revokes_old_sessions(tmp_path):
    http, store, _, _, _, session = authenticated(tmp_path)
    other = TestClient(http.app)
    sms_login(other)
    old = store.query("SELECT * FROM users WHERE id=1")[0]
    result = change(http, session, prepare(http, session))
    assert result.status_code == 200
    value = result.json()
    assert value["user_id"] == session["user_id"] and value["phone"] == "13900000009"
    assert value["csrf"] != session["csrf"] and value["email"] == ""
    assert http.get(BASE + "/session").json()["phone"] == "13900000009"
    assert other.get(BASE + "/session").status_code == 401
    latest = store.query("SELECT * FROM users WHERE id=1")[0]
    for key in ("id", "callback_key", "callback_url", "wechat_token", "username"):
        assert latest[key] == old[key]
    assert latest["revision"] == old["revision"] + 1
    fresh = TestClient(http.app)
    freed = code_login(fresh, PHONE)
    assert freed.status_code == 200 and freed.json()["user_id"] != session["user_id"]
    assert fresh.get("/wechat/api/v1/accounts").json() == []
    assert code_login(fresh, value["phone"]).status_code == 200


def test_bind_then_replace_email_both_contacts_login_and_old_email_stops(tmp_path):
    http, store, _, _, clock, session = authenticated(tmp_path)
    service = http.app.state.authentication

    class Email:
        codes = {}

        async def send(self, target, code):
            self.codes[target] = code

    service.config.smtp_host = "isolated.invalid"
    service.config.smtp_from = "fixture@example.com"
    service.config.smtp_username = "fixture"
    service.config.smtp_password = "fixture"
    service.email = Email()
    for old, target in ((None, "first@example.com"), ("first@example.com", "next@example.com")):
        clock.advance(61)
        payload = prepare(http, session, target, "email")
        payload["code"] = service.email.codes[target]
        result = change(http, session, payload)
        assert result.status_code == 200, result.text
        session = result.json()
        assert session["phone"] == PHONE and session["email"] == target
        fresh = TestClient(http.app)
        assert code_login(fresh, PHONE).status_code == 200
        assert code_login(fresh, target, "email").status_code == 200
        if old:
            freed = code_login(fresh, old, "email")
            assert freed.status_code == 200 and freed.json()["user_id"] != session["user_id"]
    assert len(store.query("SELECT * FROM user_contacts WHERE user_id=1")) == 2
    other = TestClient(http.app)
    assert (
        other.post(
            BASE + "/code/send",
            json={"channel": "email", "identifier": "next@example.com", "purpose": "register"},
        ).status_code
        == 422
    )


def test_email_registered_user_binds_phone_with_only_new_phone_code(tmp_path):
    http, store, _, _, _, _ = authenticated(tmp_path)
    service = http.app.state.authentication

    class Email:
        async def send(self, target, code):
            self.code = code

    service.config.smtp_host = "isolated.invalid"
    service.config.smtp_from = "fixture@example.com"
    service.config.smtp_username = "fixture"
    service.config.smtp_password = "fixture"
    service.email = Email()
    client = TestClient(http.app)
    sent = client.post(
        BASE + "/code/send",
        json={
            "channel": "email",
            "identifier": "primary@example.com",
            "purpose": "login",
        },
    ).json()
    registered = client.post(
        BASE + "/login",
        json={
            "channel": "email",
            "identifier": "primary@example.com",
            "challenge_id": sent["challenge_id"],
            "code": service.email.code,
        },
    )
    assert registered.status_code == 200
    session = registered.json()
    assert session["phone"] == ""
    result = change(client, session, prepare(client, session))
    assert result.status_code == 200, result.text
    assert result.json()["phone"] == "13900000009"
    assert result.json()["email"] == result.json()["identifier"] == "primary@example.com"
    assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE


def test_wrong_new_code_and_expiry_do_not_modify_contact(tmp_path):
    http, store, _, _, clock, session = authenticated(tmp_path)
    payload = prepare(http, session)
    assert change(http, session, {**payload, "code": "000000"}).status_code == 400
    assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE
    clock.advance(301)
    assert change(http, session, payload).status_code == 400
    assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE


def test_new_code_is_session_destination_channel_and_purpose_bound(tmp_path):
    http, _, _, _, _, session = authenticated(tmp_path)
    payload = prepare(http, session)
    other = TestClient(http.app)
    other_session = sms_login(other).json()
    assert change(other, other_session, payload).status_code == 400
    assert (
        http.post(
            BASE + "/login",
            json={
                "method": "code",
                "identifier": payload["identifier"],
                "challenge_id": payload["challenge_id"],
                "code": "123456",
            },
        ).status_code
        == 400
    )
    assert (
        http.post(
            BASE + "/code/send",
            headers={"X-CSRF-Token": session["csrf"]},
            json={"purpose": "contact", "identifier": "13900000008"},
        ).status_code
        == 403
    )
    assert change(http, session, {**payload, "user_id": 2}).status_code == 422
    assert change(http, session, {**payload, "identifier": "13900000008"}).status_code == 400
    assert (
        change(http, session, {**payload, "channel": "email", "identifier": "new@example.com"}).status_code
        == 400
    )
    result = change(http, session, payload)
    assert result.status_code == 200
    assert change(http, result.json(), payload).status_code == 400


def test_contact_change_needs_only_new_contact_code_after_login(tmp_path):
    http, store, _, sms, clock = setup(tmp_path)
    session = sms_login(http).json()
    clock.advance(301)
    assert "password_set" not in session
    sent_before = len(sms.sent)
    result = change(http, session, prepare(http, session))
    assert result.status_code == 200, result.text
    assert len(sms.sent) == sent_before + 1 and sms.sent[-1][0] == "13900000009"
    assert "password_hash" not in store.query("SELECT * FROM users WHERE id=1")[0]


@pytest.mark.parametrize("failure", ["anonymous", "csrf", "origin", "logout", "expired"])
def test_contact_send_and_change_require_valid_session_and_csrf(tmp_path, failure):
    http, store, _, sms, clock, session = authenticated(tmp_path)
    payload = prepare(http, session)
    client = TestClient(http.app) if failure == "anonymous" else http
    headers = {"X-CSRF-Token": session["csrf"]}
    expected = 401
    if failure == "csrf":
        headers = {}
        expected = 403
    elif failure == "origin":
        headers["Origin"] = "https://evil.invalid"
        expected = 403
    elif failure == "logout":
        assert http.post(BASE + "/logout", headers=headers).status_code == 200
    elif failure == "expired":
        clock.advance(7 * 86400 + 1)
    sends, verifies = len(sms.sent), len(sms.verified)
    assert (
        client.post(
            BASE + "/contacts/code/send",
            headers=headers,
            json={"channel": "phone", "identifier": "13900000008"},
        ).status_code
        == expected
    )
    assert client.put(BASE + "/contacts", headers=headers, json=payload).status_code == expected
    assert len(sms.sent) == sends and len(sms.verified) == verifies
    assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE


def test_occupied_destination_and_uncertain_delivery_keep_original(tmp_path):
    http, store, _, sms, clock, session = authenticated(tmp_path)

    def send(target):
        return http.post(
            BASE + "/contacts/code/send",
            headers={"X-CSRF-Token": session["csrf"]},
            json={"channel": "phone", "identifier": target},
        )

    assert send(PHONE).status_code == 409
    sms.send_error = True
    assert send("13900000009").status_code == 503
    assert store.query("SELECT state FROM auth_codes WHERE purpose='contact'")[0]["state"] == "failed"
    assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE
    clock.advance(61)
    sms.send_error = False
    assert send("13900000009").status_code == 200


def process_send_contact(config, now, token, session, event, queue):
    provider = Sms()
    app = create_app(config, sms_provider=provider, auth_clock=lambda: now)
    client = TestClient(app)
    client.cookies.set(COOKIE, token, domain="testserver.local", path="/wechat/")
    event.wait(10)
    response = send_contact(client, session)
    queue.put((response.status_code, len(provider.sent)))
    app.state.manager.store.close()


def test_two_independent_processes_share_send_limit_without_duplicate_delivery(tmp_path):
    http, store, config, _, clock, session = authenticated(tmp_path)
    ctx = mp.get_context("spawn")
    event, queue = ctx.Event(), ctx.Queue()
    children = [
        ctx.Process(
            target=process_send_contact,
            args=(config, clock.value, http.cookies.get(COOKIE), session, event, queue),
        )
        for _ in range(2)
    ]
    for child in children:
        child.start()
    event.set()
    for child in children:
        child.join(25)
        assert child.exitcode == 0
    results = [queue.get(timeout=3) for _ in children]
    assert sorted(status for status, _ in results) == [200, 429]
    assert sum(sent for _, sent in results) == 1
    assert len(store.query("SELECT * FROM auth_codes WHERE purpose='contact'")) == 1
    assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE


def process_change(config, now, token, session, payload, event, queue):
    app = create_app(config, sms_provider=Sms(), auth_clock=lambda: now)
    client = TestClient(app)
    client.cookies.set(COOKIE, token, domain="testserver.local", path="/wechat/")
    event.wait(10)
    response = change(client, session, payload)
    queue.put(response.status_code)
    app.state.manager.store.close()


def test_two_independent_processes_cannot_replace_twice(tmp_path):
    http, store, config, _, clock, session = authenticated(tmp_path)
    payload = prepare(http, session)
    revision = store.query("SELECT revision FROM users WHERE id=1")[0]["revision"]
    ctx = mp.get_context("spawn")
    event, queue = ctx.Event(), ctx.Queue()
    children = [
        ctx.Process(
            target=process_change,
            args=(config, clock.value, http.cookies.get(COOKIE), session, payload, event, queue),
        )
        for _ in range(2)
    ]
    for child in children:
        child.start()
    event.set()
    for child in children:
        child.join(25)
        assert child.exitcode == 0
    statuses = sorted(queue.get(timeout=3) for _ in children)
    assert statuses[0] == 200 and statuses[1] in (400, 401)
    assert store.query("SELECT revision FROM users WHERE id=1")[0]["revision"] == revision + 1
    assert store.query("SELECT count(*) FROM auth_sessions WHERE user_id=1")[0]["count"] == 1


def process_interrupt(config, now, token, session, payload, mode, queue):
    class InterruptedSms(Sms):
        async def verify(self, phone, cid, code):
            if mode == "crash":
                os._exit(17)
            raise asyncio.CancelledError()

    app = create_app(config, sms_provider=InterruptedSms(), auth_clock=lambda: now)
    client = TestClient(app)
    client.cookies.set(COOKIE, token, domain="testserver.local", path="/wechat/")
    try:
        change(client, session, payload)
    except BaseException:
        queue.put("cancelled")
    finally:
        app.state.manager.store.close()


def test_process_crash_and_cancel_do_not_replay_and_fresh_request_recovers(tmp_path):
    http, store, config, _, clock, session = authenticated(tmp_path)
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    for mode in ("crash", "cancel"):
        payload = prepare(http, session)
        child = ctx.Process(
            target=process_interrupt,
            args=(config, clock.value, http.cookies.get(COOKIE), session, payload, mode, queue),
        )
        child.start()
        child.join(25)
        assert child.exitcode == (17 if mode == "crash" else 0)
        if mode == "cancel":
            assert queue.get(timeout=3) == "cancelled"
        assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE
        assert (
            store.query("SELECT state FROM auth_codes WHERE id=%s", (payload["challenge_id"],))[0]["state"]
            == "verifying"
        )
        assert change(http, session, payload).status_code == 400
        clock.advance(301)
    assert change(http, session, prepare(http, session)).status_code == 200


def test_other_users_contact_cannot_be_taken_and_registration_checks_secondary(tmp_path):
    http, store, _, _, _, session = authenticated(tmp_path)
    other = TestClient(http.app)
    sent = other.post(BASE + "/code/send", json={"identifier": "13900000007", "purpose": "login"}).json()
    registered = other.post(
        BASE + "/login",
        json={
            "identifier": "13900000007",
            "challenge_id": sent["challenge_id"],
            "code": "123456",
        },
    )
    assert registered.status_code == 200
    result = http.post(
        BASE + "/contacts/code/send",
        headers={"X-CSRF-Token": session["csrf"]},
        json={"channel": "phone", "identifier": "13900000007"},
    )
    assert result.status_code == 409
    assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE
    assert other.get(BASE + "/session").json()["user_id"] != session["user_id"]


def test_destination_claimed_after_sending_is_rejected_without_losing_original(tmp_path):
    http, store, _, _, clock, session = authenticated(tmp_path)
    payload = prepare(http, session)
    clock.advance(61)
    other = TestClient(http.app)
    sent = other.post(
        BASE + "/code/send", json={"identifier": payload["identifier"], "purpose": "login"}
    ).json()
    registered = other.post(
        BASE + "/login",
        json={
            "identifier": payload["identifier"],
            "challenge_id": sent["challenge_id"],
            "code": "123456",
        },
    )
    assert registered.status_code == 200
    assert change(http, session, payload).status_code == 409
    assert http.get(BASE + "/session").json()["phone"] == PHONE
    assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE
    assert (
        store.query("SELECT state FROM auth_codes WHERE id=%s", (payload["challenge_id"],))[0]["state"]
        == "verifying"
    )


def test_logout_during_external_verification_cannot_commit(tmp_path):
    http, store, _, sms, _, session = authenticated(tmp_path)
    payload = prepare(http, session)
    client = TestClient(http.app)
    client.cookies.set(COOKIE, http.cookies.get(COOKIE), domain="testserver.local", path="/wechat/")
    sms.entered, sms.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(2) as executor:
        pending = executor.submit(change, client, session, payload)
        assert sms.entered.wait(5)
        assert http.post(BASE + "/logout", headers={"X-CSRF-Token": session["csrf"]}).status_code == 200
        sms.release.set()
        assert pending.result(timeout=5).status_code == 401
    assert store.query("SELECT identifier FROM users WHERE id=1")[0]["identifier"] == PHONE


def test_contact_code_rate_limits_and_failed_attempts_are_preserved(tmp_path):
    http, store, _, sms, clock, session = authenticated(tmp_path)
    payload = prepare(http, session)
    assert send_contact(http, session).status_code == 429
    assert len(sms.sent) == 2
    for _ in range(5):
        assert change(http, session, {**payload, "code": "000000"}).status_code == 400
    assert change(http, session, payload).status_code == 400
    assert (
        store.query("SELECT state FROM auth_codes WHERE id=%s", (payload["challenge_id"],))[0]["state"]
        == "failed"
    )
    clock.advance(61)
    assert change(http, session, prepare(http, session)).status_code == 200
