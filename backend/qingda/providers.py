import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urlsplit

import httpx

from .database_async import database_call
from .emoji_policy import ANIMATED, CUSTOM


class ProviderError(Exception):
    def __init__(self, reason, uncertain=False):
        super().__init__(reason)
        self.reason, self.uncertain = reason, uncertain


class ImageDownload(bytes):
    """原始图片字节附带已验证的下载规格；不保存临时下载地址。"""

    def __new__(cls, body, image_type):
        value = super().__new__(cls, body)
        value.image_type = image_type
        return value


class Gateway:
    validates_request_capacity = True

    def __init__(self, manager, transport=None, *, tool_config=None):
        self.manager = manager
        self.base_url = os.getenv("WECHAT_API_BASE", "https://wechatapi.net/finder/v2/api").rstrip("/")
        if self.base_url != "https://wechatapi.net/finder/v2/api":
            raise ValueError("首版仅使用已核实的 WechatApi HTTPS 官方接口")
        self.client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self.reply_tools = None
        self.context_writer = None
        if tool_config is not None:
            from .reply_tools import ReplyTools

            async def model_key():
                return await database_call(self.secret, "model_key")

            async def weather_credentials():
                from .weather_settings import credentials

                return await database_call(credentials, manager)

            self.reply_tools = ReplyTools(
                tool_config,
                transport=transport,
                key_provider=model_key,
                weather_provider=weather_credentials,
            )

    def secret(self, name):
        if name == "wechat_token":
            from .tenancy import current_user

            rows = self.manager.store.query(
                "SELECT wechat_token,binding_state FROM users WHERE id=%s", (current_user.get(),)
            )
            if not rows or not rows[0]["wechat_token"]:
                raise ProviderError("missing_wechat_token")
            if rows[0]["binding_state"] != "configured":
                raise ProviderError("wechat_binding_pending")
            return self.manager.vault.decrypt(rows[0]["wechat_token"])
        encrypted = self.manager.store.setting(name)
        if not encrypted:
            raise ProviderError("missing_" + name)
        return self.manager.vault.decrypt(encrypted)

    async def call(self, path, payload, *, timeout=30, token=None):
        token = token or await database_call(self.secret, "wechat_token")
        try:
            response = await asyncio.wait_for(
                self.client.post(
                    self.base_url + path,
                    json=payload,
                    headers={"wechat-token": token},
                    timeout=httpx.Timeout(timeout, connect=min(5, timeout)),
                ),
                timeout=timeout,
            )
            if response.status_code >= 500:
                raise ProviderError("platform_http_5xx", uncertain=True)
            if response.status_code != 200:
                raise ProviderError(
                    "platform_http_" + str(response.status_code),
                    uncertain=response.status_code not in (400, 401, 403, 404, 429),
                )
            result = json.loads(response.content)
            if not isinstance(result, dict) or "ret" not in result:
                raise ProviderError("invalid_platform_response", uncertain=True)
            return result
        except (httpx.RequestError, ValueError, asyncio.TimeoutError) as e:
            raise ProviderError("platform_response_unknown", uncertain=True) from e

    async def online(self, app_id):
        result = await self.call("/login/checkOnline", {"appId": app_id})
        if result.get("ret") != 200:
            raise ProviderError("platform_check_rejected")
        if not isinstance(result.get("data"), bool):
            raise ProviderError("invalid_online_response")
        return result["data"]

    async def send(self, app_id, peer, content, ats=""):
        return await self.call(
            "/message/postText", {"appId": app_id, "toWxid": peer, "content": content, "ats": ats}
        )

    async def download_media(self, app_id, kind, xml, msg_id=None, md5=None, message_id=None):
        if kind == "emoji":
            raise ProviderError(CUSTOM)
        if kind == "video":
            raise ProviderError("video_disabled")
        if kind == "image":
            try:
                return await self.download_image(app_id, xml, message_id=message_id)
            except ProviderError as error:
                if message_id is not None and error.reason != ANIMATED:
                    await database_call(
                        self.manager.store.query,
                        "UPDATE media_resources SET state='unavailable',reason=%s WHERE message_id=%s RETURNING message_id",
                        (error.reason, message_id),
                    )
                raise
        elif kind == "voice":
            if isinstance(msg_id, bool) or not re.fullmatch(r"[0-9]{1,20}", str(msg_id)):
                raise ProviderError("invalid_voice_msg_id")
            path, payload = "/message/downloadVoice", {"appId": app_id, "xml": xml, "msgId": int(msg_id)}
        elif kind == "file":
            path, payload = "/message/download" + kind.title(), {"appId": app_id, "xml": xml}
        else:
            raise ProviderError("unsupported_media")

        attempted_ips = set()

        async def download_once():
            result = await self.call(path, payload, timeout=30)
            if result.get("ret") != 200 or not isinstance(result.get("data"), dict):
                raise ProviderError("platform_download_rejected")
            data = result["data"]
            url = data.get("fileUrl") or data.get("url") or data.get("Url")
            if not isinstance(url, str) or not url:
                raise ProviderError("missing_media_url")
            if message_id is not None:
                import time

                def _database_download_once_124():
                    with self.manager.store.connect() as conn:
                        conn.execute(
                            "UPDATE media_resources SET urls=coalesce((SELECT jsonb_agg(item) FROM jsonb_array_elements(urls) item WHERE item->>'field'<>'fileUrl'),'[]'::jsonb)||%s::jsonb,url_obtained_at=%s,url_expires_at=%s,state='available',reason='' WHERE message_id=%s",
                            (
                                json.dumps([{"field": "fileUrl", "value": url}]),
                                time.time(),
                                time.time() + 7 * 86400,
                                message_id,
                            ),
                        )
                    return ("normal", None, locals())

                _database_result = await database_call(_database_download_once_124)
            return await self.fetch_media(
                url,
                3 * 1024 * 1024
                if kind == "voice"
                else 100 * 1024 * 1024
                if kind == "file"
                else 32 * 1024 * 1024,
                attempted_ips=attempted_ips,
            )

        try:
            for attempt in range(2):
                try:
                    return await asyncio.wait_for(download_once(), 120 if kind == "file" else 30)
                except ProviderError as exc:
                    if attempt or exc.reason not in (
                        "platform_http_5xx",
                        "platform_http_429",
                        "platform_response_unknown",
                        "media_download_transient",
                    ):
                        raise
                except (httpx.RequestError, asyncio.TimeoutError) as exc:
                    if attempt:
                        raise ProviderError("media_download_timeout") from exc
                await asyncio.sleep(0.25)
        except ProviderError as error:
            if message_id is not None and error.reason != CUSTOM:
                await database_call(
                    self.manager.store.query,
                    "UPDATE media_resources SET state='unavailable',reason=%s WHERE message_id=%s RETURNING message_id",
                    (error.reason, message_id),
                )
            raise

    async def download_image(self, app_id, xml, *, message_id=None, check=None, commit_guard=None):
        from .media import image_frames
        from .protocol import safe_xml

        root = safe_xml(xml)
        node = root.find(".//img") if root is not None else None
        if node is None and root is not None and root.tag == "img":
            node = root
        specs = [2]
        if node is not None:
            if node.get("cdnbigimgurl"):
                specs.append(1)
            if node.get("cdnthumburl"):
                specs.append(3)
        transient = {
            "platform_http_5xx",
            "platform_http_429",
            "platform_response_unknown",
            "media_download_transient",
        }
        fallback = {
            "platform_download_rejected",
            "missing_media_url",
            "media_download_rejected",
            "media_resource_expired",
            "invalid_image",
            "unsupported_image_format",
        }
        remaining_retry = 1
        last = "image_download_failed"

        async def run():
            nonlocal remaining_retry, last
            for image_type in specs:
                while True:
                    if check and (reason := await database_call(check)):
                        raise ProviderError(reason)
                    try:
                        result = await self.call(
                            "/message/downloadImage",
                            {"appId": app_id, "xml": xml, "type": image_type},
                            timeout=30,
                        )
                        if result.get("ret") != 200 or not isinstance(result.get("data"), dict):
                            raise ProviderError("platform_download_rejected")
                        data = result["data"]
                        url = data.get("fileUrl") or data.get("url") or data.get("Url")
                        if not isinstance(url, str) or not url:
                            raise ProviderError("missing_media_url")
                        body = await self.fetch_media(url, 32 * 1024 * 1024, attempted_ips=set())
                        try:
                            await asyncio.to_thread(image_frames, body)
                        except (ValueError, OSError) as exc:
                            raise ProviderError("invalid_image") from exc
                        if check and (reason := await database_call(check)):
                            raise ProviderError(reason)
                        if message_id is not None:
                            import time

                            def save_url():
                                with self.manager.store.transaction() as conn:
                                    if commit_guard and (reason := commit_guard(conn)):
                                        raise ProviderError(reason)
                                    conn.execute(
                                        "UPDATE media_resources SET urls=coalesce((SELECT jsonb_agg(item) FROM jsonb_array_elements(urls) item WHERE item->>'field'<>'fileUrl'),'[]'::jsonb)||%s::jsonb,url_obtained_at=%s,url_expires_at=%s,state='available',reason='' WHERE message_id=%s",
                                        (
                                            json.dumps([{"field": "fileUrl", "value": url}]),
                                            time.time(),
                                            time.time() + 7 * 86400,
                                            message_id,
                                        ),
                                    )

                            await database_call(save_url)
                        return ImageDownload(body, image_type)
                    except (httpx.RequestError, asyncio.TimeoutError) as exc:
                        if not remaining_retry:
                            raise ProviderError("media_download_timeout") from exc
                        remaining_retry -= 1
                    except ProviderError as exc:
                        last = exc.reason
                        if last in transient and remaining_retry:
                            remaining_retry -= 1
                        elif last in fallback:
                            break
                        else:
                            raise
            raise ProviderError(last)

        try:
            return await asyncio.wait_for(run(), 60)
        except asyncio.TimeoutError as exc:
            raise ProviderError("image_download_budget_exceeded") from exc

    async def fetch_media(
        self,
        url,
        maximum,
        *,
        attempted_ips=None,
    ):
        # 只读取官方下载接口返回的公网地址；不跟随重定向、不携带 Token。
        if not isinstance(url, str) or len(url) > 8192:
            raise ProviderError("invalid_media_url")
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 80, 443)
            ):
                raise ValueError()
            addresses = await asyncio.get_running_loop().getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError()
        except (ValueError, OSError) as exc:
            raise ProviderError("invalid_media_url") from exc
        if attempted_ips is None:
            attempted_ips = set()
        address = next(
            (a[4][0] for a in addresses if (parsed.hostname, a[4][0]) not in attempted_ips),
            addresses[0][4][0],
        )
        attempted_ips.add((parsed.hostname, address))
        # 固定本次核实的公网 IP；TLS 仍按原域名验证，避免 DNS 再绑定绕过检查。
        # 重试优先选择其他已校验节点，避免重复命中同一个慢节点。
        target = httpx.URL(url).copy_with(host=address)
        async with self.client.stream(
            "GET",
            target,
            headers={"Host": parsed.netloc},
            extensions={"sni_hostname": parsed.hostname},
            timeout=httpx.Timeout(30, connect=5),
        ) as response:
            if response.status_code == 429 or response.status_code >= 500:
                raise ProviderError("media_download_transient")
            if response.status_code in (404, 410):
                raise ProviderError("media_resource_expired")
            if response.status_code != 200:
                raise ProviderError("media_download_rejected")
            length_header = response.headers.get("content-length", "")
            size = int(length_header) if length_header.isdigit() and len(length_header) <= 10 else 0
            if size > maximum:
                raise ProviderError("media_too_large_or_empty")
            chunks, length = [], 0
            async for chunk in response.aiter_bytes():
                length += len(chunk)
                if length > maximum:
                    raise ProviderError("media_too_large_or_empty")
                chunks.append(chunk)
            if not length:
                raise ProviderError("media_too_large_or_empty")
            return b"".join(chunks)

    async def model_input(self, context):
        from .reply_queue import current_start, fit_context

        def original_view(item):
            value = dict(item)
            if isinstance(value.get("original_content"), str):
                value["normalized_content"] = value["content"]
                value["content"] = value.pop("original_content")
            if isinstance(value.get("reference"), dict):
                if value["reference"].get("message_kind") in ("video", "emoji"):
                    value.pop("reference")
                else:
                    value["reference"] = original_view(value["reference"])
            for key in ("recent_images", "recent_media"):
                if key in value:
                    value[key] = [
                        original_view(item)
                        for item in value[key]
                        if item.get("message_kind") not in ("video", "emoji")
                    ]
            return value

        # 原文是主要输入，表情归一化仅提供释义，避免精确复述被替换。
        context = [
            original_view(item) for item in context if item.get("message_kind") not in ("video", "emoji")
        ]
        if not context:
            raise ProviderError("unsupported_auto_input")
        media = getattr(self.manager, "media", None)
        if any(item.get("images") for item in context) and not media:
            raise ProviderError("missing_media_service")
        context = fit_context(context, media)
        boundary = current_start(context)
        references = [(i, ref) for i, item in enumerate(context) for ref in item.get("images", [])]
        history, background, locations = [], [], {}
        for index, item in enumerate(context[:boundary]):
            target = background if item.get("context_scope") == "background" else history
            name = "group_background" if target is background else "conversation_history"
            locations[index] = f"{name}[{len(target)}]"
            target.append({k: v for k, v in item.items() if k != "images"})
        current, pending, signals = [], [], []
        for index, item in enumerate(context[boundary:], boundary):
            target = (
                signals
                if item.get("message_kind") == "pat"
                else pending
                if item.get("input_scope") == "pending"
                else current
            )
            name = (
                "interaction_signals"
                if target is signals
                else "pending_messages"
                if target is pending
                else "current_messages"
            )
            locations[index] = f"{name}[{len(target)}]"
            value = {
                k: v
                for k, v in item.items()
                if k
                not in (
                    "current",
                    "images",
                    "role",
                    "participant_profiles",
                    "profile_schema_version",
                    "_context_snapshot_id",
                )
            }
            if target is signals:
                value = {
                    k: value[k] for k in ("message_id", "sender", "timestamp", "message_kind") if k in value
                }
            target.append(value)

        def compact(value):
            if isinstance(value, list):
                return [compact(v) for v in value]
            if not isinstance(value, dict):
                return value
            return {
                k: compact(v)
                for k, v in value.items()
                if k not in ("history_selection", "_context_snapshot_id")
                and not (k in ("original_content", "normalized_content") and v == value.get("content"))
            }

        serialized = json.dumps(
            compact(
                {
                    "conversation_history": history,
                    "history_coverage": context[boundary].get("history_selection", {}),
                    "group_background": background,
                    "conversation_kind": context[boundary].get("conversation_kind", "private"),
                    "reply_target": context[boundary].get("reply_target", context[boundary].get("sender")),
                    "participant_profiles": context[boundary].get("participant_profiles", {}),
                    "profile_schema_version": context[boundary].get("profile_schema_version"),
                    "presence_suppressed": context[boundary].get("presence_suppressed", False),
                    "current_messages": current,
                    "pending_messages": pending,
                    "interaction_signals": signals,
                }
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if not references:
            return serialized
        parts = [{"type": "text", "text": serialized}]
        included = set()
        for index, reference in references:
            identity = (reference["message_id"], reference["sha256"])
            if identity in included:
                continue
            included.add(identity)
            data = await asyncio.to_thread(media.path(reference).read_bytes)
            if hashlib.sha256(data).hexdigest() != reference["sha256"]:
                raise ProviderError("media_cache_changed")
            location = locations[index]
            source_type = reference.get("source_type", "history" if index < boundary else "direct")
            if source_type == "quote":
                location += ".reference"
            elif source_type == "recent":
                location += ".recent_images"
            parts.extend(
                [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "image_location": location,
                                "message_id": reference["message_id"],
                                "source_type": source_type,
                                "sender": reference.get("sender", context[index].get("sender")),
                            },
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + base64.b64encode(data).decode(),
                            "detail": "auto",
                        },
                    },
                ]
            )
        if len(json.dumps(parts, ensure_ascii=False).encode()) >= 48 * 1024 * 1024 - 65536:
            raise ProviderError("model_image_input_too_large")
        return parts

    async def model(self, config, prompt, context):
        return await self.chat(
            config,
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": await self.model_input(context)},
            ],
        )

    async def model_with_tools(self, config, prompt, context, **runtime):
        from .tool_generation import generate

        if self.reply_tools is None and not runtime.get("decision_mode"):
            raise ProviderError("missing_reply_tools")
        return await generate(self, self.reply_tools, config, prompt, context, **runtime)

    async def validate_model_key(self, model_key):
        from .model_settings import MODEL

        try:
            response = await self.client.get(
                "https://api.deepseek.com/models",
                headers={"Authorization": "Bearer " + model_key},
                timeout=httpx.Timeout(10, connect=5),
            )
            if response.status_code != 200:
                raise ProviderError("model_http_" + str(response.status_code))
            data = response.json()
            if data.get("object") != "list" or not any(item.get("id") == MODEL for item in data["data"]):
                raise ValueError()
        except (httpx.RequestError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise ProviderError("model_key_validation_failed") from error

    async def chat(self, config, messages, tools=None):
        from .context_capture import CURRENT, Writer
        from .model_settings import effective_model
        from .reply_execution import AUDIT

        config = {
            **config,
            **{
                key: value
                for key, value in effective_model(config).items()
                if key in ("model", "provider", "base_url", "thinking_enabled", "web_search_enabled")
            },
        }
        key = await database_call(self.secret, "model_key")
        thinking = config.get("thinking_enabled", False)
        payload = {
            "model": config["model"],
            "messages": messages,
            "thinking": {"type": "enabled" if thinking else "disabled"},
            "stream": False,
            "max_tokens": config["max_tokens"],
        }
        if not thinking:
            payload["temperature"] = 0.2
        if tools is not None:
            payload.update(
                tools=tools,
                tool_choice="required"
                if not thinking
                and any(
                    t.get("function", {}).get("name") in ("finish_reply", "finish_input_control")
                    for t in tools
                )
                else "auto",
            )
        from .context_budget import INPUT_TOKENS, TEXT_BYTES, request_size

        size = await asyncio.to_thread(request_size, messages, tools or [])
        if size["tokens"] > INPUT_TOKENS or size["text_bytes"] > TEXT_BYTES:
            raise ProviderError("model_context_capacity")
        if len(json.dumps(payload, ensure_ascii=False).encode()) >= 48 * 1024 * 1024:
            raise ProviderError("model_input_too_large")
        started, observed, status = time.monotonic(), {}, None
        capture, capture_id = CURRENT.get(), None
        if capture is not None:
            if self.context_writer is None:
                self.context_writer = Writer(self.manager.store)
            capture_id = capture.request(payload, size, self.context_writer)
        try:
            response = await self.client.post(
                config["base_url"].rstrip("/") + "/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer " + key},
                timeout=httpx.Timeout(config["timeout"], connect=5),
            )
            status = response.status_code
            try:
                candidate = response.json()
                if isinstance(candidate, dict):
                    observed = candidate
            except ValueError:
                pass
            if response.status_code != 200:
                raise ProviderError("model_http_" + str(response.status_code))
            result = json.loads(response.content)
            if not isinstance(result, dict):
                raise ValueError()
            observed = result
            return result
        except (httpx.RequestError, ValueError) as e:
            raise ProviderError("model_request_failed") from e
        finally:
            if capture is not None and capture_id:
                capture.response(capture_id, observed, status, int((time.monotonic() - started) * 1000))
            if audit := AUDIT.get():
                await audit(
                    {
                        "model": observed.get("model") or config["model"],
                        "requested_model": config["model"],
                        "request_id": observed.get("id"),
                        "status": status,
                        "usage": observed.get("usage") or {},
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "inference_options": {
                            "thinking": "enabled" if thinking else "disabled",
                            "web_search_enabled": config.get("web_search_enabled", True),
                        },
                        "price_basis": "public_peak_upper_estimate",
                    }
                )

    async def close(self):
        if self.context_writer is not None:
            try:
                await asyncio.wait_for(self.context_writer.drain(), 2)
            except asyncio.TimeoutError:
                pass
        if self.reply_tools is not None:
            await self.reply_tools.close()
        await self.client.aclose()
