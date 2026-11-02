"""服务器真实模型验证：只读逐用户凭据、公开图片，不发送微信。"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from qingda.config import Config
from qingda.model_settings import effective_model
from qingda.providers import Gateway
from qingda.reply_execution import AUDIT
from qingda.security import Vault
from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import user_scope


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user-id", type=int, default=1)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    config = Config.from_env()
    store = Store(config.database_url)
    manager = Manager(store, Vault(config.key_file))
    image = args.output / "public-red-square.png"
    Image.new("RGB", (96, 96), "red").save(image)
    manager.media = SimpleNamespace(path=lambda reference: image)
    gateway = Gateway(manager, tool_config=config)
    requests, results = [], []

    async def audit(value):
        requests.append(value)

    token = AUDIT.set(audit)
    try:
        with user_scope(args.user_id):
            options = effective_model(store.setting("model", {}))
            for thinking in (False,):
                context = [
                    {
                        "role": "user",
                        "current": True,
                        "message_id": 1,
                        "sender": "public-check",
                        "message_kind": "image",
                        "content": "图片是纯色方块。仅输出其颜色的中文名称。",
                        "images": [
                            {
                                "message_id": 1,
                                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                                "width": 96,
                                "height": 96,
                            }
                        ],
                    }
                ]
                result = await gateway.model_with_tools(
                    {**options, "thinking_enabled": thinking, "web_search_enabled": True},
                    "根据图片回答用户问题，单独调用finish_reply提交答案。",
                    context,
                    decision_mode=True,
                )
                answer = result["reply_decision"]["content"]
                assert answer.strip("。.!！ \n") in ("红色", "红"), answer
                assert not any(item["name"] == "inspect_media" for item in result["tool_trace"]["tool_calls"])
                results.append(
                    {"thinking": thinking, "answer": answer, "passed": True, "usage": result["usage"]}
                )
        proof = {
            "case_version": "native-flash-20261019-v1",
            "grading_version": "exact-color-v1",
            "code_revision": os.getenv("APP_REVISION", "development"),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "model": "deepseek-flash",
            "real_model": True,
            "sends_wechat": False,
            "results": results,
            "requests": requests,
        }
        (args.output / "live-model.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2))
        print(json.dumps(proof, ensure_ascii=True))
    finally:
        AUDIT.reset(token)
        await gateway.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
