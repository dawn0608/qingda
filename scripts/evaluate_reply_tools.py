"""生产只读真实模型工具评测；保存答案和工具证据，绝不发送微信或写业务库。"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from evaluate_reply_batches import MARKDOWN, ReadSettings
from qingda.config import ROOT, Config
from qingda.model_settings import effective_model
from qingda.providers import Gateway
from qingda.reply_prompt import load
from qingda.security import Vault
from qingda.tenancy import current_user, user_scope
from reply_evaluation import account_prompt as read_account_prompt
from reply_evaluation import prompt_metrics


def published_on(value, date, zone="Asia/Shanghai"):
    """供应商时间为UTC；按提问者当地日期判分，不截取UTC日期字符串。"""
    if not isinstance(value, str):
        return False
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            timestamp.tzinfo is not None and timestamp.astimezone(ZoneInfo(zone)).date().isoformat() == date
        )
    except ValueError:
        return False


def evidence_checks(case, answer, trace):
    def contains_temperature(value):
        numbers = [float(part) for part in re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?", answer)]
        return any(abs(number - value) <= 0.51 for number in numbers)

    successful = [item for item in trace if item["result"].get("ok")]
    checks = {
        "required_tools": set(case["tools"]) <= {item["name"] for item in successful},
        "plain_text": not MARKDOWN.search(answer),
        "concise": 0 < len(answer) <= 200,
    }
    if case.get("no_tools"):
        checks["no_tools"] = not trace
    if case.get("expected"):
        checks["exact"] = answer == case["expected"]
    if case.get("contains"):
        checks["contains"] = case["contains"] in answer
    if case.get("clarify"):
        checks["location_clarification"] = any(
            word in answer for word in ("城市", "地区", "哪里", "地点", "哪个")
        ) and not any(item["name"] == "get_weather" for item in trace)
    for item in successful:
        data = item["result"]["data"]
        if item["name"] == "get_current_time":
            checks["current_date"] = (
                data["date"]
                == datetime.now(timezone.utc).astimezone(ZoneInfo(data["timezone"])).date().isoformat()
            )
        elif item["name"] == "get_weather":
            if data["mode"] == "current":
                checks["weather_temperature"] = contains_temperature(data["temperature"]["value"])
                checks["weather_condition"] = data["condition"]["text"] in answer
            else:
                day = data["days"][-1]
                checks["weather_range"] = all(
                    contains_temperature(day[key]["value"]) for key in ("temperatureMin", "temperatureMax")
                )
        elif item["name"] == "web_search":
            checks["search_sources"] = bool(data["results"]) and all(
                row["title"] and row["url"] and row["summary"] for row in data["results"]
            )
            checks["search_no_default_links"] = "http" not in answer
            if case.get("today_source"):
                today = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
                checks["today_source"] = any(
                    published_on(row.get("published_at"), today) for row in data["results"]
                )
        elif item["name"] == "web_fetch":
            checks["fetch_content"] = bool(data["content"] and data["url"])
    return checks


async def evaluate(account_id, output, case_file, account_prompt_file=None):
    config = Config.from_env()
    settings = ReadSettings(config.database_url)
    model = effective_model(settings.setting("model", {}))
    assert model["provider"] == "deepseek" and model["model"] == "deepseek-flash"
    account_prompt = read_account_prompt(config.database_url, account_id, account_prompt_file)
    template, prompt = load(account_prompt, "private", config.prompt_file)
    spec = json.loads(case_file.read_text(encoding="utf-8"))
    manager = SimpleNamespace(store=settings, vault=Vault(config.key_file))
    gateway = Gateway(manager, tool_config=config)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = []
    try:
        for case in spec["cases"]:
            context = [
                {
                    "role": "user",
                    "current": True,
                    "message_id": 1,
                    "sender": "evaluation-user",
                    "content": case["question"],
                }
            ]
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    gateway.model_with_tools(model, prompt, context, decision_mode=True),
                    timeout=model["timeout"],
                )
                answer = response["choices"][0]["message"]["content"]
                trace = response["tool_trace"]["tool_calls"]
                checks = evidence_checks(case, answer, trace)
                item = {
                    "id": case["id"],
                    "answer": answer,
                    "checks": checks,
                    "passed": all(checks.values()),
                    "usage": response["usage"],
                    "trace": response["tool_trace"],
                }
            except Exception as error:
                item = {
                    "id": case["id"],
                    "passed": False,
                    "error": getattr(error, "reason", type(error).__name__),
                }
            item["duration_ms"] = int((time.monotonic() - started) * 1000)
            results.append(item)
            from qingda.reply_decisions import TOOL

            receipt = {
                "user_id": current_user.get(),
                "revision": os.getenv("APP_REVISION")
                or json.loads((ROOT / "release.json").read_bytes())["revision"],
                "case_version": spec["case_version"],
                "grading_version": spec["grading_version"],
                "prompt_sha256": hashlib.sha256(template.encode()).hexdigest(),
                "account_prompt_sha256": hashlib.sha256(account_prompt.encode()).hexdigest(),
                "prompt_metrics": prompt_metrics(prompt, TOOL),
                "model": model["model"],
                "wechat_send_count": 0,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "results": results,
            }
            (output / "receipt.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {k: v for k, v in item.items() if k not in ("trace", "usage")}, ensure_ascii=False
                ),
                flush=True,
            )
    finally:
        await gateway.close()
    return all(item["passed"] for item in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True, help="已授权的用户范围")
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--account-prompt-file", type=Path, help="只读候选账号设定")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-file", type=Path, default=ROOT / "evals/acceptance/reply-tools.json")
    args = parser.parse_args()
    with user_scope(args.user_id):
        raise SystemExit(
            0
            if asyncio.run(evaluate(args.account_id, args.output, args.case_file, args.account_prompt_file))
            else 1
        )
