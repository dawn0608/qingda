import time
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg_pool import PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.gzip import GZipMiddleware

from .accounts import AccountService
from .config import Config
from .database_async import database_call
from .model_settings import EDITABLE_FIELDS, effective_model, masked_key, validate_model
from .providers import Gateway, ProviderError
from .security import Vault
from .service import Manager
from .store import Store
from .tenancy import current_user

PREFIX = "/wechat/api/v1"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClearHistoryInput(Input):
    request_id: UUID
    confirmed: Literal[True]


class WeatherInput(Input):
    api_host: str = Field(max_length=253)
    api_key: str | None = Field(default=None, max_length=1000)


class SettingsInput(Input):
    model_key: str | None = Field(default=None, max_length=1000)
    model: dict | None = None
    asr_key: str | None = Field(default=None, max_length=1000)
    weather: WeatherInput | None = None


class Automation(Input):
    prompt: str | None = Field(default=None, max_length=50000)
    ai_enabled: bool | None = None


class AutoReplyInput(Input):
    enabled: bool


class PromptDraftInput(Input):
    markdown: str = Field(max_length=50000)
    version: str | None = Field(default=None, max_length=64)
    base_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PromptApplyInput(Input):
    version: str = Field(min_length=1, max_length=64)
    base_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def create_app(config=None, gateway_factory=None, sms_provider=None, auth_clock=time.time):
    config = config or Config.from_env()
    store = Store(config.database_url)
    vault = Vault(config.key_file)
    manager = Manager(store, vault)
    gateway = gateway_factory(manager) if gateway_factory else Gateway(manager, tool_config=config)
    account_service = AccountService(manager, gateway)

    @asynccontextmanager
    async def lifespan(app):
        await database_call(store.register_runtime, "web")
        yield
        await gateway.close()
        store.close()

    app = FastAPI(title="青答 Qingda", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager, app.state.gateway, app.state.config = manager, gateway, config

    @app.exception_handler(RequestValidationError)
    async def input_error(request, exc):
        return JSONResponse({"error": "输入格式不正确，请检查必填项"}, status_code=422)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    from .prompt_drafts import DraftConflict

    @app.exception_handler(DraftConflict)
    async def draft_conflict(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(PoolTimeout)
    @app.exception_handler(TooManyRequests)
    @app.exception_handler(psycopg.OperationalError)
    async def database_busy(request, exc):
        return JSONResponse(
            {"error": "服务暂时繁忙，请稍后重试"}, status_code=503, headers={"Retry-After": "2"}
        )

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse(
            {"error": "平台请求失败，请检查配置或稍后重试", "reason": exc.reason, "uncertain": exc.uncertain},
            status_code=502,
        )

    @app.middleware("http")
    async def response_headers(request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        if response.status_code == 200 and getattr(request.state, "event_id", None):
            try:

                def _database_response_headers_147():
                    with store.connect() as conn:
                        conn.execute(
                            "INSERT INTO callback_receipts(event_id,at,processing_ms) VALUES(%s,%s,%s)",
                            (request.state.event_id, time.time(), int((time.monotonic() - started) * 1000)),
                        )
                    return ("normal", None, locals())

                _database_result = await database_call(_database_response_headers_147)
            except (psycopg.Error, PoolTimeout, TooManyRequests, TimeoutError, OSError):
                response = Response(status_code=503)
            response.headers["Server-Timing"] = f"inbox;dur={(time.monotonic() - started) * 1000:.2f}"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/wechat/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob: https:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            )
        return response

    from .auth import install as install_auth

    auth = install_auth(app, store, config, sms_provider, auth_clock)
    from .profile import install as install_profile

    install_profile(app, manager, config, gateway, auth)
    from .media import IncomingMedia

    manager.media = IncomingMedia(manager, config)

    @app.get("/internal/health")
    async def internal_health():
        try:
            result = await database_call(store.health)
            return JSONResponse(result, status_code=200 if result["database_ready"] else 503)
        except Exception:
            return JSONResponse({"database_backend": "postgresql", "database_ready": False}, status_code=503)

    @app.api_route("/wechatapi/callback/{key}", methods=["GET", "HEAD", "POST"])
    async def callback(key: str, request: Request):
        if current_user.get() is None:
            return Response(status_code=404)
        if request.method != "POST":
            return Response(status_code=200, headers={"X-Callback-Receiver": "qingda"})
        if request.headers.get("transfer-encoding"):
            return Response(status_code=400)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16 * 1024 * 1024:
                return Response(status_code=413)
        if raw:
            try:
                request.state.event_id = await database_call(store.receive, bytes(raw))
            except (psycopg.Error, PoolTimeout, TooManyRequests, TimeoutError, OSError):
                return Response(status_code=503)
        return Response(status_code=200, headers={"X-Callback-Receiver": "qingda"})

    @app.get(PREFIX + "/settings", dependencies=[Depends(auth)])
    async def settings():
        from .speech import MODEL as ASR_MODEL
        from .weather_settings import public_settings

        model_key = await database_call(store.setting, "model_key")
        asr_key = await database_call(store.setting, "asr_key")
        return {
            "weather": await database_call(public_settings, manager),
            "asr": {
                "provider": "aliyun",
                "model": ASR_MODEL,
                "key_configured": bool(asr_key),
                "key_preview": masked_key(vault.decrypt(asr_key)) if asr_key else "",
            },
            "model": effective_model(await database_call(store.setting, "model", {})),
            "wechat_token_configured": bool(
                (
                    await database_call(
                        store.query, "SELECT wechat_token FROM users WHERE id=%s", (current_user.get(),)
                    )
                )[0]["wechat_token"]
            ),
            "model_key_configured": bool(model_key),
            "model_key_preview": masked_key(vault.decrypt(model_key)) if model_key else "",
            "model_revision": await database_call(store.setting, "model_revision", 0),
        }

    @app.patch(PREFIX + "/settings", dependencies=[Depends(auth)])
    async def update_settings(payload: SettingsInput):
        from .speech import validate_key

        asr_key = validate_key(payload.asr_key or "")
        model_key = (payload.model_key or "").strip()
        if payload.model is not None or model_key:
            if payload.model and set(payload.model) - EDITABLE_FIELDS:
                raise ValueError("模型配置包含未知字段")
            validate_model(payload.model or {})
            if model_key:
                try:
                    await gateway.validate_model_key(model_key)
                except ProviderError as error:
                    message = (
                        "DeepSeek API 密钥无效或没有权限"
                        if error.reason in ("model_http_401", "model_http_403")
                        else "密钥校验失败，请稍后重试"
                    )
                    raise HTTPException(400, message) from error
            await database_call(manager.configure_model, payload.model or {}, model_key)
        if asr_key:
            await database_call(manager.configure_asr, asr_key)
        if payload.weather is not None:
            from .weather_settings import save

            await database_call(save, manager, payload.weather.api_host, payload.weather.api_key)
        return await settings()

    @app.get(PREFIX + "/accounts", dependencies=[Depends(auth)])
    async def accounts(view: Literal["active", "archived"] = "active"):
        return await database_call(manager.accounts, view)

    @app.post(PREFIX + "/accounts/{aid}/online", dependencies=[Depends(auth)])
    async def check_online(aid: int):
        return await account_service.online(aid)

    @app.patch(PREFIX + "/accounts/{aid}/automation", dependencies=[Depends(auth)])
    async def automation(aid: int, payload: Automation):
        if payload.prompt is None and payload.ai_enabled is None:
            raise ValueError("请提供提示词或自动回复开关")
        return await database_call(manager.configure_automation, aid, payload.prompt, payload.ai_enabled)

    @app.get(PREFIX + "/accounts/{aid}/prompt-draft", dependencies=[Depends(auth)])
    async def prompt_draft(aid: int):
        from .prompt_drafts import read

        return await database_call(read, manager, aid)

    @app.put(PREFIX + "/accounts/{aid}/prompt-draft", dependencies=[Depends(auth)])
    async def save_prompt_draft(aid: int, payload: PromptDraftInput):
        from .prompt_drafts import save

        return await database_call(save, manager, aid, payload.markdown, payload.version, payload.base_sha256)

    @app.post(PREFIX + "/accounts/{aid}/prompt-draft/apply", dependencies=[Depends(auth)])
    async def apply_prompt_draft(aid: int, payload: PromptApplyInput):
        from .prompt_drafts import apply

        return await database_call(apply, manager, aid, payload.version, payload.base_sha256)

    @app.post(PREFIX + "/accounts/{aid}/prompt-draft/resolve", dependencies=[Depends(auth)])
    async def resolve_prompt_draft(aid: int, payload: PromptDraftInput):
        from .prompt_drafts import resolve

        return await database_call(
            resolve, manager, aid, payload.markdown, payload.version, payload.base_sha256
        )

    @app.get(PREFIX + "/accounts/{aid}/owner-profile-draft", dependencies=[Depends(auth)])
    async def owner_profile_draft(aid: int, owner_wxid: str = Query(min_length=1, max_length=200)):
        from .prompt_drafts import read

        return await database_call(read, manager, aid, owner_wxid=owner_wxid)

    @app.put(PREFIX + "/accounts/{aid}/owner-profile-draft", dependencies=[Depends(auth)])
    async def save_owner_profile_draft(
        aid: int, payload: PromptDraftInput, owner_wxid: str = Query(min_length=1, max_length=200)
    ):
        from .prompt_drafts import save

        return await database_call(
            save,
            manager,
            aid,
            payload.markdown,
            payload.version,
            payload.base_sha256,
            owner_wxid=owner_wxid,
        )

    @app.post(PREFIX + "/accounts/{aid}/owner-profile-draft/apply", dependencies=[Depends(auth)])
    async def apply_owner_profile_draft(
        aid: int, payload: PromptApplyInput, owner_wxid: str = Query(min_length=1, max_length=200)
    ):
        from .prompt_drafts import apply

        return await database_call(
            apply, manager, aid, payload.version, payload.base_sha256, owner_wxid=owner_wxid
        )

    @app.post(PREFIX + "/accounts/{aid}/owner-profile-draft/resolve", dependencies=[Depends(auth)])
    async def resolve_owner_profile_draft(
        aid: int, payload: PromptDraftInput, owner_wxid: str = Query(min_length=1, max_length=200)
    ):
        from .prompt_drafts import resolve

        return await database_call(
            resolve,
            manager,
            aid,
            payload.markdown,
            payload.version,
            payload.base_sha256,
            owner_wxid=owner_wxid,
        )

    @app.get(PREFIX + "/conversations", dependencies=[Depends(auth)])
    async def conversations(account_id: int | None = None, kind: str | None = None):
        from .conversation_options import conversations

        return conversations(manager, account_id, kind)

    @app.get(PREFIX + "/accounts/{aid}/reply-scope", dependencies=[Depends(auth)])
    async def reply_scope(
        aid: int,
        kind: Literal["private", "group"] = "private",
        q: str = Query(default="", max_length=2000),
        status: Literal["all", "enabled", "disabled", "paused", "removed"] = "all",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=10, ge=1, le=50),
    ):
        from .conversation_options import reply_scope

        if page_size not in (10, 20, 50):
            raise HTTPException(422, "输入格式不正确，请检查必填项")
        return await database_call(reply_scope, manager, aid, kind, q, status, page, page_size)

    @app.patch(PREFIX + "/conversations/{cid}/auto-reply", dependencies=[Depends(auth)])
    async def auto_reply(cid: int, payload: AutoReplyInput):
        return await database_call(manager.set_auto_reply, cid, payload.enabled)

    @app.get(PREFIX + "/accounts/{aid}/conversations/{cid}/history", dependencies=[Depends(auth)])
    async def chat_history(
        aid: int,
        cid: int,
        q: str = Query(default="", max_length=2000),
        cursor: str | None = Query(default=None, max_length=16000),
        after_id: int | None = Query(default=None, ge=0, le=2**63 - 1),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        from .chat_records import history

        return await database_call(history, manager, aid, cid, q, cursor, after_id, limit)

    @app.delete(PREFIX + "/accounts/{aid}/conversations/{cid}/history", dependencies=[Depends(auth)])
    async def clear_chat_history(aid: int, cid: int, payload: ClearHistoryInput):
        from .history_deletion import advance, start, status

        try:
            operation = await database_call(start, manager, aid, cid, str(payload.request_id))
        except ValueError as error:
            raise HTTPException(404 if str(error) == "会话不存在" else 409, str(error)) from None
        await database_call(advance, manager, operation["id"])
        return await database_call(status, manager, aid, cid, operation["id"])

    @app.get(PREFIX + "/accounts/{aid}/conversations/{cid}/history/deletion", dependencies=[Depends(auth)])
    async def chat_history_deletion(aid: int, cid: int, request_id: UUID | None = None):
        from .history_deletion import advance, status

        try:
            operation = await database_call(
                status, manager, aid, cid, str(request_id) if request_id else None
            )
        except ValueError:
            raise HTTPException(404, "会话不存在") from None
        if operation and operation["state"] != "complete":
            await database_call(advance, manager, operation["id"])
            operation = await database_call(status, manager, aid, cid, operation["id"])
        return {"operation": operation}

    @app.get(PREFIX + "/accounts/{aid}/conversations/{cid}/history/{mid}/image", dependencies=[Depends(auth)])
    async def chat_image(aid: int, cid: int, mid: int):
        from .chat_records import image_content

        body = await database_call(image_content, manager, aid, cid, mid)
        if body is None:
            raise HTTPException(404, "图片暂不可用")
        return Response(body, media_type="image/png")

    @app.get(PREFIX + "/accounts/{aid}/conversations/{cid}/history/{mid}/voice", dependencies=[Depends(auth)])
    async def chat_voice(aid: int, cid: int, mid: int, request: Request):
        from .history_voice import voice_content

        try:
            body = await voice_content(manager, config, aid, cid, mid, request)
        except (ProviderError, OSError, ValueError, TimeoutError) as error:
            if isinstance(error, ProviderError) and str(error) == "voice_playback_busy":
                raise HTTPException(503, "语音播放繁忙，请稍后重试") from None
            body = None
        if body is None:
            raise HTTPException(404, "语音暂不可用")
        return Response(body, media_type="audio/wav")

    if config.frontend_dir.exists():
        assets = config.frontend_dir / "assets"
        if assets.exists():
            app.mount(
                "/wechat/assets",
                GZipMiddleware(StaticFiles(directory=assets), minimum_size=1024, compresslevel=6),
                name="assets",
            )

    @app.api_route(
        "/wechat/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    async def frontend(path: str, request: Request):
        if path.strip("/") or request.method not in ("GET", "HEAD"):
            raise HTTPException(404, "接口不存在")
        index = config.frontend_dir / "index.html"
        if not index.exists():
            return JSONResponse({"error": "前端尚未构建"}, status_code=503)
        return FileResponse(index)

    return app
