"""Worker与真实模型评测共用同一场景模板装配入口。"""

import json

from .config import ROOT
from .private_context import emotion_tokens
from .providers import ProviderError


def fixed(name, **variables):
    template = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    if template.startswith("<!--"):
        _, separator, template = template.partition("-->")
        if not separator:
            raise ProviderError("invalid_prompt_template")
    try:
        return template.strip().format(**variables)
    except (KeyError, ValueError) as error:
        raise ProviderError("invalid_prompt_template") from error


def load_default(assistant_name):
    template = (ROOT / "prompts/assistant_default.md").read_text(encoding="utf-8")
    if "{assistant_name}" not in template or not assistant_name.strip():
        raise ProviderError("invalid_prompt_template")
    if template.startswith("<!--"):
        _, separator, template = template.partition("-->")
        if not separator:
            raise ProviderError("invalid_prompt_template")
    return template.strip().format(assistant_name=assistant_name.strip())


def load(account_prompt, kind, prompt_file=ROOT / "prompts/auto_reply.md", *, owner_profile=""):
    template = prompt_file.read_text(encoding="utf-8")
    if "{account_prompt}" not in template or kind not in ("private", "group"):
        raise ProviderError("invalid_prompt_template")
    if prompt_file.name == "auto_reply.md":
        template += "\n" + prompt_file.with_name(f"{kind}_reply.md").read_text(encoding="utf-8")
        if kind == "private" and "{builtin_emotions}" not in template:
            raise ProviderError("invalid_prompt_template")
    template += "\n" + prompt_file.with_name("reply_decision.md").read_text(encoding="utf-8")
    return template, template.format(
        account_prompt=account_prompt,
        builtin_emotions=emotion_tokens() if kind == "private" else "",
        owner_profile=json.dumps(
            {"configured": bool(owner_profile.strip()), "markdown": owner_profile}, ensure_ascii=False
        ),
    )
