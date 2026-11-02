"""服务器真实模型验收；只读生产密钥，只发送公开算术，不发送微信。"""

import asyncio
import json
from types import SimpleNamespace

from qingda.config import Config
from qingda.model_settings import DEFAULT_MODEL
from qingda.providers import Gateway, ProviderError
from qingda.reply_tools import ReplyTools
from qingda.security import Vault
from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import user_scope
from qingda.tool_generation import generate


async def main():
    config = Config.from_env()
    store = Store(config.database_url)
    manager = Manager(store, Vault(config.key_file))
    gateway = Gateway(manager)
    tools = ReplyTools(SimpleNamespace())
    results = []
    try:
        with user_scope(1):
            await gateway.validate_model_key(gateway.secret("model_key"))
            for model in ("deepseek-flash",):
                for thinking in (False,):
                    options = {
                        **DEFAULT_MODEL,
                        "model": model,
                        "thinking_enabled": thinking,
                        "web_search_enabled": True,
                    }
                    result = await asyncio.wait_for(
                        generate(
                            gateway,
                            tools,
                            options,
                            "这是无敏感数据的公开接口验收。先调用calculate计算0.1+0.2，然后单独调用finish_reply：action=reply，content为计算结果0.3，reason=answer，close_dialogue=false，handled_message_ids=[1]。不能直接输出正文。",
                            [
                                {
                                    "role": "user",
                                    "content": "计算0.1+0.2，只输出结果",
                                    "current": True,
                                    "message_id": 1,
                                    "sender": "public-check",
                                }
                            ],
                            decision_mode=True,
                        ),
                        120,
                    )
                    assert result["reply_decision"]["content"] == "0.3"
                    calls = result["tool_trace"]["tool_calls"]
                    assert any(item["name"] == "calculate" and item["result"]["ok"] for item in calls)
                    row = dict(
                        model=model,
                        thinking=thinking,
                        passed=True,
                        rounds=result["tool_trace"]["model_rounds"],
                        usage=result["usage"],
                    )
                    results.append(row)
                    print(json.dumps(row), flush=True)
            print(json.dumps({"passed": len(results), "cases": results}), flush=True)
    except ProviderError as error:
        print(json.dumps({"passed": len(results), "failure": error.reason}), flush=True)
        raise SystemExit(1) from None
    finally:
        await tools.close()
        await gateway.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
