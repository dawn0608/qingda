"""后端唯一的微信内置表情词表。"""

import json


def compile_catalog(root):
    return json.loads((root / "backend/wechat_manager/native_emotions.json").read_text(encoding="utf-8"))
