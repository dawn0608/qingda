"""服务器隔离验证：自定义表情无网络等待、图片动画只做准入判断。"""

import httpx
import pytest
from qingda.providers import Gateway, ProviderError
from test_automation import setup
from test_emoji_policy import image_bytes


@pytest.mark.parametrize("digest", [None, "a" * 32])
async def test_custom_emoji_rejected_before_credentials_dns_or_http(tmp_path, digest):
    _, manager, _, _ = setup(tmp_path)

    def forbidden(request):
        raise AssertionError("自定义表情不得访问网络")

    gateway = Gateway(manager, httpx.MockTransport(forbidden))
    try:
        with pytest.raises(ProviderError, match="custom_emoji_disabled"):
            await gateway.download_media("device-a", "emoji", "", md5=digest)
    finally:
        await gateway.close()


@pytest.mark.parametrize("fmt", ["GIF", "PNG", "WEBP"])
async def test_animated_image_does_not_fallback_to_static_thumbnail(tmp_path, fmt):
    _, manager, _, _ = setup(tmp_path)
    gateway = Gateway(manager)
    calls = []

    async def call(path, payload, **kwargs):
        calls.append(payload["type"])
        return {"ret": 200, "data": {"fileUrl": "https://cdn.example.test/image"}}

    async def fetch(*args, **kwargs):
        return image_bytes(fmt)

    gateway.call, gateway.fetch_media = call, fetch
    try:
        with pytest.raises(ProviderError, match="animated_emoji_disabled"):
            await gateway.download_image(
                "device-a", '<msg><img cdnbigimgurl="big" cdnthumburl="thumb"/></msg>'
            )
        assert calls == [2]
    finally:
        await gateway.close()
