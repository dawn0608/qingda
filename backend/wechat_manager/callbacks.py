"""可信回调的业务可用性、撤回和联系人资料归并。"""

import json
import re

from .protocol import normalize, safe_xml
from .store import load_event, string


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def message_key(mid):
    return f"m:{int(mid):020d}"


def platform_id(value):
    value = str(value or "")
    return value if re.fullmatch(r"[0-9]{1,80}", value) and int(value) else ""


def parse_state(event):
    n = normalize(event)
    data = event.get("Data") or {}
    body = string(data.get("Content")) or ""
    if n["kind"] == "group" and ":\n" in body:
        body = body.split(":\n", 1)[1]
    root = safe_xml(body)
    visible = n["msg_type"] != "51"
    effect = None
    if n["msg_type"] in ("10000", "10002") and root is not None:
        subtype = root.get("type", "")
        if subtype == "revokemsg":
            target = platform_id(root.findtext(".//revokemsg/newmsgid")) or platform_id(
                root.findtext(".//revokemsg/msgid")
            )
            if target and root.findtext(".//revokemsg/session") == n["peer"]:
                effect = {"kind": "revoke", "target_id": target}
            visible = False
        elif subtype in (
            "functionmsg",
            "dynacfg",
            "gog_wcs_plugin_config",
            "sns",
            "SnsSync",
            "ClientCheckConsistency",
            "config",
        ) or subtype.lower().startswith(("sns", "function", "config")):
            visible = False
    friend = None
    if n["metadata"].get("friend_greeting"):
        friend = {"peer_id": n["peer"]}
    elif n["msg_type"] == "37":
        visible = False
    return {"visible": visible, "effect": effect, "friend": friend}


def put(conn, key, aid, cid, *, visible=True, revoked=False, source_id=None, event_id=None):
    old = conn.execute("SELECT * FROM message_views WHERE key=%s", (key,)).fetchone()
    values = (aid, cid, int(visible), int(revoked), source_id, event_id)
    if (
        old
        and tuple(
            old[k] for k in ("account_id", "conversation_id", "visible", "revoked", "source_id", "event_id")
        )
        == values
    ):
        return False
    revision = conn.execute("SELECT coalesce(max(revision),0)+1 FROM message_views").fetchone()[0]
    conn.execute(
        "INSERT INTO message_views VALUES(%s,%s,%s,%s,%s,'{}','','',%s,%s,%s) ON CONFLICT(user_id,key) DO UPDATE SET visible=excluded.visible,revoked=excluded.revoked,presentation='{}',summary='',search_text='',source_id=excluded.source_id,event_id=excluded.event_id,revision=excluded.revision",
        (key, *values[:4], source_id, event_id, revision),
    )
    return True


def trusted(conn, account, event_row):
    return bool(
        event_row["origin"] == "live"
        and account["wxid"]
        and event_row["wxid"] == account["wxid"]
        and conn.execute(
            "SELECT 1 FROM terminals WHERE app_id=%s AND account_id=%s AND verified=1",
            (event_row["app_id"], account["id"]),
        ).fetchone()
    )


