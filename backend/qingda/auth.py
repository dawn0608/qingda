"""多用户认证，验证码消费与会话签发共用短事务。"""

import hmac
import math
import re
import secrets
import time
from typing import Literal

import psycopg
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field

from .database_async import database_call
from .email_codes import EmailCodes
from .security import Vault, digest, new_session
from .sms import AliyunSms, SmsUnavailable
from .tenancy import current_user, initialize_user

PREFIX = "/wechat/api/v1/auth"
COOKIE = "qingda_session"
FAILURE = "邮箱、手机号或凭据不正确，请检查后重试"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SmsSend(Input):
    channel: Literal["email", "phone"] = "phone"
    identifier: str = Field(default="", max_length=254)
    purpose: Literal["login", "contact"] = "login"


class LoginInput(SmsSend):
    purpose: Literal["login"] = "login"
    method: Literal["code"] = "code"
    challenge_id: str = Field(min_length=32, max_length=64)
    code: str = Field(pattern=r"^[0-9]{6}$")


def identifier(payload):
    value = payload.identifier.strip().lower()
    pattern = r"1[0-9]{10}" if payload.channel == "phone" else r"[^\s@<>]+@[^\s@<>.]+(?:\.[^\s@<>.]+)+"
    if not re.fullmatch(pattern, value):
        raise HTTPException(422, "请输入有效的邮箱或手机号")
    return value


def limited(until, now):
    return HTTPException(
        429, "尝试次数过多，请稍后重试", headers={"Retry-After": str(max(1, math.ceil(until - now)))}
    )


