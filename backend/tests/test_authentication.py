"""认证接口契约；仅使用明确标记的隔离号码/短信替身，不证明真实短信送达。"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from wechat_manager.app import create_app
from wechat_manager.auth import COOKIE
from wechat_manager.config import Config
from wechat_manager.security import create_key
from wechat_manager.sms import AliyunSms, SmsUnavailable
from wechat_manager.store import Store

BASE = "/wechat/api/v1/auth"
PHONE = "13900000000"


class Clock:
    value = 2_000_000_000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Sms:
    """仅测试用替身；生产适配器不存在固定验证码。"""

    def __init__(self):
        self.sent, self.verified = [], []
        self.send_error = self.verify_error = False
        self.entered = self.release = None

    async def send(self, phone, cid):
        self.sent.append((phone, cid))
        if self.send_error:
            raise SmsUnavailable()

    async def verify(self, phone, cid, code):
        self.verified.append((phone, cid))
        if self.entered:
            self.entered.set()
            await asyncio.to_thread(self.release.wait, 5)
        if self.verify_error:
            raise SmsUnavailable()
        return code == "123456"


def setup(tmp_path, *, configured=True):
    create_key(tmp_path / "master.key")
    config = Config(
        tmp_path,
        tmp_path / "master.key",
        public_url="http://testserver",
        cookie_secure=False,
        aliyun_access_key_id="test-key" if configured else "",
        aliyun_access_key_secret="test-secret",
        aliyun_sms_sign_name="test-sign",
        aliyun_sms_template_code="test-template",
    )
    sms, clock = Sms(), Clock()
    app = create_app(config, sms_provider=sms, auth_clock=clock)
    return TestClient(app), app.state.manager.store, config, sms, clock


def send(http, purpose="login", headers=None):
    return http.post(BASE + "/code/send", json={"identifier": PHONE, "purpose": purpose}, headers=headers)


def sms_login(http, cid=None, code="123456"):
    if cid is None:
        http.app.state.authentication.clock.advance(61)
        cid = send(http).json()["challenge_id"]
    return http.post(
        BASE + "/login", json={"method": "code", "identifier": PHONE, "challenge_id": cid, "code": code}
    )


def code_login(http, target=PHONE, channel="phone"):
    service = http.app.state.authentication
    service.clock.advance(61)
    response = http.post(BASE + "/code/send", json={"channel": channel, "identifier": target})
    if response.status_code != 200:
        return response
    return http.post(
        BASE + "/login",
        json={
            "method": "code",
            "channel": channel,
            "identifier": target,
            "challenge_id": response.json()["challenge_id"],
            "code": "123456" if channel == "phone" else service.email.codes[target.lower()],
        },
    )


def test_code_login_replay_cookie_and_restart_preserve_session(tmp_path):
    http, store, config, sms, clock = setup(tmp_path)
    cid = send(http).json()["challenge_id"]
    result = sms_login(http, cid)
    assert result.status_code == 200
    assert not {"password_set", "recent_code", "recent_sms", "sms_verified_until"} & result.json().keys()
    assert "HttpOnly" in result.headers["set-cookie"] and "SameSite=strict" in result.headers["set-cookie"]
    assert sms_login(http, cid).status_code == 400
    restarted = TestClient(create_app(config, sms_provider=sms, auth_clock=clock))
    restarted.cookies.update(http.cookies)
    assert restarted.get(BASE + "/session").json()["user_id"] == result.json()["user_id"]
    csrf = result.json()["csrf"]
    assert http.post(BASE + "/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert restarted.get(BASE + "/session").status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"method": "password", "identifier": PHONE, "password": "old-password"},
        {
            "method": "code",
            "identifier": PHONE,
            "password": "old-password",
            "challenge_id": "x" * 43,
            "code": "123456",
        },
        {"method": "code", "identifier": PHONE, "challenge_id": "x" * 43, "code": "abc123"},
    ],
)
def test_password_and_invalid_code_contracts_rejected_without_provider(tmp_path, payload):
    http, _, _, sms, _ = setup(tmp_path)
    assert http.post(BASE + "/login", json=payload).status_code == 422
    assert not sms.sent and not sms.verified
    assert http.put(BASE + "/password", json={"new_password": "old-password"}).status_code == 404
    assert send(http, "password").status_code == 422


def test_code_expiry_attempts_and_cooldown_survive_restart(tmp_path):
    http, _, config, sms, clock = setup(tmp_path)
    cid = send(http).json()["challenge_id"]
    for _ in range(5):
        assert sms_login(http, cid, "000000").status_code == 400
    assert sms_login(http, cid).status_code == 400
    assert len(sms.verified) == 5
    restarted = TestClient(create_app(config, sms_provider=sms, auth_clock=clock))
    limited = send(restarted)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) == 60
    clock.advance(61)
    next_id = send(restarted).json()["challenge_id"]
    clock.advance(301)
    assert sms_login(restarted, next_id).status_code == 400


def test_failed_send_consumes_quota_and_never_retries(tmp_path):
    http, _, _, sms, _ = setup(tmp_path)
    sms.send_error = True
    assert send(http).status_code == 503
    assert send(http).status_code == 429 and len(sms.sent) == 1


def test_target_hour_day_shared_across_ips_and_purposes(tmp_path):
    http, _, config, sms, clock = setup(tmp_path)
    for hour in (0, 1):
        for _ in range(5):
            assert send(http).status_code == 200
            clock.advance(61)
        if hour == 0:
            other = TestClient(
                create_app(config, sms_provider=sms, auth_clock=clock), client=("198.51.100.9", 10)
            )
            assert send(other).status_code == 429
            clock.advance(3601)
    other = TestClient(create_app(config, sms_provider=sms, auth_clock=clock), client=("192.0.2.9", 10))
    assert send(other).status_code == 429
    assert len(sms.sent) == 10


def test_ip_hour_and_day_count_invalid_send_and_aliases(tmp_path):
    http, _, _, sms, clock = setup(tmp_path)
    for hour in range(3):
        for i in range(10):
            path = "/code/send"
            assert http.post(BASE + path, json={"identifier": "bad"}).status_code == 422
        assert send(http).status_code == 429
        clock.advance(3601)
    assert send(http).status_code == 429 and not sms.sent


def test_client_forwarded_header_cannot_change_rate_subject(tmp_path):
    http, _, _, sms, _ = setup(tmp_path)
    for i in range(10):
        assert (
            http.post(
                BASE + "/code/send", json={"identifier": "bad"}, headers={"X-Forwarded-For": f"192.0.2.{i}"}
            ).status_code
            == 422
        )
    assert send(http).status_code == 429 and not sms.sent


def test_origin_and_auth_request_limit(tmp_path):
    http, _, _, sms, _ = setup(tmp_path)
    assert (
        http.post(
            BASE + "/code/send", json={"identifier": PHONE}, headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    for _ in range(29):
        assert http.get(BASE + "/options").status_code == 200
    assert http.get(BASE + "/options").status_code == 429 and not sms.sent


def test_concurrent_code_consumption_and_superseding_inflight_verification(tmp_path):
    http, _, _, sms, clock = setup(tmp_path)
    cid = send(http).json()["challenge_id"]
    sms.entered, sms.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(2) as executor:
        pending = executor.submit(sms_login, TestClient(http.app), cid)
        assert sms.entered.wait(5)
        assert sms_login(TestClient(http.app), cid).status_code == 400
        clock.advance(61)
        assert send(http).status_code == 200
        sms.release.set()
        assert pending.result(timeout=5).status_code == 400
    assert http.get(BASE + "/session").status_code == 401


async def test_aliyun_sdk_parameters_and_pass_not_transport_success(tmp_path):
    _, _, config, _, _ = setup(tmp_path)
    adapter = AliyunSms(config)
    calls = []

    async def send_sdk(request, runtime):
        calls.append(request)
        assert runtime.autoretry is False and runtime.max_attempts == 1
        assert request.return_verify_code is False and request.auto_retry == 0
        assert request.code_length == 6 and request.valid_time == 300 and request.duplicate_policy == 1
        assert "##code##" in request.template_param
        return SimpleNamespace(body=SimpleNamespace(code="OK", success=True, model=SimpleNamespace()))

    async def check_sdk(request, runtime):
        calls.append(request)
        return SimpleNamespace(
            body=SimpleNamespace(
                code="OK",
                success=True,
                model=SimpleNamespace(
                    verify_result="PASS" if request.verify_code != "000000" else "UNKNOWN",
                    out_id="different-challenge" if request.verify_code == "111111" else request.out_id,
                ),
            )
        )

    adapter.client = SimpleNamespace(
        send_sms_verify_code_with_options_async=send_sdk, check_sms_verify_code_with_options_async=check_sdk
    )
    await adapter.send(PHONE, "challenge")
    assert await adapter.verify(PHONE, "challenge", "000000") is False
    assert await adapter.verify(PHONE, "challenge", "111111") is False
    assert await adapter.verify(PHONE, "challenge", "123456") is True
    assert calls[0].scheme_name == calls[1].scheme_name


def test_concurrent_verification_can_issue_only_one_session(tmp_path):
    http, _, _, sms, _ = setup(tmp_path)
    cid = send(http).json()["challenge_id"]
    sms.entered, sms.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(2) as executor:
        pending = executor.submit(sms_login, TestClient(http.app), cid)
        assert sms.entered.wait(5)
        rejected = sms_login(TestClient(http.app), cid)
        assert rejected.status_code == 400 and COOKIE not in rejected.cookies
        sms.release.set()
        accepted = pending.result(timeout=5)
        assert accepted.status_code == 200 and COOKIE in accepted.cookies
    assert len(sms.verified) == 1


def test_invalid_send_payloads_count_towards_ip_hour_limit(tmp_path):
    http, _, _, sms, clock = setup(tmp_path)
    for _ in range(10):
        assert http.post(BASE + "/code/send", json={"identifier": "not-a-phone"}).status_code == 422
    clock.advance(61)
    assert send(http).status_code == 429
    assert not sms.sent


def test_sdk_errors_are_sanitized_and_no_retry(tmp_path):
    _, _, config, _, _ = setup(tmp_path)
    adapter = AliyunSms(config)
    calls = []

    async def failed(request, runtime):
        calls.append(1)
        raise RuntimeError("sensitive-provider-request-phone-and-code")

    adapter.client = SimpleNamespace(send_sms_verify_code_with_options_async=failed)
    with pytest.raises(SmsUnavailable) as error:
        asyncio.run(adapter.send(PHONE, "challenge"))
    assert str(error.value) == "" and calls == [1]


def test_mismatched_database_version_rejects_startup_without_history_writes(tmp_path):
    _, store, _, _, _ = setup(tmp_path)
    store.receive(b'{"TypeName":"Unknown","Data":{}}')
    before = store.events()
    with store.transaction() as conn:
        from wechat_manager.store import SCHEMA_VERSION

        conn.execute("INSERT INTO schema_versions VALUES(%s)", (SCHEMA_VERSION + 1,))
    with pytest.raises(RuntimeError, match="数据库版本不匹配"):
        Store(store.url)
    assert store.events() == before


def process_send(config, now, start, result, mode="normal"):
    class Provider(Sms):
        async def send(self, phone, cid):
            result.put(("supplier", cid))
            if mode == "crash":
                __import__("os")._exit(17)
            if mode == "cancel":
                raise asyncio.CancelledError()

    app = create_app(config, sms_provider=Provider(), auth_clock=lambda: now)
    client = TestClient(app)
    start.wait(10)
    try:
        result.put(("status", send(client).status_code))
    except BaseException:
        result.put(("cancelled", 0))
    finally:
        app.state.manager.store.close()


def test_two_processes_share_send_quota_and_crash_cancel_keep_reservation(tmp_path):
    import multiprocessing as mp

    http, store, config, sms, clock = setup(tmp_path)
    ctx = mp.get_context("spawn")
    start, result = ctx.Event(), ctx.Queue()
    children = [ctx.Process(target=process_send, args=(config, clock.value, start, result)) for _ in range(2)]
    for child in children:
        child.start()
    start.set()
    for child in children:
        child.join(25)
        assert child.exitcode == 0
    events = [result.get(timeout=3) for _ in range(3)]
    assert sorted(v for k, v in events if k == "status") == [200, 429]
    assert sum(k == "supplier" for k, _ in events) == 1
    for mode in ("crash", "cancel"):
        clock.advance(61)
        child = ctx.Process(target=process_send, args=(config, clock.value, start, result, mode))
        child.start()
        child.join(25)
        assert child.exitcode == (17 if mode == "crash" else 0)
        assert send(http).status_code == 429
    assert len(store.query("SELECT * FROM auth_codes")) == 3
    clock.advance(61)
    assert send(http).status_code == 200 and len(sms.sent) == 1


@pytest.mark.parametrize("channel,target", [("phone", "13900000008"), ("email", "new@example.com")])
def test_auto_registration_requires_code_and_keeps_user_isolated(tmp_path, channel, target):
    http, store, config, sms, clock = setup(tmp_path)

    class Email:
        async def send(self, target, code):
            self.code = code

    config.smtp_host = "fixture.invalid"
    config.smtp_from = "fixture@example.com"
    http.app.state.authentication.email = Email()
    sent = http.post(BASE + "/code/send", json={"channel": channel, "identifier": target, "purpose": "login"})
    assert sent.status_code == 200
    payload = {
        "channel": channel,
        "identifier": target,
        "challenge_id": sent.json()["challenge_id"],
        "code": "123456" if channel == "phone" else http.app.state.authentication.email.code,
    }
    assert http.post(BASE + "/login", json={**payload, "password": "forbidden"}).status_code == 422
    assert len(store.query("SELECT * FROM users")) == 1
    registered = http.post(BASE + "/login", json=payload)
    assert registered.status_code == 200 and registered.json()["user_id"] != 1
    assert http.get("/wechat/api/v1/accounts").json() == []
    assert http.post(BASE + "/login", json=payload).status_code == 400
    clock.advance(61)
    assert (
        http.post(
            BASE + "/code/send", json={"channel": channel, "identifier": target, "purpose": "login"}
        ).status_code
        == 200
    )


def test_limits_require_positive_configuration(tmp_path):
    _, _, config, _, _ = setup(tmp_path)
    config.auth_code_ip_daily_limit = 0
    with pytest.raises(ValueError, match="AUTH_CODE_IP_DAILY_LIMIT"):
        config.__post_init__()