def project(conn, account, event_row, event, message):
    if not trusted(conn, account, event_row):
        conn.execute(
            "INSERT INTO callback_processing VALUES(%s,'waiting_identity') ON CONFLICT DO NOTHING",
            (event_row["id"],),
        )
        return
    conn.execute(
        "INSERT INTO callback_processing VALUES(%s,'projected') ON CONFLICT (user_id,event_id) DO UPDATE SET state=excluded.state",
        (event_row["id"],),
    )
    aid, cid = account["id"], message["conversation_id"]
    key = message_key(message["id"])
    value = parse_state(event)
    old = conn.execute("SELECT * FROM message_views WHERE key=%s", (key,)).fetchone()
    if not old or not old["revoked"]:
        put(conn, key, aid, cid, visible=value["visible"], source_id=message["id"], event_id=event_row["id"])
    ids = [
        message["platform_id"],
        (event.get("Data") or {}).get("NewMsgId"),
        (event.get("Data") or {}).get("MsgId"),
    ]
    for value_id in ids:
        mid = platform_id(value_id)
        if mid:
            conn.execute(
                "INSERT INTO message_aliases VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (aid, cid, mid, key)
            )
    if value["friend"]:
        f = value["friend"]
        known = conn.execute(
            "SELECT 1 FROM contact_entries e WHERE account_id=%s AND peer_id=%s AND kind='friend' AND (owner_wxid=%s OR (owner_wxid='' AND EXISTS(SELECT 1 FROM account_contacts s WHERE s.account_id=e.account_id AND s.wxid=%s AND s.list_updated_at IS NOT NULL)))",
            (aid, f["peer_id"], account["wxid"], account["wxid"]),
        ).fetchone()
        if not known:
            put(conn, key, aid, cid, visible=False, source_id=message["id"], event_id=event_row["id"])
    effect = value["effect"]
    if effect:
        conn.execute(
            "INSERT INTO message_effects VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (event_row["id"], aid, cid, effect["kind"], effect["target_id"], key, ""),
        )
    reconcile(conn, aid, cid)


def reconcile(conn, aid, cid):
    # 主动发送不会收到 AddMsg，必须同时识别本地发送回执。
    for row in conn.execute(
        "SELECT id,response_json FROM tasks WHERE account_id=%s AND conversation_id=%s AND state='accepted'",
        (aid, cid),
    ):
        response = json.loads(row["response_json"])
        mid = platform_id((response.get("data") or {}).get("newMsgId"))
        if mid:
            conn.execute(
                "INSERT INTO message_aliases VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,account_id,conversation_id,platform_id) DO UPDATE SET key=excluded.key",
                (aid, cid, mid, "t:" + row["id"]),
            )
    for effect in conn.execute(
        "SELECT * FROM message_effects WHERE account_id=%s AND conversation_id=%s ORDER BY CASE kind WHEN 'replace' THEN 0 ELSE 1 END,event_id",
        (aid, cid),
    ).fetchall():
        target = conn.execute(
            "SELECT key FROM message_aliases WHERE account_id=%s AND conversation_id=%s AND platform_id=%s",
            (aid, cid, effect["target_id"]),
        ).fetchone()
        if not target or target[0] == effect["source_key"]:
            continue
        key = target[0]
        if effect["kind"] != "revoke":
            continue
        put(conn, key, aid, cid, revoked=True, event_id=effect["event_id"])
        for duplicate in conn.execute(
            "SELECT id FROM messages WHERE account_id=%s AND conversation_id=%s AND platform_id=%s",
            (aid, cid, effect["target_id"]),
        ).fetchall():
            other = message_key(duplicate["id"])
            if other != key:
                put(conn, other, aid, cid, visible=False, revoked=True, event_id=effect["event_id"])


def revoked(conn, mid):
    row = conn.execute("SELECT revoked FROM message_views WHERE key=%s", (message_key(mid),)).fetchone()
    return bool(row and row[0])


