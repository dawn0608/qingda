"""管理台只读聊天记录；与模型上下文及自动回复资格独立。"""

import base64
import hashlib
import json
import math
import os
import re
import stat
import struct

from .chat_visibility import chat_visible
from .conversation_options import query_parts
from .protocol import TYPE_NAMES, safe_xml

SCAN_LIMIT = 1000
SOURCE = """ FROM messages m
    LEFT JOIN message_contents raw ON raw.id=m.content_id
    LEFT JOIN incoming_media media ON media.message_id=m.id
    LEFT JOIN media_resources resource ON resource.message_id=m.id
    LEFT JOIN message_views mv ON mv.key=('m:'||lpad(m.id::text,20,'0'))
    LEFT JOIN message_views tv ON tv.key=('t:'||m.outbound_task_id)
    LEFT JOIN tasks sent ON sent.id=m.outbound_task_id
    LEFT JOIN reply_participant_profiles participant ON participant.account_id=m.account_id
        AND participant.conversation_id=m.conversation_id AND participant.member_id=m.sender
        AND participant.owner_wxid=%s
    LEFT JOIN chat_member_profiles member ON member.account_id=m.account_id
        AND member.peer_id=%s AND member.member_id=m.sender
    WHERE m.account_id=%s AND m.conversation_id=%s
        AND m.msg_type NOT IN ('51','10000','10002')
        AND (m.msg_type<>'37' OR coalesce((m.metadata->>'friend_greeting')::boolean,false))
        AND NOT coalesce((m.metadata->>'test')::boolean,false)
        AND (m.outbound_task_id IS NULL OR m.event_id IS NOT NULL OR sent.state='accepted'
            OR m.metadata->>'send_state'='accepted')
"""
SELECT = """SELECT m.*,raw.xml,media.transcript,resource.file_name,
    media.state AS media_state,media.kind AS media_kind,media.reason AS media_reason,
    media.sha256 AS media_sha256,
    media.frames_json,media.trace_json AS media_trace,
    greatest(coalesce(mv.revoked,0),coalesce(tv.revoked,0)) AS revoked,
    participant.group_name,participant.profile_json AS participant_profile,
    member.profile_json AS member_profile"""


def decoded(value):
    return json.loads(value) if isinstance(value, str) and value else value or {}


def xml_body(value, group=False):
    if group and ":\n" in value and not value.lstrip().startswith("<"):
        value = value.split(":\n", 1)[1]
    return safe_xml(value)


def conversation(conn, aid, cid):
    name, _, contact_source = query_parts()
    conv = conn.execute(
        "SELECT c.*,a.wxid,a.nickname AS account_name,"
        + name
        + " AS display_name"
        + contact_source
        + " WHERE c.id=%s AND c.account_id=%s AND a.archived_at IS NULL",
        (cid, aid),
    ).fetchone()
    if not conv or not chat_visible(conv["peer_id"], conv["wxid"]):
        raise ValueError("会话不存在")
    return {**conv, "name": conv["display_name"]}


