"""账户资料及按用户保存的微信接入；外部绑定不占数据库事务。"""

import asyncio
import hmac
import io
import time

import psycopg
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import Field

from .auth import Input
from .database_async import database_call
from .providers import ProviderError
from .tenancy import current_user


class Profile(Input):
    username: str = Field(min_length=1, max_length=32)


class Wechat(Input):
    token: str = Field(default="", max_length=1000)
    revision: int = Field(ge=0)


def avatar_image(raw):
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP", "GIF") or image.width * image.height > 16_000_000:
                raise ValueError()
            image.seek(0)
            image = ImageOps.exif_transpose(image).convert("RGB")
            size = min(1024, image.width, image.height)
            image = ImageOps.fit(image, (size, size), method=Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, "JPEG", quality=95, subsampling=0)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(422, "请选择有效的头像图片") from None


def wechat_settings(store, uid, vault):
    rows = store.query("SELECT * FROM users WHERE id=%s", (uid,))
    if not rows:
        raise HTTPException(401, "请登录管理台")
    user = rows[0]
    state = user["binding_state"]
    if state == "binding" and (user["binding_started"] or 0) < time.time() - 60:
        with store.transaction() as c:
            c.execute(
                "UPDATE users SET binding_state='uncertain' WHERE id=%s AND binding_state='binding' AND binding_revision=%s",
                (uid, user["binding_revision"]),
            )
        state = "uncertain"
    encrypted_token = (
        user["candidate_token"]
        if state in ("binding", "uncertain") and user["candidate_token"]
        else user["wechat_token"]
    )
    return dict(
        token=vault.decrypt(encrypted_token) if encrypted_token else "",
        token_configured=bool(user["wechat_token"]),
        callback_url=user["callback_url"],
        state=state,
        revision=user["binding_revision"],
    )


async def bind_wechat(manager, config, gateway, payload):
    store, vault = manager.store, manager.vault
    uid = current_user.get()

    def prepare():
        with store.transaction() as c:
            user = c.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (uid,)).fetchone()
            expected = config.public_url + "/wechatapi/callback/" + vault.decrypt(user["callback_key"])
            if user["binding_revision"] != payload.revision or (
                user["binding_state"] == "binding" and user["binding_started"] > time.time() - 60
            ):
                raise HTTPException(409, "配置已变化或正在绑定，请刷新后重试")
            if c.execute(
                "SELECT 1 FROM tasks WHERE state IN ('generating','preparing','ready','sending') LIMIT 1"
            ).fetchone():
                raise HTTPException(409, "当前仍有任务运行，请等待完成后修改微信配置")
            token = payload.token.strip() or (
                vault.decrypt(user["candidate_token"])
                if user["binding_state"] == "uncertain" and user["candidate_token"]
                else vault.decrypt(user["wechat_token"])
                if user["wechat_token"]
                else ""
            )
            if not token:
                raise HTTPException(422, "请输入微信平台Token")
            fingerprint = hmac.new(config.key_file.read_bytes(), token.encode(), "sha256").hexdigest()
            if c.execute(
                "SELECT 1 FROM users WHERE id!=%s AND (token_fingerprint=%s OR candidate_fingerprint=%s)",
                (uid, fingerprint, fingerprint),
            ).fetchone():
                raise HTTPException(409, "该Token已绑定其他用户")
            revision = payload.revision + 1
            c.execute(
                "UPDATE users SET binding_state='binding',binding_revision=%s,binding_started=%s,candidate_token=%s,candidate_fingerprint=%s WHERE id=%s",
                (revision, time.time(), vault.encrypt(token), fingerprint, uid),
            )
            return token, revision, user["wechat_token"], expected

    try:
        token, revision, previous, callback_url = await database_call(prepare)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "该Token已绑定其他用户") from None
    state = "configured"
    try:
        result = await gateway.call(
            "/login/setCallback", {"token": token, "callbackUrl": callback_url}, token=token
        )
        if result.get("ret") != 200:
            state = "failed"
    except ProviderError as error:
        state = "uncertain" if error.uncertain else "failed"
    except asyncio.CancelledError:
        # 中断后保持 binding，60秒后读回转为不确定；禁止把新凭据用于发信。
        raise

    def finish():
        with store.transaction() as c:
            user = c.execute(
                "SELECT * FROM users WHERE id=%s AND binding_revision=%s FOR UPDATE", (uid, revision)
            ).fetchone()
            if not user:
                raise HTTPException(409, "配置已变化，请刷新后查看")
            if state == "configured":
                changed = not previous or vault.decrypt(previous) != token
                c.execute(
                    "UPDATE users SET wechat_token=candidate_token,token_fingerprint=candidate_fingerprint,callback_url=%s,binding_state='configured',candidate_token=NULL,candidate_fingerprint=NULL WHERE id=%s",
                    (callback_url, uid),
                )
                if changed:
                    c.execute("UPDATE accounts SET online=NULL,online_checked_at=NULL")
                    c.execute("UPDATE terminals SET verified=0")
                    manager.cancel_auto(c, "wechat_credentials_changed")
                    c.execute(
                        "UPDATE account_discovery SET state='pending',next_at=%s,attempts=0",
                        (time.time(),),
                    )
            else:
                if state == "failed":
                    c.execute(
                        "UPDATE users SET candidate_token=NULL,candidate_fingerprint=NULL WHERE id=%s",
                        (uid,),
                    )
                c.execute(
                    "UPDATE users SET binding_state=%s WHERE id=%s",
                    ("configured" if state == "failed" and previous else state, uid),
                )
        return wechat_settings(store, uid, vault)

    answer = await database_call(finish)
    if state != "configured":
        raise HTTPException(
            502,
            "回调绑定结果待确认，请核对后重试" if state == "uncertain" else "官方回调绑定失败，请检查Token",
        )
    return answer