def cancel_revoked_batches(conn, now):
    from .dialogue_windows import recover_pat, wake_revoked
    from .reply_queue import members

    conn.execute(
        "UPDATE dialogue_windows SET closed_reason='wake_revoked' WHERE closed_reason='' AND EXISTS(SELECT 1 FROM message_views v WHERE v.key=('m:' || lpad((source_mid)::text,20,'0')) AND v.revoked=1)"
    )

    for row in conn.execute(
        "SELECT * FROM tasks WHERE kind='auto' AND state IN ('queued','preparing','generating','ready')"
    ).fetchall():
        task = dict(row)
        batch = members(conn, task)
        used = conn.execute(
            "SELECT revision FROM reply_view_versions WHERE task_id=%s", (task["id"],)
        ).fetchone()
        stale = used and used[0] < revocation_revision(conn, task["conversation_id"])
        if not stale and not any(
            revoked(conn, item["message_id"]) or wake_revoked(conn, item["message_id"]) for item in batch
        ):
            continue
        # 已冻结批次整体作废；保留仍有效的成员原任务，按原顺序重新认领。
        conn.execute("DELETE FROM reply_batch_items WHERE batch_id=%s", (task["id"],))
        conn.execute("DELETE FROM reply_batches WHERE task_id=%s", (task["id"],))
        conn.execute("DELETE FROM reply_view_versions WHERE task_id=%s", (task["id"],))
        for item in batch:
            gone = revoked(conn, item["message_id"]) or wake_revoked(conn, item["message_id"])
            if gone:
                recover_pat(conn, item, now)
            conn.execute(
                "UPDATE tasks SET state=%s,reason=%s,content='',context_json='[]',lease_owner=NULL,updated_at=%s WHERE id=%s AND state IN ('queued','preparing','generating','ready','merged')",
                (
                    "canceled" if gone else "queued",
                    "message_revoked" if gone else "batch_rebuilt",
                    now,
                    item["id"],
                ),
            )


def revocation_revision(conn, cid):
    return conn.execute(
        "SELECT coalesce(max(revision),0) FROM message_views WHERE conversation_id=%s AND revoked=1", (cid,)
    ).fetchone()[0]


def contact_profile(conn, account, data, now):
    """仅更新回调实际提供的展示字段；关系仍由既有核实队列判定。"""
    from .contact_profiles import merge_profile, profile_value

    peer = string(data.get("UserName"))
    fields = {
        "NickName": "nickName",
        "Remark": "remark",
        "Alias": "alias",
        "QuanPin": "quanPin",
        "PyInitial": "pyInitial",
        "RemarkQuanPin": "remarkQuanPin",
        "RemarkPyinitial": "remarkPyInitial",
        "SmallHeadImgUrl": "smallHeadImgUrl",
        "BigHeadImgUrl": "bigHeadImgUrl",
        "Signature": "signature",
    }
    addition = profile_value(
        {
            value: string(data[key])
            for key, value in fields.items()
            if key in data and string(data[key]) is not None
        }
    )
    if not addition:
        return
    aid = account["id"]
    previous = conn.execute(
        "SELECT profile_json FROM contact_entries WHERE account_id=%s AND peer_id=%s", (aid, peer)
    ).fetchone()
    if previous:
        profile = merge_profile(json.loads(previous[0]), addition)
        name = profile.get("remark") or profile.get("nickName") or peer
        order = (profile.get("remarkQuanPin") if profile.get("remark") else profile.get("quanPin")) or name
        first = order[:1].upper()
        conn.execute(
            "UPDATE contact_entries SET profile_json=%s,display_name=%s,sort_key=%s,group_key=%s,profile_state='ready',profile_updated_at=%s WHERE account_id=%s AND peer_id=%s",
            (
                dumps(profile),
                name,
                order.casefold(),
                first if first.isascii() and first.isalpha() else "#",
                now,
                aid,
                peer,
            ),
        )
    previous = conn.execute(
        "SELECT profile_json FROM chat_profiles WHERE account_id=%s AND peer_id=%s", (aid, peer)
    ).fetchone()
    profile = merge_profile(json.loads(previous[0]) if previous else {}, addition)
    conn.execute(
        "INSERT INTO chat_profiles(account_id,peer_id,profile_json,updated_at,state) VALUES(%s,%s,%s,%s,'ready') ON CONFLICT(user_id,account_id,peer_id) DO UPDATE SET profile_json=excluded.profile_json,updated_at=excluded.updated_at,state='ready'",
        (aid, peer, dumps(profile), now),
    )


