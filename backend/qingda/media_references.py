"""聊天媒体定位信息；不预下载，不承诺平台资源永久有效。"""

import json

from .protocol import safe_xml
from .store import string


def remember(conn, message, event):
    data = event.get("Data") or {}
    body = string(data.get("Content")) or ""
    if ":\n" in body:
        body = body.split(":\n", 1)[1]
    root = safe_xml(body)
    metadata = json.loads(message["metadata"])
    kind = metadata.get("message_kind", "other")
    if str(data.get("MsgType")) not in ("3", "34", "43", "47", "49", "62"):
        return
    urls = []
    if root is not None:
        for node in root.iter():
            for key, value in node.attrib.items():
                if "url" in key.lower() and value:
                    urls.append({"field": key, "value": value})
            if "url" in node.tag.lower() and node.text:
                urls.append({"field": node.tag, "value": node.text})
    hashes = []
    lengths = []
    if root is not None:
        for node in root.iter():
            for key, value in node.attrib.items():
                if key.lower() in ("md5", "filemd5") and value:
                    hashes.append(value)
                if key.lower() in ("length", "len", "totallen", "size") and value.isdigit():
                    lengths.append(int(value))
            if node.tag.lower() in ("md5", "filemd5") and node.text:
                hashes.append(node.text)
    size = metadata.get("totallen") or metadata.get("emoji_size") or (lengths[0] if lengths else None)
    size = int(size) if str(size).isdigit() else None
    conn.execute(
        "INSERT INTO media_resources(message_id,kind,urls,app_id,msg_id,md5,file_name,size) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,message_id) DO UPDATE SET urls=excluded.urls,kind=excluded.kind,md5=excluded.md5,file_name=excluded.file_name,size=excluded.size",
        (
            message["id"],
            kind,
            json.dumps(urls, ensure_ascii=False),
            event.get("Appid"),
            str(data.get("MsgId") or ""),
            metadata.get("emoji_md5") or (hashes[0] if hashes else None),
            metadata.get("title"),
            size,
        ),
    )


def record_outbound(store, conn, task, response, state, now):
    from .history_identity import next_id

    platform = (response.get("data") or {}).get("newMsgId")
    platform = (
        str(platform)
        if isinstance(platform, (int, str)) and not isinstance(platform, bool) and str(platform).isdigit()
        else None
    )
    if conn.execute("SELECT 1 FROM message_records WHERE outbound_task_id=%s", (task["id"],)).fetchone():
        return
    account = conn.execute("SELECT wxid FROM accounts WHERE id=%s", (task["account_id"],)).fetchone()
    meta = {"message_kind": "text", "type_name": "文本", "send_state": state}
    existing = (
        conn.execute(
            "SELECT m.id,m.content_id,m.event_id,c.text FROM message_records m LEFT JOIN message_contents c ON c.id=m.content_id WHERE m.account_id=%s AND m.platform_id=%s",
            (task["account_id"], platform),
        ).fetchone()
        if platform
        else None
    )
    if existing:
        conn.execute(
            "UPDATE message_records SET outbound_task_id=%s,source=%s WHERE id=%s",
            (task["id"], task["kind"], existing["id"]),
        )
        if existing["content_id"] and existing["text"] == task["content"]:
            cid = existing["content_id"]
            conn.execute("UPDATE message_records SET content_id=NULL WHERE id=%s", (existing["id"],))
            if existing["event_id"]:
                conn.execute(
                    "UPDATE events SET content_id=NULL,payload=jsonb_set(payload,'{Data,_outbound_task_id}',to_jsonb(%s::text),true) WHERE id=%s",
                    (task["id"], existing["event_id"]),
                )
            conn.execute(
                "DELETE FROM message_contents WHERE id=%s AND NOT EXISTS(SELECT 1 FROM events WHERE content_id=%s) AND NOT EXISTS(SELECT 1 FROM message_records WHERE content_id=%s)",
                (cid, cid, cid),
            )
        return
    conn.execute(
        "INSERT INTO message_records(id,account_id,conversation_id,platform_id,direction,source,msg_type,sender,created_at,received_at,metadata,outbound_task_id) VALUES(%s,%s,%s,%s,'outbound',%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (
            next_id(conn, "messages"),
            task["account_id"],
            task["conversation_id"],
            platform,
            task["kind"],
            "49" if meta["message_kind"] != "text" else "1",
            account[0],
            now,
            now,
            json.dumps(meta, ensure_ascii=False),
            task["id"],
        ),
    )