def install(app, manager, config, gateway, auth):
    store = manager.store
    vault = manager.vault
    base = "/wechat/api/v1"

    def get_user():
        rows = store.query("SELECT * FROM users WHERE id=%s", (current_user.get(),))
        if not rows:
            raise HTTPException(401, "请登录管理台")
        return rows[0]

    def settings():
        return wechat_settings(store, current_user.get(), vault)

    @app.patch(base + "/profile", dependencies=[Depends(auth)])
    async def profile(payload: Profile):
        name = payload.username.strip()
        if not name:
            raise HTTPException(422, "请输入用户名")

        def save():
            with store.transaction() as c:
                c.execute("UPDATE users SET username=%s WHERE id=%s", (name, current_user.get()))
            return {"username": name}

        return await database_call(save)

    @app.put(base + "/profile/avatar", dependencies=[Depends(auth)])
    async def upload(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 5 * 1024 * 1024:
                raise HTTPException(413, "头像不能超过5MiB")
        image = await asyncio.to_thread(avatar_image, bytes(raw))

        def save():
            with store.transaction() as c:
                row = c.execute(
                    "UPDATE users SET avatar=%s,avatar_revision=avatar_revision+1 WHERE id=%s RETURNING avatar_revision",
                    (image, current_user.get()),
                ).fetchone()
                return {"avatar_url": f"{base}/profile/avatar?v={row[0]}"}

        return await database_call(save)

    @app.get(base + "/profile/avatar", dependencies=[Depends(auth)])
    async def avatar():
        user = await database_call(get_user)
        if not user["avatar"]:
            raise HTTPException(404, "头像不存在")
        return Response(bytes(user["avatar"]), media_type="image/jpeg")

    @app.get(base + "/wechat-config", dependencies=[Depends(auth)])
    async def read(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await database_call(settings)

    @app.put(base + "/wechat-config", dependencies=[Depends(auth)])
    async def bind(payload: Wechat, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await bind_wechat(manager, config, gateway, payload)