class Authentication:
    def __init__(self, store, config, sms=None, clock=time.time, email=None):
        self.store, self.config, self.clock = store, config, clock
        self.sms = sms if sms is not None else AliyunSms(config)
        self.email = email if email is not None else EmailCodes(config)
        self.secret = config.key_file.read_bytes()
        self.vault = Vault(config.key_file)

    def csrf(self, token):
        return hmac.new(self.secret, token.encode(), "sha256").hexdigest()

    def ip(self, request):
        return self.csrf(request.client.host if request.client else "unknown")

    def origin(self, request):
        origin = request.headers.get("Origin")
        if (origin and origin.rstrip("/") != self.config.public_url) or request.headers.get(
            "Sec-Fetch-Site"
        ) == "cross-site":
            raise HTTPException(403, "请求来源不符")

    def reserve(self, c, kind, subject, rules, now):
        times = [
            r[0]
            for r in c.execute(
                "SELECT at FROM auth_events WHERE kind=%s AND subject=%s AND at>%s ORDER BY at",
                (kind, subject, now - max(w for w, _ in rules)),
            )
        ]
        waits = []
        for window, maximum in rules:
            current = [at for at in times if at > now - window]
            if len(current) >= maximum:
                waits.append(current[-maximum] + window)
        if waits:
            raise limited(max(waits), now)
        c.execute("INSERT INTO auth_events(kind,subject,at) VALUES(%s,%s,%s)", (kind, subject, now))

    def ingress(self, request):
        now = self.clock()
        with self.store.transaction() as c:
            c.execute("DELETE FROM auth_events WHERE at<=%s", (now - 86400,))
            c.execute("DELETE FROM auth_codes WHERE expires_at<=%s", (now - 86400,))
            self.reserve(c, "ingress", self.ip(request), [(60, self.config.auth_request_ip_per_minute)], now)
            if request.url.path.endswith("/code/send"):
                self.reserve(
                    c,
                    "code_ip",
                    self.ip(request),
                    [
                        (3600, self.config.auth_code_ip_hourly_limit),
                        (86400, self.config.auth_code_ip_daily_limit),
                    ],
                    now,
                )

    def session_row(self, c, token):
        row = c.execute(
            "SELECT s.*,a.revision,a.user_id FROM sessions s JOIN auth_sessions a USING(id_hash) JOIN users u ON u.id=a.user_id AND u.revision=a.revision WHERE s.id_hash=%s AND s.expires_at>%s",
            (digest(token), self.clock()),
        ).fetchone()
        if not token or not row:
            raise HTTPException(401, "请登录管理台")
        return row

    def require(self, request: Request):
        token = request.cookies.get(COOKIE, "")
        with self.store.connect() as c:
            row = self.session_row(c, token)
        if current_user.get() != row["user_id"]:
            raise HTTPException(401, "请登录管理台")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            self.origin(request)
            csrf = request.headers.get("X-CSRF-Token", "")
            if not csrf or not hmac.compare_digest(digest(csrf), row["csrf_hash"]):
                raise HTTPException(403, "会话校验失败，请重新登录")
        return token

    def contacts(self, c, user_id):
        return {
            r["channel"]: r["identifier"]
            for r in c.execute("SELECT channel,identifier FROM user_contacts WHERE user_id=%s", (user_id,))
        }

    def describe(self, user, csrf, contacts=None):
        contacts = contacts or {user["channel"]: user["identifier"]}
        return dict(
            user_id=user["id"],
            channel=user["channel"],
            identifier=user["identifier"],
            phone=contacts.get("phone", ""),
            email=contacts.get("email", ""),
            username=user["username"],
            avatar_url=f"/wechat/api/v1/profile/avatar?v={user['avatar_revision']}"
            if user["avatar"]
            else None,
            csrf=csrf,
            sms_available=self.config.sms_configured,
            email_available=self.config.email_configured,
        )

    def issue(self, c, user):
        token, _ = new_session()
        csrf = self.csrf(token)
        c.execute("DELETE FROM sessions WHERE expires_at<=%s", (self.clock(),))
        c.execute(
            "INSERT INTO sessions VALUES(%s,%s,%s)", (digest(token), digest(csrf), self.clock() + 7 * 86400)
        )
        c.execute(
            "INSERT INTO auth_sessions(id_hash,revision,user_id) VALUES(%s,%s,%s)",
            (digest(token), user["revision"], user["id"]),
        )
        return token, self.describe(user, csrf, self.contacts(c, user["id"]))

    def response(self, issued):
        token, data = issued
        response = JSONResponse(data)
        response.set_cookie(
            COOKIE,
            token,
            max_age=7 * 86400,
            path="/wechat/",
            secure=self.config.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    def code_state(self, cid, state, previous):
        with self.store.transaction() as c:
            return c.execute(
                "UPDATE auth_codes SET state=%s WHERE id=%s AND state=%s AND expires_at>%s",
                (state, cid, previous, self.clock()),
            ).rowcount

    async def send(self, payload, request, *, contact_change=False):
        self.origin(request)
        target = identifier(payload)
        session_hash = ""
        token = ""
        if payload.purpose == "contact":
            token = self.require(request)
            session_hash = digest(token)
        if payload.purpose == "contact" and not contact_change:
            raise HTTPException(403, "请通过联系方式设置获取验证码")
        cid = secrets.token_urlsafe(32)
        now = self.clock()
        code = f"{secrets.randbelow(1000000):06d}"

        def reserve():
            with self.store.transaction() as c:
                user = c.execute(
                    "SELECT user_id FROM user_contacts WHERE channel=%s AND identifier=%s",
                    (payload.channel, target),
                ).fetchone()
                if payload.purpose == "contact":
                    session = self.session_row(c, token)
                    if session["user_id"] != current_user.get():
                        raise HTTPException(401, "请重新登录")
                    if user:
                        raise HTTPException(409, "该邮箱或手机号已绑定，请使用其他联系方式")
                elif not user and not self.config.registration_enabled:
                    raise HTTPException(503, "暂不支持新账号首次登录")
                available = (
                    self.config.sms_configured if payload.channel == "phone" else self.config.email_configured
                )
                if not available:
                    raise HTTPException(503, "验证码服务暂不可用，请稍后再试")
                self.reserve(
                    c,
                    "code_target",
                    self.csrf(target),
                    [
                        (self.config.auth_code_interval_seconds, 1),
                        (3600, self.config.auth_code_target_hourly_limit),
                        (86400, self.config.auth_code_target_daily_limit),
                    ],
                    now,
                )
                c.execute(
                    "UPDATE auth_codes SET state='superseded' WHERE channel=%s AND identifier=%s AND purpose=%s AND (purpose!='contact' OR session_hash=%s) AND state IN ('sending','sent','verifying')",
                    (payload.channel, target, payload.purpose, session_hash),
                )
                c.execute(
                    "INSERT INTO auth_codes(id,channel,identifier,purpose,session_hash,created_at,expires_at,state,code_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,'sending',%s)",
                    (
                        cid,
                        payload.channel,
                        target,
                        payload.purpose,
                        session_hash,
                        now,
                        now + 300,
                        self.csrf(cid + ":" + code) if payload.channel == "email" else None,
                    ),
                )

        await database_call(reserve)
        try:
            if payload.channel == "phone":
                await self.sms.send(target, cid)
            else:
                await self.email.send(target, code)
        except SmsUnavailable:
            await database_call(self.code_state, cid, "failed", "sending")
            raise HTTPException(
                503,
                "验证码发送未确认，请稍后重新获取",
                headers={"Retry-After": str(self.config.auth_code_interval_seconds)},
            ) from None
        if not await database_call(self.code_state, cid, "sent", "sending"):
            raise HTTPException(400, "验证码已失效，请重新获取")
        return dict(
            challenge_id=cid,
            expires_in=max(0, int(now + 300 - self.clock())),
            retry_after=max(1, math.ceil(now + self.config.auth_code_interval_seconds - self.clock())),
        )

    async def verify(self, cid, code, channel, target, purpose, token=""):
        def claim():
            with self.store.transaction() as c:
                row = c.execute(
                    "UPDATE auth_codes SET state='verifying',attempts=attempts+1 WHERE id=%s AND channel=%s AND identifier=%s AND purpose=%s AND session_hash=%s AND state='sent' AND expires_at>%s AND attempts<5 RETURNING *",
                    (cid, channel, target, purpose, digest(token) if token else "", self.clock()),
                ).fetchone()
                if not row:
                    raise HTTPException(400, "验证码不正确或已失效，请重新获取")
                return row

        row = await database_call(claim)
        try:
            valid = (
                await self.sms.verify(target, cid, code)
                if channel == "phone"
                else hmac.compare_digest(row["code_hash"] or "", self.csrf(cid + ":" + code))
            )
        except SmsUnavailable:
            await database_call(self.code_state, cid, "failed", "verifying")
            raise HTTPException(503, "验证码核验未确认，请重新获取验证码") from None
        if not valid:
            await database_call(
                self.code_state, cid, "failed" if row["attempts"] >= 5 else "sent", "verifying"
            )
            raise HTTPException(400, "验证码不正确或已失效，请检查后重试")

    def consume(self, c, cid):
        if (
            c.execute(
                "UPDATE auth_codes SET state='consumed' WHERE id=%s AND state='verifying' AND expires_at>%s",
                (cid, self.clock()),
            ).rowcount
            != 1
        ):
            raise HTTPException(400, "验证码已失效，请重新获取")

    async def login(self, payload, request):
        self.origin(request)
        target = identifier(payload)
        users = await database_call(
            self.store.query,
            "SELECT u.* FROM users u JOIN user_contacts c ON c.user_id=u.id WHERE c.channel=%s AND c.identifier=%s",
            (payload.channel, target),
        )
        user = users[0] if users else None
        if not user and not self.config.registration_enabled:
            raise HTTPException(503, "暂不支持新账号首次登录")
        await self.verify(payload.challenge_id, payload.code, payload.channel, target, "login")

        def commit():
            with self.store.transaction() as c:
                latest = c.execute(
                    "SELECT u.* FROM users u JOIN user_contacts c ON c.user_id=u.id WHERE c.channel=%s AND c.identifier=%s FOR UPDATE OF u",
                    (payload.channel, target),
                ).fetchone()
                if user and (
                    not latest or latest["id"] != user["id"] or latest["revision"] != user["revision"]
                ):
                    raise HTTPException(401, FAILURE)
                if not latest and not self.config.registration_enabled:
                    raise HTTPException(503, "暂不支持新账号首次登录")
                self.consume(c, payload.challenge_id)
                if not latest:
                    key = secrets.token_urlsafe(48)
                    name = "用户" + "".join(
                        secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(6)
                    )
                    latest = c.execute(
                        "INSERT INTO users(channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                        (
                            payload.channel,
                            target,
                            name,
                            self.clock(),
                            digest(key),
                            self.vault.encrypt(key),
                            self.config.public_url + "/wechatapi/callback/" + key,
                        ),
                    ).fetchone()
                    initialize_user(c, latest["id"])
                return self.issue(c, latest)

        try:
            return self.response(await database_call(commit))
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "联系方式状态已更改，请重新获取验证码") from None

    def resolve(self, request):
        path = request.url.path
        with self.store.connect() as c:
            if path.startswith("/wechatapi/callback/"):
                row = c.execute(
                    "SELECT id FROM users WHERE callback_key_hash=%s", (digest(path.rsplit("/", 1)[-1]),)
                ).fetchone()
                return row[0] if row else None
            try:
                return self.session_row(c, request.cookies.get(COOKIE, ""))["user_id"]
            except HTTPException:
                return None


