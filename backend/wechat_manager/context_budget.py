"""统一上下文预算；仅加载经哈希核验的官方 tokenizer 数据，不执行远程代码。"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer

VERSION = "conversation-context-v2"
TOKENIZER_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"
INPUT_TOKENS = 128000
RECALL_RESERVE = 24000
TEXT_BYTES = 2 * 1024 * 1024
PRIVATE = (12, 48, 12000)
GROUP = (8, 32, 8000)
BACKGROUND = (6, 1000)


@lru_cache(maxsize=1)
def tokenizer():
    data = Path(__file__).with_name("tokenizer").joinpath("tokenizer.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
        raise RuntimeError("上下文分词资源哈希不符")
    return Tokenizer.from_str(data.decode("utf-8"))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def tokens(value):
    return len(
        tokenizer().encode(value if isinstance(value, str) else dumps(value), add_special_tokens=False).ids
    )


def request_size(messages, tools=()):
    # 内联图片不能按 Base64 文本计数；每张按官方上界 1024 token 加协议余量。
    images = 0

    def text_view(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") == "image_url":
                images += 1
                return {"type": "image_url"}
            return {key: text_view(item) for key, item in value.items()}
        if isinstance(value, list):
            return [text_view(item) for item in value]
        return value

    text = dumps([text_view(messages), tools])
    return {
        "tokens": tokens(text) + images * 1056 + 32 * len(messages) + 256,
        "text_bytes": len(text.encode()),
    }


def request_tokens(messages, tools=()):
    return request_size(messages, tools)["tokens"]


def fit_prompt(context, prompt, tools):
    from .reply_queue import fit_context

    overhead = tokens(prompt) + tokens(tools) + 2048
    return fit_context(context, text_tokens=INPUT_TOKENS - RECALL_RESERVE - overhead)


def statistics(context):
    history = [v for v in context if not v.get("current")]
    return {
        "policy": VERSION,
        "tokenizer_sha256": TOKENIZER_SHA256,
        "selected_messages": len(history),
        "selected_rounds": len({v.get("history_round") for v in history}),
        "history_tokens": tokens(history),
        "context_tokens": tokens(context),
        "text_bytes": len(dumps(context).encode()),
        "selection": next((v["history_selection"] for v in context if "history_selection" in v), {}),
    }
