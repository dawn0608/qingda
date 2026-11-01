"""使用服务器现有模型与账号提示词评测合并输入及微信表达；只读业务库，不发送微信。"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import psycopg
from PIL import Image, ImageDraw
from reply_evaluation import account_prompt, prompt_metrics
from wechat_manager.config import ROOT, Config
from wechat_manager.model_settings import effective_model
from wechat_manager.providers import Gateway
from wechat_manager.reply_prompt import load
from wechat_manager.security import Vault
from wechat_manager.tenancy import current_user, user_scope


class ReadSettings:
    def __init__(self, database):
        self.database = database

    def setting(self, key, default=None):
        with psycopg.connect(self.database, options="-c default_transaction_read_only=on") as conn:
            if current_user.get() is None:
                raise ValueError("真实评测必须显式指定用户范围")
            conn.execute("SELECT set_config('app.user_id',%s,true)", (str(current_user.get()),))
            row = conn.execute(
                "SELECT value FROM settings WHERE user_id=%s AND key=%s", (current_user.get(), key)
            ).fetchone()
        return json.loads(row[0]) if row else default


MARKDOWN = re.compile(
    r"(?m)^\s*(?:#{1,6}\s|```|~~~|>\s|[-*+]\s|\d+[.)]\s|\|.*\|)"
    r"|\*\*[^\n]+?\*\*|__[^\n]+?__|~~[^\n]+?~~"
    r"|(?<!\*)\*[^\s*\n][^*\n]*?\*(?!\*)|`[^`\n]+`"
    r"|!?\[[^\]\n]+\]\([^\)\n]+\)|</?[A-Za-z][^>\n]*>"
    r"|\\[\[(]|\$\$|\$[^$\n]+\$"
)


def grade(case, answer, choice):
    checks = {
        "complete": choice.get("finish_reason") == "stop"
        and bool(answer)
        and not choice.get("message", {}).get("tool_calls")
    }
    if "expected" in case:
        checks["exact"] = answer == case["expected"]
    if case.get("plain_text"):
        checks["plain_text"] = not MARKDOWN.search(answer)
    if "max_chars" in case:
        checks["length"] = len(answer) <= case["max_chars"]
    if "max_paragraphs" in case:
        checks["paragraphs"] = len(re.split(r"\n\s*\n", answer)) <= case["max_paragraphs"]
    if "max_questions" in case:
        checks["questions"] = len(re.findall(r"[?？]", answer)) <= case["max_questions"]
    if case.get("contains_all"):
        checks["literal_content"] = all(value in answer for value in case["contains_all"])
    if case.get("contains_any_groups"):
        checks["key_points"] = all(
            any(value in answer for value in group) for group in case["contains_any_groups"]
        )
    if case.get("excludes"):
        checks["no_wrapper"] = not any(value in answer for value in case["excludes"])
    if case.get("unsupported_video"):
        checks["unsupported_video"] = bool(
            re.search(
                r"(?:不支持|不能|无法|没法|看不了)(?:直接)?(?:查看|观看|读取|处理|分析|看)?(?:这个|这类)?视频"
                r"|视频[^。！？\n]{0,12}(?:看不了|不支持|无法(?:查看|观看|读取|处理)|不能(?:看|处理))",
                answer,
            )
        )
        checks["no_video_resend"] = not re.search(
            r"把视频(?:发|传)|视频发(?:来|给|上)|视频链接|再发一次|重发视频", answer
        )
        checks["no_false_asr_failure"] = not re.search(
            r"(?:没收到|没有拿到|没拿到|没有收到)[^。！？\n]{0,15}转写|听不到", answer
        )
    return checks


async def evaluate(
    account_id,
    output,
    case_file,
    private=False,
    baseline_root=None,
    without_profiles=False,
    repeat=1,
    account_prompt_file=None,
):
    config = Config.from_env()
    settings = ReadSettings(config.database_url)
    model = effective_model(settings.setting("model", {}))
    if model["provider"] != "deepseek" or model["model"] != "deepseek-flash":
        raise ValueError("评测仅使用本次已确认的现有 DeepSeek deepseek-flash 配置")
    selected_prompt = account_prompt(config.database_url, account_id, account_prompt_file)
    cases = json.loads(case_file.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    image_path = output / "three-green-dots.png"
    picture = Image.new("RGB", (240, 100), "white")
    draw = ImageDraw.Draw(picture)
    for x in (20, 100, 180):
        draw.ellipse((x, 30, x + 35, 65), fill="green")
    picture.save(image_path)
    image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    media = SimpleNamespace(path=lambda ref: image_path)
    manager = SimpleNamespace(store=settings, vault=Vault(config.key_file), media=media)
    gateway = Gateway(manager, tool_config=config)
    results = []
    templates = {}
    metrics = {}
    try:
        for run_number, case in [
            (number, case) for number in range(1, repeat + 1) for case in cases["cases"]
        ]:
            kind = case.get("kind", "private")
            template, prompt = load(selected_prompt, kind, config.prompt_file)
            if baseline_root:
                template, prompt = load(selected_prompt, kind, baseline_root / "prompts/auto_reply.md")
            templates[kind] = hashlib.sha256(template.encode()).hexdigest()
            from wechat_manager.reply_decisions import TOOL

            metrics[kind] = prompt_metrics(prompt, TOOL)
            case_history = (
                json.loads((case_file.parent / case["history_file"]).read_text(encoding="utf-8"))
                if case.get("history_file")
                else case["history"]
            )
            if case.get("history_file"):
                background_ids = {
                    v["message_id"] for v in case_history if v.get("context_scope") == "background"
                }
                keep_background = [
                    v["message_id"] for v in case_history if v.get("context_scope") == "background"
                ][-20:]
                case_history = [
                    v
                    for v in case_history
                    if v["message_id"] not in background_ids or v["message_id"] in keep_background
                ]
            image_reference = {"message_id": 1, "sha256": image_hash, "width": 240, "height": 100}
            context = [
                {"role": "user", "content": value} if isinstance(value, str) else value
                for value in case_history
            ]
            for index, text in enumerate(case["current_messages"]):
                item = {
                    "role": "user",
                    "current": True,
                    "message_id": index + 1,
                    "sender": "evaluation-user",
                    "reply_target": "evaluation-user",
                    "conversation_kind": kind,
                    "wakeup_reason": case.get("wakeup_reason", "continuation" if kind == "group" else None),
                    "content": text if isinstance(text, str) else text["content"],
                    "message_kind": "image" if case.get("image") and index == 0 else "text",
                }
                if isinstance(text, dict):
                    item.update(text)
                if index == 0 and case.get("participant_profiles") and not without_profiles:
                    item["participant_profiles"] = case["participant_profiles"]
                    item["profile_schema_version"] = "participant-profiles-v1"
                if case.get("image") and index == 0:
                    item["images"] = [image_reference]
                context.append(item)
            original_run = gateway.reply_tools.run
            if case.get("controlled_weather_error"):

                async def weather_error_run(name, arguments):
                    if name == "get_weather":
                        return {"ok": False, "error": case["controlled_weather_error"]}
                    return await original_run(name, arguments)

                gateway.reply_tools.run = weather_error_run
            if case.get("frozen_search") or case.get("frozen_fetch"):
                frozen = (
                    json.loads((case_file.parent / case["frozen_search"]).read_text(encoding="utf-8"))
                    if case.get("frozen_search")
                    else None
                )

                async def frozen_run(name, arguments):
                    if name == "web_search" and frozen is not None:
                        return frozen
                    if name == "web_fetch" and case.get("frozen_fetch"):
                        return case["frozen_fetch"].get(
                            arguments.get("url"),
                            {
                                "ok": False,
                                "error": {
                                    "code": "fixture_url_unavailable",
                                    "message": "该网址不在冻结资料内",
                                },
                            },
                        )
                    return await original_run(name, arguments)

                gateway.reply_tools.run = frozen_run
            started = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    gateway.model_with_tools(model, prompt, context, decision_mode=True),
                    timeout=model["timeout"],
                )
            except Exception as error:
                results.append(
                    {
                        "id": case["id"],
                        "run": run_number,
                        "passed": False,
                        "error": getattr(error, "reason", type(error).__name__),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    }
                )
                gateway.reply_tools.run = original_run
                continue
            gateway.reply_tools.run = original_run
            choice = response["choices"][0]
            answer = choice.get("message", {}).get("content", "")
            checks = grade(case, answer, choice)
            if case.get("weather"):
                from reply_evaluation import weather_checks

                checks.update(weather_checks(case["weather"], answer, response["tool_trace"]["tool_calls"]))
            if case.get("forbidden_weather_requests"):
                checks["no_weather_network"] = not any(
                    v["name"] == "get_weather"
                    and (
                        v["result"].get("ok")
                        or v["result"].get("request_usage", {}).get("requests", 0)
                        or v["result"].get("error", {}).get("request_sent")
                    )
                    for v in response["tool_trace"]["tool_calls"]
                )
            if case.get("required_successful_tools"):
                checks["required_tools"] = set(case["required_successful_tools"]) <= {
                    call["name"]
                    for call in response.get("tool_trace", {}).get("tool_calls", [])
                    if call["result"].get("ok")
                }
            if case.get("forbidden_successful_tools"):
                checks["no_profile_location_query"] = not any(
                    call["name"] in case["forbidden_successful_tools"] and call["result"].get("ok")
                    for call in response.get("tool_trace", {}).get("tool_calls", [])
                )
            if case.get("required_failed_tools"):
                checks["required_failures"] = set(case["required_failed_tools"]) <= {
                    call["name"]
                    for call in response.get("tool_trace", {}).get("tool_calls", [])
                    if not call["result"].get("ok")
                }
            if case.get("forbidden_tools"):
                checks["no_unneeded_tool_calls"] = not any(
                    call["name"] in case["forbidden_tools"]
                    for call in response.get("tool_trace", {}).get("tool_calls", [])
                )
            decision = response.get("reply_decision") or {"action": "reply", "close_dialogue": False}
            expected_action = case.get("expected_action", "reply")
            checks["decision"] = decision["action"] == expected_action
            if expected_action in ("silent", "wait"):
                checks["complete"] = (
                    choice.get("finish_reason") == "stop"
                    and not answer
                    and decision["action"] == expected_action
                )
            if "expected_close" in case:
                checks["closure"] = decision["close_dialogue"] == case["expected_close"]
            if "expected_reason" in case:
                checks["reason"] = decision.get("reason") == case["expected_reason"]
            results.append(
                {
                    "id": case["id"],
                    "run": run_number,
                    "answer": answer,
                    "decision": decision,
                    "finish_reason": choice.get("finish_reason"),
                    "passed": all(checks.values()),
                    "checks": checks,
                    "characters": len(answer),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "usage": response.get("usage", {}),
                    "tool_trace": response.get("tool_trace", {}),
                    "context_sha256": hashlib.sha256(
                        json.dumps(context, ensure_ascii=False, sort_keys=True).encode()
                    ).hexdigest(),
                    "controlled_weather_error": bool(case.get("controlled_weather_error")),
                }
            )
            (output / "partial.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps({"id": case["id"], "run": run_number, "passed": all(checks.values())}), flush=True
            )
    finally:
        await gateway.close()
    receipt = {
        "user_id": current_user.get(),
        "case_version": cases["case_version"],
        "grading_version": cases["grading_version"],
        "case_sha256": hashlib.sha256(case_file.read_bytes()).hexdigest(),
        "revision": os.getenv("EVALUATION_REVISION")
        or json.loads((ROOT / "release.json").read_bytes())["revision"],
        "model": model["model"],
        "model_configuration": {
            k: model[k]
            for k in ("provider", "model", "thinking_enabled", "web_search_enabled", "timeout", "max_tokens")
            if k in model
        },
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder, pattern in (("backend/wechat_manager", "*.py"), ("prompts", "*.md"))
            for path in (ROOT / folder).glob(pattern)
        },
        "reply_policy": "baseline"
        if baseline_root
        else __import__("wechat_manager.reply_decisions", fromlist=["VERSION"]).VERSION,
        "repetitions": repeat,
        "prompt_sha256": templates,
        "prompt_metrics": metrics,
        "account_prompt_sha256": hashlib.sha256(selected_prompt.encode()).hexdigest(),
        "wechat_send_count": 0,
        "profiles_included": not without_profiles,
        "results": results,
    }
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))
    if not all(item["passed"] for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True, help="已授权的用户范围")
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--account-prompt-file", type=Path, help="只读、已填好助手名称的候选设定，不写账号")
    parser.add_argument("--repeat", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private", action="store_true", help="加载私聊补充模板")
    parser.add_argument("--baseline-root", type=Path, help="只读旧版模板路径，用同一组输入作基线对照")
    parser.add_argument(
        "--without-profiles", action="store_true", help="同模板同输入移除资料，用于资料能力对照"
    )
    parser.add_argument("--case-file", type=Path, default=ROOT / "evals/acceptance/reply-batches.json")
    args = parser.parse_args()
    with user_scope(args.user_id):
        asyncio.run(
            evaluate(
                args.account_id,
                args.output,
                args.case_file,
                args.private,
                args.baseline_root,
                args.without_profiles,
                args.repeat,
                args.account_prompt_file,
            )
        )