def install(app, store, config, sms=None, clock=time.time):
    service = Authentication(store, config, sms, clock)
    app.state.authentication = service
    from .contact_bindings import install as install_contacts

    install_contacts(app, service)

    @app.middleware("http")
    async def guard(request, call_next):
        scope = None
        try:
            if request.url.path.startswith(PREFIX + "/"):
                await database_call(service.ingress, request)
            scope = current_user.set(await database_call(service.resolve, request))
            return await call_next(request)
        except HTTPException as error:
            return JSONResponse(
                {"detail": error.detail}, status_code=error.status_code, headers=error.headers
            )
        except (psycopg.OperationalError, PoolTimeout, TooManyRequests, TimeoutError):
            return JSONResponse(
                {"error": "服务暂时繁忙，请稍后重试"}, status_code=503, headers={"Retry-After": "2"}
            )
        finally:
            if scope is not None:
                current_user.reset(scope)

    @app.get(PREFIX + "/options")
    async def options():
        return dict(
            sms_available=config.sms_configured,
            email_available=config.email_configured,
            registration_enabled=config.registration_enabled,
            filing=config.filing,
        )

    @app.post(PREFIX + "/code/send")
    async def send(payload: SmsSend, request: Request):
        return await service.send(payload, request)

    @app.post(PREFIX + "/login")
    async def login(payload: LoginInput, request: Request):
        return await service.login(payload, request)

    @app.get(PREFIX + "/session")
    async def session(token=Depends(service.require)):
        def read():
            with store.connect() as c:
                row = service.session_row(c, token)
                user = c.execute("SELECT * FROM users WHERE id=%s", (row["user_id"],)).fetchone()
                return service.describe(user, service.csrf(token), service.contacts(c, user["id"]))

        return await database_call(read)

    @app.post(PREFIX + "/logout")
    async def logout(token=Depends(service.require)):
        def revoke():
            with store.transaction() as c:
                c.execute("DELETE FROM sessions WHERE id_hash=%s", (digest(token),))

        await database_call(revoke)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/wechat/")
        return response

    return service.require
