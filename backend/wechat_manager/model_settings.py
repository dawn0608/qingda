"""逐用户模型配置与服务端统一运行参数。"""

TEXT_TIMEOUT = 120
OUTPUT_TOKENS = 16384
MODEL = "deepseek-flash"
DEFAULT_MODEL = {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": MODEL,
    "timeout": TEXT_TIMEOUT,
    "max_tokens": OUTPUT_TOKENS,
    "thinking_enabled": False,
    "web_search_enabled": True,
}
EDITABLE_FIELDS = {"model"}


def masked_key(value):
    if not value:
        return ""
    if len(value) <= 15:
        return "********"
    return value[:7] + "********" + value[-4:]


def effective_model(saved=None):
    return {**DEFAULT_MODEL, "timeout": TEXT_TIMEOUT, "max_tokens": OUTPUT_TOKENS}


def validate_model(changes):
    if "model" in changes and changes["model"] != MODEL:
        raise ValueError("模型固定为 deepseek-flash")
    for key in ("provider", "base_url"):
        if key in changes and changes[key] != DEFAULT_MODEL[key]:
            raise ValueError("仅使用 DeepSeek 官方接口")
    for key in ("thinking_enabled", "web_search_enabled"):
        if key in changes and type(changes[key]) is not bool:
            raise ValueError("模型开关必须为布尔值")


def available_tools(config, tools):
    return list(tools)
