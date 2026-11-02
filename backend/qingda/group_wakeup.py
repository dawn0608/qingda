"""群聊唤醒依据。只在可信新回调入库前计算，发送前复用持久结果。"""

import json
import re

from .contact_changes import trusted_peer
from .protocol import safe_xml
from .store import string


def profile(conn, aid, peer, member):
    row = conn.execute(
        "SELECT profile_json FROM chat_member_profiles WHERE account_id=%s AND peer_id=%s AND member_id=%s",
        (aid, peer, member),
    ).fetchone()
    return json.loads(row[0]) if row else {}


def annotate(conn, account, event_row, event, msg):
    metadata = msg["metadata"]
    # 显式空值也持久化：后续资料变化和重新启动不能重新解释旧消息。
    metadata["wakeup_reason"] = None
    if not trusted_peer(conn, account, event_row, msg["peer"], require_wxid=True):
        return
    policy = conn.execute("SELECT * FROM dialogue_policy WHERE id=1").fetchone()
    if event_row["id"] > policy["last_old_event"] and (msg["created_at"] or 0) >= policy["enabled_at"]:
        metadata["dialogue_version"] = 1
    if msg["msg_type"] == "10002":
        if msg["kind"] != "group" and not metadata.get("dialogue_version"):
            return
        data = event.get("Data") or {}
        body = string(data.get("Content")) or ""
        prefix = None
        if ":\n" in body and not body.lstrip().startswith("<"):
            prefix, body = body.split(":\n", 1)
        root = safe_xml(body)
        if root is None or root.tag != "sysmsg" or root.get("type") != "pat":
            return
        nodes = root.findall("pat")
        if len(nodes) != 1:
            return
        node = nodes[0]
        fields = ("fromusername", "pattedusername", "chatusername")
        if any(len(node.findall(k)) != 1 for k in fields):
            return
        actor, target, session = (node.findtext(k) or "" for k in fields)
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", actor)
            or actor == account["wxid"]
            or target != account["wxid"]
            or (msg["kind"] == "group" and session != msg["peer"])
            or (
                msg["kind"] != "group"
                and (actor != msg["peer"] or session not in (msg["peer"], account["wxid"]))
            )
            or (prefix is not None and prefix not in (actor, msg["peer"]))
            or msg["sender"] not in (actor, msg["peer"])
            or msg["recipient"] not in (account["wxid"], msg["peer"])
            or msg["outgoing"]
        ):
            return
        member = profile(conn, account["id"], msg["peer"], actor)
        name = member.get("displayName") or member.get("nickName")
        name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or ""))[:200]
        who = (f"群成员“{name}”" if name else "一位群成员") if msg["kind"] == "group" else "当前联系人"
        msg.update(sender=actor, text=f"[拍一拍事件] {who}拍了拍你。")
        metadata.update(wakeup_reason="pat", message_kind="pat", pat_actor=actor, pat_target=target)
        return
    if msg["outgoing"] or msg["kind"] != "group":
        return
    if metadata.get("mentioned"):
        metadata["wakeup_reason"] = "mention"
        return
    if metadata.get("mentions"):
        return
    if metadata.get("reference_id") and metadata.get("message_kind") == "text":
        from .reply_references import assistant_reply

        conv = conn.execute(
            "SELECT id FROM conversations WHERE account_id=%s AND peer_id=%s", (account["id"], msg["peer"])
        ).fetchone()
        reply = (
            assistant_reply(
                conn, account["id"], conv["id"], metadata["reference_id"], event_row["received_at"]
            )
            if conv
            else None
        )
        if reply:
            metadata.update(wakeup_reason="reply_reference", reference_task_id=reply["id"])
            return
    # 明确的协议名单优先；引用、卡片内部的 @ 不参与文本呼叫匹配。
    if msg["msg_type"] != "1" or metadata.get("mentions"):
        return
    own = profile(conn, account["id"], msg["peer"], account["wxid"])
    names = {account["nickname"], own.get("displayName"), own.get("nickName")}
    text = msg["text"].lstrip()
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        if re.match(r"@?" + re.escape(name) + r"(?=$|[\s，。！？：；、,.!?:;])", text):
            metadata["wakeup_reason"] = "text_call"
            return
    if text.startswith("@"):
        metadata["directed_elsewhere"] = True


def awakened(metadata):
    return metadata.get("wakeup_reason") in (
        "mention",
        "text_call",
        "pat",
        "reply_reference",
        "continuation",
        "pat_backfill",
        "wait_continuation",
    )