def refresh_greetings(conn, aid=None, peer=None):
    # 关系确认后重建隐藏的打招呼投影；原始消息和事件始终保留。
    rows = conn.execute(
        "SELECT m.id,m.event_id FROM message_views v JOIN messages m ON m.id=v.source_id JOIN conversations c ON c.id=m.conversation_id JOIN accounts a ON a.id=m.account_id JOIN contact_entries e ON e.account_id=m.account_id AND e.peer_id=c.peer_id WHERE v.visible=0 AND e.kind='friend' AND (((m.metadata #>> '{friend_greeting}'))::boolean)=true AND (e.owner_wxid=a.wxid OR (e.owner_wxid='' AND EXISTS(SELECT 1 FROM account_contacts s WHERE s.account_id=a.id AND s.wxid=a.wxid AND s.list_updated_at IS NOT NULL))) AND (%s::bigint IS NULL OR m.account_id=%s) AND (%s::text IS NULL OR c.peer_id=%s)",
        (aid, aid, peer, peer),
    ).fetchall()
    for row in rows:
        message = conn.execute("SELECT * FROM messages WHERE id=%s", (row["id"],)).fetchone()
        event = conn.execute("SELECT * FROM events WHERE id=%s", (row["event_id"],)).fetchone()
        account = conn.execute("SELECT * FROM accounts WHERE id=%s", (message["account_id"],)).fetchone()
        project(conn, account, event, load_event(conn, event), message)


def process_versions(manager, limit=50):
    for row in manager.store.query(
        "SELECT p.event_id FROM callback_processing p JOIN events e ON e.id=p.event_id JOIN terminals t ON t.app_id=e.app_id WHERE p.state='waiting_identity' AND t.verified=1 LIMIT %s",
        (limit,),
    ):
        with manager.store.transaction() as conn:
            event = conn.execute("SELECT * FROM events WHERE id=%s", (row["event_id"],)).fetchone()
            message = conn.execute("SELECT * FROM messages WHERE event_id=%s", (row["event_id"],)).fetchone()
            if message:
                project(
                    conn,
                    manager.account(message["account_id"], conn),
                    event,
                    load_event(conn, event),
                    message,
                )
    for item in manager.store.query(
        "SELECT id FROM callback_versions WHERE processed=0 ORDER BY id LIMIT %s", (limit,)
    ):
        with manager.store.transaction() as conn:
            version = conn.execute(
                "SELECT * FROM callback_versions WHERE id=%s AND processed=0", (item["id"],)
            ).fetchone()
            if not version:
                continue
            message = conn.execute(
                "SELECT * FROM messages WHERE event_id=%s", (version["event_id"],)
            ).fetchone()
            if not message:
                conn.execute(
                    "UPDATE callback_versions SET processed=2 WHERE id=%s AND EXISTS(SELECT 1 FROM events WHERE id=%s AND processed=1)",
                    (version["id"], version["event_id"]),
                )
                continue
            event_row = dict(
                conn.execute("SELECT * FROM events WHERE id=%s", (version["event_id"],)).fetchone()
            )
            event = json.loads(version["payload"])
            base_event = load_event(conn, event_row)
            if event.get("Data", {}).pop("_content_event_id", None) == event_row["id"]:
                event["Data"]["Content"] = base_event.get("Data", {}).get("Content", "")
            # 只接受同身份、同会话的语义版本；不可用同 ID 修改归属。
            original = normalize(base_event)
            revised = normalize(event)
            valid = (
                all(original[k] == revised[k] for k in ("me", "peer", "sender", "recipient"))
                and event.get("Appid") == event_row["app_id"]
                and event.get("TypeName") == "AddMsg"
            )
            # 微信有明确的文件完成覆盖语义；其它同 ID 改文报文留证待支持，不覆盖聊天正文或 AI 输入。
            valid = (
                valid
                and original["msg_type"] == revised["msg_type"] == "49"
                and original["metadata"].get("appmsg_type") in ("6", "74")
                and revised["metadata"].get("appmsg_type") == "6"
            )
            if valid:
                project(conn, manager.account(message["account_id"], conn), event_row, event, message)
            conn.execute(
                "UPDATE callback_versions SET processed=%s WHERE id=%s", (1 if valid else 2, version["id"])
            )
