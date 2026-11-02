"""真实模型评测共用的只读账号设定与提示词度量。"""

import hashlib
import json
from pathlib import Path

import psycopg
from qingda.context_budget import tokens
from qingda.tenancy import current_user


def account_prompt(database, account_id, prompt_file=None):
    if prompt_file is not None:
        value = Path(prompt_file).read_text(encoding="utf-8").strip()
    else:
        with psycopg.connect(database, options="-c default_transaction_read_only=on") as conn:
            if current_user.get() is None:
                raise ValueError("评测账号必须显式指定用户范围")
            conn.execute("SELECT set_config('app.user_id',%s,true)", (str(current_user.get()),))
            row = conn.execute(
                "SELECT prompt FROM accounts WHERE user_id=%s AND id=%s", (current_user.get(), account_id)
            ).fetchone()
        value = row[0] if row else ""
    if not value or not value.strip() or len(value) > 50000:
        raise ValueError("评测账号提示词为空或超限")
    return value


def prompt_metrics(prompt, finish_tool, *, history_enabled=False):
    from qingda.reply_tools import TOOLS

    available = [*TOOLS, finish_tool]
    if history_enabled:
        from qingda.history_tool import TOOL as HISTORY_TOOL

        available.append(HISTORY_TOOL)
    return {
        "characters": len(prompt),
        "tokens": tokens(prompt),
        "sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "finish_tool_tokens": tokens(finish_tool),
        "finish_tool_sha256": hashlib.sha256(
            json.dumps(finish_tool, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "tools_tokens": tokens(available),
        "tools_sha256": hashlib.sha256(
            json.dumps(available, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
    }


def weather_checks(spec, answer, trace):
    """按本次真实工具原文判分，不冻结天气数字或把HTTP成功当答案正确。"""
    import re
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    calls = [v for v in trace if v["name"] == "get_weather"]
    successful = [v for v in calls if v["result"].get("ok")]
    checks = {
        "weather_succeeded": bool(successful),
        "weather_source_display": ("和风天气" in answer)
        if spec.get("source_requested")
        else ("和风天气" not in answer and "qweather" not in answer.casefold()),
    }
    numbers = [float(v) for v in re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?", answer)]
    for index, call in enumerate(successful):
        data = call["result"]["data"]
        prefix = f"weather_{index}_"
        checks[prefix + "location"] = data["location"] == spec["location"]
        checks[prefix + "mode"] = data["mode"] in spec.get("allowed_modes", [spec.get("mode", "current")])
        checks[prefix + "evidence"] = bool(call.get("location_evidence", {}).get("message_id"))
        checks[prefix + "real_requests"] = call["result"].get("request_usage", {}).get("requests", 0) >= 2
        if data["mode"] == "current":
            values = [data["temperature"]["value"]]
            checks[prefix + "condition"] = data["condition"]["text"] in answer
        else:
            day = data["days"][-1]
            values = [day[k]["value"] for k in ("temperatureMin", "temperatureMax")]
            target = datetime.now(timezone.utc).astimezone(ZoneInfo(data["timezone"])).date()
            checks[prefix + "date"] = (
                day["date"] == (target + timedelta(days=spec.get("day_offset", 0))).isoformat()
            )
            conditions = [day.get(k, {}).get("condition", {}).get("text") for k in ("daytime", "nighttime")]
            checks[prefix + "condition"] = any(v and v in answer for v in conditions)
        checks[prefix + "temperature"] = all(any(abs(n - v) <= 0.51 for n in numbers) for v in values)
    return checks
