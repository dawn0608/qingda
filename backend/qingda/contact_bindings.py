"""已登录用户直接验证新联系方式；验证码绑定用途、目的地和当前会话。"""

from typing import Literal

import psycopg
from fastapi import Depends, HTTPException, Request
from pydantic import Field

from .auth import PREFIX, Input, SmsSend, identifier
from .database_async import database_call
from .security import digest
from .tenancy import current_user


class ContactCode(Input):
    channel: Literal["phone", "email"]
    identifier: str = Field(min_length=1, max_length=254)


class ContactChange(ContactCode):
    challenge_id: str = Field(min_length=32, max_length=64)
    code: str = Field(pattern=r"^[0-9]{6}$")


def install(app, service):
    @app.post(PREFIX + "/contacts/code/send")
    async def send(payload: ContactCode, request: Request, token=Depends(service.require)):
        return await service.send(
            SmsSend(channel=payload.channel, identifier=payload.identifier, purpose="contact"),
            request,
            contact_change=True,
        )

    @app.put(PREFIX + "/contacts")
    async def change(payload: ContactChange, token=Depends(service.require)):
        target = identifier(payload)
        user_id = current_user.get()
        await service.verify(payload.challenge_id, payload.code, payload.channel, target, "contact", token)

        def commit():
            with service.store.transaction() as c:
                user = c.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
                # 外部核验结束后再检查会话，防止退出、改密或并发换绑后的旧请求提交。
                session = service.session_row(c, token)
                if session["user_id"] != user_id:
                    raise HTTPException(401, "请重新登录")
                old = service.contacts(c, user_id)
                if old.get(payload.channel) == target:
                    raise HTTPException(400, "联系方式未更改")
                service.consume(c, payload.challenge_id)
                c.execute(
                    "INSERT INTO user_contacts(user_id,channel,identifier) VALUES(%s,%s,%s) ON CONFLICT(user_id,channel) DO UPDATE SET identifier=excluded.identifier",
                    (user_id, payload.channel, target),
                )
                user = c.execute(
                    "UPDATE users SET identifier=CASE WHEN channel=%s THEN %s ELSE identifier END,revision=revision+1 WHERE id=%s RETURNING *",
                    (payload.channel, target, user_id),
                ).fetchone()
                # 新旧目的地挑战及其他终端会话全部失效；只为本次操作重新签发会话。
                c.execute(
                    "UPDATE auth_codes SET state='superseded' WHERE (identifier=ANY(%s) OR session_hash=%s) AND state IN ('sending','sent','verifying')",
                    (list(old.values()) + [target], digest(token)),
                )
                c.execute(
                    "DELETE FROM sessions WHERE id_hash IN (SELECT id_hash FROM auth_sessions WHERE user_id=%s)",
                    (user_id,),
                )
                return service.issue(c, user)

        try:
            return service.response(await database_call(commit))
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "该邮箱或手机号已绑定，请使用其他联系方式") from None