def image_reference(row):
    """只读已确认静态图片的像素引用；不准备、修复或补取素材。"""
    if (
        row["msg_type"] != "3"
        or row.get("media_state") != "ready"
        or row.get("media_kind") != "image"
        or row.get("media_reason")
    ):
        return None
    metadata, trace = decoded(row["metadata"]), decoded(row.get("media_trace"))
    frames = decoded(row.get("frames_json"))
    if (
        not isinstance(metadata, dict)
        or not isinstance(trace, dict)
        or metadata.get("auto_reply_exclusion")
        or trace.get("animated")
        or metadata.get("visual_frame_count", 1) != 1
        or trace.get("frame_count") != 1
        or not isinstance(frames, list)
        or len(frames) != 1
    ):
        return None
    frame = frames[0]
    if (
        not isinstance(frame, dict)
        or type(frame.get("message_id")) is not int
        or frame["message_id"] != row["id"]
        or not isinstance(frame.get("sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", frame["sha256"])
        or any(
            type(frame.get(key)) is not int or not 0 < frame[key] <= 16_000_000 for key in ("width", "height")
        )
        or frame["width"] * frame["height"] > 16_000_000
    ):
        return None
    return frame


def image_content(manager, aid, cid, mid):
    """认证后读取同一会话的现有缓存；释放数据库连接后再读取文件。"""
    with manager.store.connect(snapshot=True) as conn:
        try:
            conv = conversation(conn, aid, cid)
        except ValueError:
            return None
        row = conn.execute(
            SELECT + SOURCE + " AND m.id=%s",
            (conv["wxid"], conv["peer_id"], aid, cid, mid),
        ).fetchone()
        frame = image_reference(row) if row else None
    if not frame:
        return None
    try:
        path = manager.media.path(frame)
        # 媒体目录不作为静态目录开放，也不跟随被替换的符号链接。
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 32 <= info.st_size <= 8 * 1024 * 1024:
                return None
            body = stream.read(8 * 1024 * 1024 + 1)
        if (
            len(body) != info.st_size
            or hashlib.sha256(body).hexdigest() != frame["sha256"]
            or body[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            or struct.unpack(">II", body[16:24]) != (frame["width"], frame["height"])
        ):
            return None
        return body
    except (OSError, ValueError):
        return None


def presentation(row, conversation):
    """只提取可见文字，不输出 XML、下载凭据或原始回调。"""
    metadata = decoded(row["metadata"])
    msg_type = row["msg_type"]
    label = TYPE_NAMES.get(msg_type, "其他消息")
    if metadata.get("friend_greeting"):
        label = "打招呼"
    root = xml_body(row.get("xml") or "", conversation["kind"] == "group")
    content = []
    duration = metadata.get("duration_ms")
    if not duration and root is not None and msg_type == "34":
        node = root if root.tag == "voicemsg" else root.find(".//voicemsg")
        duration = node.get("voicelength") if node is not None else None
    duration = int(duration) if str(duration or "").isdigit() else 0

    def add(value):
        if isinstance(value, str) and value.strip() and value.strip() not in content:
            content.append(value.strip())

    def find(tag):
        if root is None:
            return None
        if tag.startswith(root.tag + "/"):
            return root.findtext(tag[len(root.tag) + 1 :])
        return root.findtext(".//" + tag)

    if msg_type == "1" or metadata.get("message_kind") == "text" or metadata.get("friend_greeting"):
        # 正文保留原有换行和首尾空白；不执行 Markdown、HTML 或嵌入资源。
        if row["text"]:
            content.append(row["text"])
    if msg_type == "49":
        subtype = find("appmsg/type") or metadata.get("appmsg_type")
        label = {
            "5": "链接",
            "6": "文件",
            "19": "聊天记录",
            "24": "收藏",
            "33": "小程序",
            "36": "小程序",
            "44": "小程序",
            "57": "引用消息",
            "2000": "转账",
            "2001": "红包",
        }.get(subtype, "应用消息")
    elif msg_type == "50":
        label = "通话"

    # 元信息曾截断标题；优先从保存的原始 XML 提取完整文字。
    if msg_type != "1":
        for tag in ("appmsg/title", "appmsg/des", "title", "des", "description", "filename"):
            add(find(tag))
        if not content:
            for key in ("title", "des", "description"):
                add(metadata.get(key))
        add(row.get("file_name"))
        if msg_type == "48" and root is not None:
            node = root if root.tag == "location" else root.find(".//location")
            if node is not None:
                add(node.get("poiname"))
                add(node.get("label"))
        if msg_type == "42" and root is not None:
            add(root.get("nickname"))
        if row.get("transcript"):
            add(row["transcript"])
        if duration > 0:
            add(f"{math.ceil(duration / 1000)}秒")
        elif msg_type in ("43", "62"):
            seconds = metadata.get("playlength")
            if not seconds and root is not None:
                node = root if root.tag == "videomsg" else root.find(".//videomsg")
                seconds = node.get("playlength") if node is not None else None
            if str(seconds or "").isdigit() and int(seconds) > 0:
                add(f"{int(seconds)}秒")
        if not content and msg_type not in TYPE_NAMES:
            if root is not None:
                for tag in ("content", "text", "summary", "digest"):
                    add(find(tag))
                if not content and not list(root):
                    add(root.text)
            elif row.get("xml") and "<" not in row["xml"]:
                add(row["xml"])

    outbound = row["direction"] == "outbound"
    if outbound:
        name = conversation["account_name"] or "我"
    elif conversation["kind"] == "private":
        name = conversation["name"] or row["sender"] or "好友"
    else:
        profile = decoded(row.get("participant_profile"))
        member = decoded(row.get("member_profile"))
        name = (
            row.get("group_name")
            or profile.get("nickname")
            or member.get("displayName")
            or member.get("nickName")
            or member.get("nickname")
            or row["sender"]
            or "群成员"
        )
    frame = image_reference(row)
    return {
        "id": str(row["id"]),
        "sender": row["sender"],
        "sender_name": name,
        "timestamp": row["created_at"],
        "direction": row["direction"],
        "source": row["source"],
        "msg_type": msg_type,
        "type_label": label,
        "content": "\n".join(content),
        "revoked": bool(row["revoked"]),
        "voice": {
            "url": f"/wechat/api/v1/accounts/{row['account_id']}/conversations/{row['conversation_id']}/history/{row['id']}/voice",
            "duration_ms": duration,
            "transcript": row.get("transcript") or "",
        }
        if msg_type == "34"
        else None,
        "image": {
            "url": f"/wechat/api/v1/accounts/{row['account_id']}/conversations/{row['conversation_id']}/history/{row['id']}/image",
            "width": frame["width"],
            "height": frame["height"],
        }
        if frame
        else None,
    }


def encode_cursor(aid, cid, q, watermark, row):
    value = [aid, cid, q, watermark, row["created_at"], row["id"]]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode_cursor(value, aid, cid, q):
    try:
        account, conversation, query, watermark, timestamp, mid = json.loads(
            base64.b64decode(value, altchars=b"-_", validate=True)
        )
        if (
            [account, conversation, query] != [aid, cid, q]
            or type(watermark) is not int
            or watermark < 0
            or watermark > 2**63 - 1
            or type(mid) is not int
            or not 0 < mid <= watermark
            or type(timestamp) not in (int, float)
            or not math.isfinite(timestamp)
        ):
            raise ValueError()
        return watermark, timestamp, mid
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("聊天记录游标无效") from None


def history(manager, aid, cid, q="", cursor=None, after_id=None, limit=50):
    q = q.strip()
    if cursor and after_id is not None:
        raise ValueError("聊天记录查询参数不正确")
    with manager.store.connect(snapshot=True) as conn:
        conv = conversation(conn, aid, cid)
        deletion = conn.execute(
            "SELECT id,state,error,message_count FROM chat_history_deletions WHERE account_id=%s AND conversation_id=%s ORDER BY created_at DESC LIMIT 1",
            (aid, cid),
        ).fetchone()
        history_revision = deletion["id"] if deletion else ""
        clearing = dict(deletion) if deletion and deletion["state"] != "complete" else None
        if clearing:
            return {
                "items": [],
                "has_more": False,
                "cursor": None,
                "watermark": "0",
                "history_revision": history_revision,
                "clearing": clearing,
            }
        watermark = conn.execute(
            "SELECT coalesce(max(id),0) FROM message_records WHERE account_id=%s AND conversation_id=%s",
            (aid, cid),
        ).fetchone()[0]
        position = None
        if cursor:
            watermark, timestamp, mid = decode_cursor(cursor, aid, cid, q)
            position = (timestamp, mid)
        if after_id is not None and after_id > watermark:
            if not deletion:
                raise ValueError("聊天记录游标无效")
            after_id = None
        params = [conv["wxid"], conv["peer_id"], aid, cid]
        clause = " AND m.id<=%s"
        params.append(watermark)
        ascending = after_id is not None
        if ascending:
            clause += " AND m.id>%s"
            params.append(after_id)
        if position:
            clause += " AND (m.created_at,m.id)<(%s,%s)"
            params.extend(position)
        items, scanned, last, more = [], 0, None, False
        # 搜索在有界批次内复用同一展示投影，避免 XML 标签或下载密钥成为命中内容。
        # 未扫描完用游标继续，不能把扫描额度当作“没有匹配结果”。
        while scanned < SCAN_LIMIT and len(items) < limit:
            batch_size = min(250 if q else limit, SCAN_LIMIT - scanned)
            order = "m.id ASC" if ascending else "m.created_at DESC,m.id DESC"
            rows = conn.execute(
                SELECT + SOURCE + clause + f" ORDER BY {order} LIMIT %s", [*params, batch_size + 1]
            ).fetchall()
            more = len(rows) > batch_size
            for row in rows[:batch_size]:
                last = row
                scanned += 1
                item = presentation(row, conv)
                searchable = item["content"] + "\n" + item["type_label"]
                if not q or q.casefold() in searchable.casefold():
                    items.append(item)
                if len(items) == limit:
                    more = more or row is not rows[min(batch_size, len(rows)) - 1]
                    break
            if not more or not last:
                break
            # 下一批使用独占游标，时间相同、过滤类型或搜索不命中均不会造成重复或漏页。
            if ascending:
                clause = " AND m.id<=%s AND m.id>%s"
                params = [conv["wxid"], conv["peer_id"], aid, cid, watermark, last["id"]]
            else:
                clause = " AND m.id<=%s AND (m.created_at,m.id)<(%s,%s)"
                params = [conv["wxid"], conv["peer_id"], aid, cid, watermark, last["created_at"], last["id"]]
        items.sort(key=lambda value: (value["timestamp"], int(value["id"])))
        return {
            "history_revision": history_revision,
            "clearing": None,
            "items": items,
            "has_more": more,
            "cursor": encode_cursor(aid, cid, q, watermark, last)
            if more and last and not ascending
            else None,
            "watermark": str(last["id"] if more and last and ascending else watermark),
        }
