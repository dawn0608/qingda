import math
import re
import xml.etree.ElementTree as ET

from .store import string

TYPE_NAMES = {
    "1": "文本",
    "3": "图片",
    "34": "语音",
    "37": "好友请求",
    "42": "名片",
    "43": "视频",
    "62": "短视频",
    "47": "表情",
    "48": "位置",
    "49": "应用消息",
    "51": "同步通知",
    "10000": "系统消息",
    "10002": "系统通知",
}


def safe_xml(value):
    if (
        not isinstance(value, str)
        or len(value) > 1024 * 1024
        or re.search(r"<!DOCTYPE|<!ENTITY", value, re.I)
    ):
        return None
    try:
        return ET.fromstring(value)
    except ET.ParseError:
        return None


def normalize(event):
    data = event.get("Data") or {}
    if not isinstance(data, dict):
        data = {}
    me = string(event.get("Wxid"))
    sender = string(data.get("FromUserName")) or ""
    recipient = string(data.get("ToUserName")) or ""
    body = string(data.get("Content")) or ""
    peer = next((v for v in (sender, recipient) if v.endswith("@chatroom")), None)
    group = peer is not None
    if group and ":\n" in body:
        prefix, body = body.split(":\n", 1)
        if prefix and len(prefix) < 200:
            sender = prefix
    outgoing = sender == me and bool(me)
    if not group:
        peer = recipient if outgoing else sender
    mentions = []
    root = safe_xml(string(data.get("MsgSource")))
    if root is not None:
        for node in root.iter():
            if node.tag.lower().split("}")[-1] == "atuserlist" and node.text:
                mentions.extend(x.strip() for x in node.text.split(",") if x.strip())
    created = data.get("CreateTime")
    if (
        isinstance(created, bool)
        or not isinstance(created, (int, float))
        or not math.isfinite(created)
        or not 0 < created < 32503680000
    ):
        created = None
    msg_type = str(data.get("MsgType", ""))
    metadata = {
        "parse_status": "known_type" if msg_type in TYPE_NAMES else "unknown_type",
        "type_name": TYPE_NAMES.get(msg_type, "未知类型"),
        "message_kind": {
            "1": "text",
            "3": "image",
            "34": "voice",
            "43": "video",
            "62": "video",
            "47": "emoji",
        }.get(msg_type, "other"),
        "mentioned": bool(me and me in mentions),
        "mentions": mentions,
        "history": any(
            bool(data.get(k)) for k in ("IsHistory", "isHistory", "is_history", "History", "IsSync")
        ),
        "test": bool(data.get("IsTest")) or body == "本条消息是来自wechatapi.net的测试消息",
    }
    reply_text = ""
    # 好友验证由 fmessage 转发，真实联系人和打招呼正文在 XML 属性中。
    # 保留 37 类型及 other 分类，展示问候不代表可触发自动回复。
    if msg_type == "37" and me and recipient == me and not group:
        request = safe_xml(body)
        if request is not None and request.tag == "msg" and request.get("opcode") == "2":
            applicant = request.get("fromusername", "")
            greeting = request.get("content", "")
            if (
                re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", applicant)
                and applicant not in (me, "fmessage")
                and sender in ("fmessage", applicant)
                and greeting.strip()
            ):
                peer = sender = applicant
                reply_text = greeting
                metadata.update(friend_greeting=True, type_name="打招呼")
    if msg_type != "1":
        root = safe_xml(body)
        if root is not None:
            for tag, fields in (
                ("voicemsg", {"voicelength": "duration_ms"}),
                ("emoji", {"md5": "emoji_md5", "len": "emoji_size"}),
            ):
                node = root if root.tag == tag else root.find(".//" + tag)
                if node is not None:
                    for attribute, name in fields.items():
                        value = node.get(attribute, "")
                        if name == "emoji_md5" and re.fullmatch(r"[a-fA-F0-9]{32}", value):
                            metadata[name] = value.lower()
                        elif name != "emoji_md5" and value.isdigit() and len(value) <= 10:
                            metadata[name] = int(value)
            for tag in ("title", "des", "totallen", "fileext", "playlength", "length", "appmsg/type"):
                node = root.find(".//" + tag)
                if node is not None and node.text:
                    metadata[tag] = node.text[:500]
            appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
            if appmsg is not None:
                metadata["appmsg_type"] = appmsg.findtext("type", "")[:20]
                if metadata["appmsg_type"] == "6":
                    metadata.update(message_kind="file", type_name="文件")
            if appmsg is not None and appmsg.findtext("type") == "57":
                reference = appmsg.findtext("refermsg/svrid") or ""
                if reference.isdigit() and 1 <= len(reference) <= 80:
                    metadata["reference_id"] = reference
                    reply_text = appmsg.findtext("title") or ""
                    if reply_text.strip():
                        metadata.update(message_kind="text", type_name="引用文本")
    return {
        "me": me,
        "peer": peer,
        "sender": sender,
        "recipient": recipient,
        "outgoing": outgoing,
        "kind": "group" if group else "private",
        "text": body if msg_type == "1" else reply_text,
        "created_at": created,
        "msg_type": msg_type,
        "metadata": metadata,
    }


def redact(value, key=""):
    if isinstance(value, float) and not math.isfinite(value):
        return "[无效数值]"
    if re.search(
        r"token|secret|password|authorization|aes.?key|cdn.*(?:key|url)|proxy|phone|mobile", key, re.I
    ):
        return "[已脱敏]"
    if key.lower() in ("imgbuf", "buffer"):
        return "[二进制内容已省略]"
    if isinstance(value, dict):
        if re.search(r"wxid|app.?id|username|sender|recipient", key, re.I) and isinstance(
            value.get("string"), str
        ):
            return {k: redact(v, key if k == "string" else k) for k, v in value.items()}
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value[:100]]
    if isinstance(value, int) and ("msgid" in key.lower() or abs(value) > 9007199254740991):
        return str(value)
    if isinstance(value, str):
        if re.search(r"wxid|app.?id|username|sender|recipient", key, re.I):
            return value[:4] + "…" + value[-4:] if len(value) > 8 else "[已脱敏]"
        value = re.sub(
            r"(?i)([?&](?:token|key|secret|password|auth|sign)[^=]*=)[^&\s<\"]+", r"\1[已脱敏]", value
        )
        value = re.sub(
            r"(?is)<(aeskey|cdnkey|cdnthumbkey|cdnattachurl|cdnthumburl|cdnmidimgurl|cdnbigimgurl)>.*?</\1>",
            r"<\1>[已脱敏]</\1>",
            value,
        )
        value = re.sub(
            r"(?i)((?:aeskey|cdnkey|cdnthumbkey|cdn.*url)\s*=\s*[\"\']).*?[\"\']", r'\1[已脱敏]"', value
        )
        return value[:20000] + ("…[详情已省略，原文保留在服务器]" if len(value) > 20000 else "")
    return value
