"""会话展示资格与关系、白名单和历史保留独立。"""

from .contacts import SYSTEM_CONTACTS


def chat_visible(peer, own_wxid):
    return bool(peer and peer not in SYSTEM_CONTACTS and peer != own_wxid and not peer.startswith("gh_"))


def visibility_sql(peer="c.peer_id", own_wxid="a.wxid"):
    # 列名由内部调用者提供；系统 ID 统一绑定，SQL 与单条详情采用相同规则。
    identifiers = tuple(sorted(SYSTEM_CONTACTS))
    return (
        f"({peer}!='' AND {peer} NOT IN ({','.join('%s' for _ in identifiers)}) "
        f"AND substr({peer},1,3)!='gh_' AND {peer}!=coalesce({own_wxid},''))",
        identifiers,
    )
