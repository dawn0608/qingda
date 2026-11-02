"""真实模型的等待控制评测；只读配置，复用生产判定入口，不发送微信。"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from evaluate_reply_batches import ReadSettings
from qingda.config import ROOT, Config
from qingda.input_waits import classify
from qingda.model_settings import effective_model
from qingda.providers import Gateway
from qingda.security import Vault
from qingda.tenancy import user_scope


class EvaluationBudget:
    """独立顺序评测，没有业务任务；每例共享120秒上限。"""

    async def run(self, phase, factory):
        return await factory()

    async def slot(self, resource, limit, factory):
        return await factory()


async def evaluate(args):
    config = Config.from_env()
    settings = ReadSettings(config.database_url)
    model = effective_model(settings.setting("model", {}))
    gateway = Gateway(SimpleNamespace(store=settings, vault=Vault(config.key_file)), tool_config=config)
    original_chat = gateway.chat
    protocol_calls = []

    async def traced_chat(*values, **kwargs):
        response = await original_chat(*values, **kwargs)
        choice = (response.get("choices") or [{}])[0]
        protocol_calls.append(
            {
                "finish_reason": choice.get("finish_reason"),
                "tool_calls": choice.get("message", {}).get("tool_calls", []),
            }
        )
        return response

    gateway.chat = traced_chat
    spec_path = ROOT / "evals/acceptance/input-wait-control.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = []
    try:
        for run in range(1, 4):
            for case in spec["cases"]:
                protocol_calls.clear()
                tick = time.monotonic()
                current = [
                    {
                        "message_id": 2,
                        "message_kind": case.get("kind", "text"),
                        "content": case["text"],
                        "media_status": "ready",
                    }
                ]
                try:
                    result, usage, rounds = await asyncio.wait_for(
                        classify(
                            gateway,
                            model,
                            current,
                            {"message_id": 1, "content": "等我发完再总结"},
                            EvaluationBudget(),
                        ),
                        timeout=model["timeout"],
                    )
                    value = {
                        "id": case["id"],
                        "run": run,
                        "decision": result,
                        "usage": usage,
                        "model_rounds": rounds,
                        "passed": result["action"] == case["expected"],
                    }
                except Exception as error:
                    value = {
                        "id": case["id"],
                        "run": run,
                        "passed": False,
                        "error": getattr(error, "reason", type(error).__name__),
                    }
                value["duration_ms"] = int((time.monotonic() - tick) * 1000)
                value["protocol_calls"] = list(protocol_calls)
                results.append(value)
                print(json.dumps(value, ensure_ascii=False), flush=True)
                (args.output / "receipt.json").write_text(
                    json.dumps(
                        {
                            "case_version": spec["case_version"],
                            "grading_version": spec["grading_version"],
                            "case_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
                            "model": model["model"],
                            "revision": os.getenv("APP_REVISION"),
                            "model_configuration": {
                                k: model[k]
                                for k in (
                                    "provider",
                                    "model",
                                    "thinking_enabled",
                                    "web_search_enabled",
                                    "timeout",
                                    "max_tokens",
                                )
                                if k in model
                            },
                            "prompt_sha256": hashlib.sha256(
                                (ROOT / "prompts/input_wait_control.md").read_bytes()
                            ).hexdigest(),
                            "results": results,
                            "wechat_send_count": 0,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
    finally:
        await gateway.close()
    if not all(v["passed"] for v in results):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with user_scope(args.user_id):
        asyncio.run(evaluate(args))
