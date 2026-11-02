"""回复收集与明确等待的统一策略，不增加用户配置入口。"""

VERSION = "reply-policy-v4"
WAIT_SECONDS = 600


def collection_delay(kind, group):
    if kind == "pat":
        return 3
    if kind == "image":
        return 3 if group else 5
    return 1.5 if group else 2
