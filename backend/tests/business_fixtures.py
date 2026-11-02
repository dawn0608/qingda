"""存活业务的隔离测试辅助，不提供管理台接口。"""

import json
from datetime import date, datetime


def allow_reply(manager, cid, enabled, name=None):
    result = manager.set_auto_reply(cid, enabled)
    if name is not None:
        with manager.store.connect() as conn:
            conn.execute("UPDATE conversations SET name=%s WHERE id=%s", (name, cid))
    return result


def phone_takeover(manager, cid, content="", request_id=None):
    with manager.store.transaction() as conn:
        manager.takeover(cid, conn)


def contacts(manager, aid, kind="friend", q="", offset=0, limit=50):
    account = manager.account(aid)
    items = manager.store.query(
        "SELECT * FROM contact_entries WHERE account_id=%s AND kind=%s AND owner_wxid=%s ORDER BY sort_key,peer_id",
        (aid, kind, account["wxid"]),
    )
    for row in items:
        row["profile"] = json.loads(row["profile_json"])
        row["saved_to_contacts"] = bool(row["saved_to_contacts"])
    if q:
        items = [r for r in items if q in r["display_name"] or q in r["peer_id"]]
    total = len(items)
    return {
        "items": items[offset : offset + limit],
        "total": total,
        "joined_group_count": total,
        "friend_count": total if kind == "friend" else 0,
        "chatroom_count": sum(r["saved_to_contacts"] for r in items) if kind == "group" else 0,
    }


def queue_contact_check(http, cid):
    from qingda.contact_changes import enqueue_verification

    manager = http.app.state.manager
    with manager.store.transaction() as conn:
        conv = manager.conversation(cid, conn)
        return enqueue_verification(conn, conv["account_id"], conv["peer_id"], manager.clock())


def database_snapshot(conn):
    """完整表数据快照，用于断言只读或退役接口没有副作用。"""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=current_schema() ORDER BY tablename"
        )
    ]
    result = {}
    for table in tables:
        rows = [dict(row) for row in conn.execute('SELECT * FROM "' + table + '"')]
        result[table] = sorted(
            rows,
            key=lambda row: json.dumps(
                row,
                sort_keys=True,
                ensure_ascii=False,
                default=lambda value: (
                    value.isoformat() if isinstance(value, (date, datetime)) else bytes(value).hex()
                ),
            ),
        )
    return result
