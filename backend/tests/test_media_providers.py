import asyncio
import json
import socket

import httpx
import pytest
from qingda.providers import Gateway, ProviderError
from qingda.security import Vault, create_key
from test_automation import setup as setup_account


def setup(tmp_path):
    store, manager, account, conversation = setup_account(tmp_path)
    create_key(tmp_path / "media-provider.key")
    manager.vault = Vault(tmp_path / "media-provider.key")
    store.query(
        "UPDATE users SET wechat_token=%s WHERE id=1 RETURNING id", (manager.vault.encrypt("isolated-token"),)
    )
    return store, manager, account, conversation


@pytest.mark.parametrize(
    "kind,xml,msg_id,md5,path,payload",
    [
        (
            "image",
            "<msg><img /></msg>",
            None,
            None,
            "/message/downloadImage",
            {"appId": "device-a", "xml": "<msg><img /></msg>", "type": 2},
        ),
        (
            "voice",
            "<msg><voicemsg /></msg>",
            123,
            None,
            "/message/downloadVoice",
            {"appId": "device-a", "xml": "<msg><voicemsg /></msg>", "msgId": 123},
        ),
        (
            "file",
            "<msg><appmsg /></msg>",
            None,
            None,
            "/message/downloadFile",
            {"appId": "device-a", "xml": "<msg><appmsg /></msg>"},
        ),
    ],
)
async def test_official_download_contract_uses_public_pinned_cdn_without_token(
    tmp_path, monkeypatch, kind, xml, msg_id, md5, path, payload
):
    _, manager, _, _ = setup(tmp_path)
    requests = []
    from test_reply_images import png

    expected_body = png() if kind == "image" else b"isolated media"

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/finder/v2/api" + path
            assert json.loads(request.content) == payload
            return httpx.Response(
                200,
                json={
                    "ret": 200,
                    "data": {"fileUrl": "https://cdn.example.test/resource?signature=private"},
                },
            )
        assert request.url.host == "93.184.216.34" and request.headers["Host"] == "cdn.example.test"
        assert request.extensions["sni_hostname"] == "cdn.example.test"
        assert "wechat-token" not in request.headers and "Authorization" not in request.headers
        return httpx.Response(200, content=expected_body)

    gateway = Gateway(manager, httpx.MockTransport(handler))
    assert await gateway.download_media("device-a", kind, xml, msg_id, md5) == expected_body
    assert len(requests) == 2
    await gateway.close()


async def test_download_retries_only_once_and_private_destinations_are_rejected(tmp_path):
    _, manager, _, _ = setup(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    gateway = Gateway(manager, httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="platform_http_5xx"):
        await gateway.download_media("device-a", "image", "<msg/>")
    assert len(requests) == 2
    with pytest.raises(ProviderError, match="invalid_media_url"):
        await gateway.fetch_media("http://127.0.0.1/private", 100)
    assert len(requests) == 2
    await gateway.close()


@pytest.mark.parametrize("reverse_retry_dns", [False, True])
async def test_cdn_retry_avoids_the_previous_slow_public_node(tmp_path, monkeypatch, reverse_retry_dns):
    _, manager, _, _ = setup(tmp_path)
    requests = []
    lookups = 0

    async def resolve(*args, **kwargs):
        nonlocal lookups
        lookups += 1
        addresses = ["93.184.216.34", "93.184.216.35"]
        if reverse_retry_dns and lookups > 1:
            addresses.reverse()
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80)) for address in addresses]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"ret": 200, "data": {"url": "http://cdn.example.test/media"}})
        if request.url.host == "93.184.216.34":
            raise httpx.ReadTimeout("slow public CDN node", request=request)
        assert request.url.host == "93.184.216.35"
        assert request.headers["Host"] == "cdn.example.test"
        assert "wechat-token" not in request.headers
        return httpx.Response(200, content=b"complete media")

    gateway = Gateway(manager, httpx.MockTransport(handler))
    assert await gateway.download_media("device-a", "voice", "<voicemsg/>", msg_id=123) == b"complete media"
    assert [r.url.host for r in requests if r.method == "GET"] == ["93.184.216.34", "93.184.216.35"]
    assert len(requests) == 4
    await gateway.close()


@pytest.mark.parametrize("retry_address", ["93.184.216.34", "127.0.0.1"])
async def test_cdn_retry_remains_bounded_and_revalidates_dns(tmp_path, monkeypatch, retry_address):
    _, manager, _, _ = setup(tmp_path)
    requests = []
    lookups = 0

    async def resolve(*args, **kwargs):
        nonlocal lookups
        lookups += 1
        address = "93.184.216.34" if lookups == 1 else retry_address
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"ret": 200, "data": {"url": "http://cdn.example.test/media"}})
        assert request.url.host == "93.184.216.34"
        raise httpx.ReadTimeout("slow CDN", request=request)

    gateway = Gateway(manager, httpx.MockTransport(handler))
    expected = "invalid_media_url" if retry_address == "127.0.0.1" else "media_download_timeout"
    with pytest.raises(ProviderError, match=expected):
        await gateway.download_media("device-a", "voice", "<voicemsg/>", msg_id=123)
    assert lookups == 2
    assert len([r for r in requests if r.method == "GET"]) == (1 if retry_address == "127.0.0.1" else 2)
    await gateway.close()
