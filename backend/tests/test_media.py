"""仅服务器隔离环境运行；模型替身验证契约，不冒充真实模型质量。"""

import io
import os

import pytest
from business_fixtures import allow_reply
from PIL import Image
from qingda.config import Config
from qingda.media import IncomingMedia
from qingda.media_decode import decode
from qingda.providers import ProviderError
from qingda.security import create_key
from qingda.worker import Worker
from test_automation import NOW, deliver, setup
from test_worker import Gateway


def config_at(tmp_path):
    create_key(tmp_path / "key")
    return Config(tmp_path, tmp_path / "key")


@pytest.mark.parametrize("mode,pixel", [("RGB", (12, 65, 199)), ("RGBA", (12, 65, 199, 27))])
def test_frame_cache_preserves_exact_pixels_and_transparency(tmp_path, mode, pixel):
    from qingda.media_decode import save_frame

    source = Image.new(mode, (273, 149), pixel)
    source.putpixel((42, 56), tuple(255 - v for v in pixel))
    frame = save_frame(source, tmp_path, 1, 0)
    with Image.open(tmp_path / f"1-{frame['sha256']}.png") as restored:
        assert restored.mode == mode
        assert restored.size == source.size
        assert restored.tobytes() == source.tobytes()


def animation(animated=True):
    output = io.BytesIO()
    frames = [Image.new("RGB", (64, 64), color) for color in ("red", "green", "blue")]
    frames[0].save(
        output,
        "GIF",
        save_all=animated,
        **({"append_images": frames[1:], "duration": [100, 300, 600], "loop": 0} if animated else {}),
    )
    return output.getvalue()


class MediaGateway(Gateway):
    def __init__(self, body):
        super().__init__()
        self.body, self.contexts, self.downloads = body, [], 0

    async def download_media(self, *args, **kwargs):
        self.downloads += 1
        return self.body

    async def model(self, config, prompt, context):
        self.contexts.append(context)
        return await super().model(config, prompt, context)


async def test_animated_image_decode_is_rejected_without_frame_output(tmp_path):
    config = config_at(tmp_path)
    path = tmp_path / "raw.bin"
    path.write_bytes(animation())
    with pytest.raises(ProviderError, match="animated_emoji_disabled"):
        await decode(path, tmp_path, 1, "image", config)
    assert not list(tmp_path.glob("1-*.png"))


def test_original_frame_resolution_is_preserved_or_explicitly_rejected(tmp_path):
    from qingda.media_decode import save_frame

    frame = save_frame(Image.new("RGB", (1024, 768), "red"), tmp_path, 1, 0)
    assert (frame["width"], frame["height"]) == (1024, 768)
    noisy = Image.frombytes("RGBA", (1536, 1536), os.urandom(1536 * 1536 * 4))
    with pytest.raises(ValueError, match="media_frame_too_large"):
        save_frame(noisy, tmp_path, 2, 0)
    with pytest.raises(ValueError, match="media_frame_resolution_unsupported"):
        save_frame(Image.new("RGB", (4001, 4000)), tmp_path, 3, 0)


async def test_unwoken_group_does_not_download_or_call_models(tmp_path):
    store, manager, account, _ = setup(tmp_path)
    manager.media = IncomingMedia(manager, config_at(tmp_path))
    allow_reply(manager, manager.ensure_conversation(account["id"], "room@chatroom")["id"], True)
    deliver(
        store,
        manager,
        1,
        peer="room@chatroom",
        MsgType=47,
        Content={"string": 'member:\n<msg><emoji md5="' + "a" * 32 + '" /></msg>'},
    )
    gateway = MediaGateway(animation())
    worker = Worker(manager, gateway, clock=lambda: NOW + 35)
    try:
        await worker.once()
        assert not gateway.downloads and not gateway.sends
    finally:
        worker.release()


def test_existing_media_results_preserved_by_current_migration_check(tmp_path):
    from qingda.migration import migrate_database
    from qingda.store import SCHEMA_VERSION

    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    task = store.query("SELECT id FROM tasks")[0]["id"]
    store.query(
        "INSERT INTO media_understandings(task_id,source_message_id,input_revision,fingerprint,state,owner,model,source_sha256,question_sha256,prompt_sha256,updated_at) VALUES(%s,1,1,'f','ready','o','qwen3.8-max','s','q','p',%s) RETURNING task_id",
        (task, NOW),
    )
    before = store.query("SELECT * FROM media_understandings")
    # 密码认证已退役，不能仅删除迁移记录把当前结构伪装成schema44。
    migrate_database(store.url)
    assert store.query("SELECT * FROM media_understandings") == before
    assert store.query("SELECT max(version) AS version FROM schema_versions")[0]["version"] == SCHEMA_VERSION
