"""自动回复会话选项；不读取聊天正文或发送历史。"""

import json

from .chat_visibility import visibility_sql
from .contact_changes import RELATIONSHIP_COLUMNS, decorate_monitor


def query_parts():
    profile = "coalesce(nullif(e.profile_json,'{}'),nullif(r.profile_json,'{}'),p.profile_json)"
    whitespace = "chr(9)||chr(10)||chr(11)||chr(12)||chr(13)||chr(32)||chr(160)||chr(12288)"
    name = (
        "CASE WHEN c.peer_id=a.wxid THEN coalesce(nullif(btrim((a.profile_json #>> '{nickName}')),''),nullif(btrim(a.nickname),''),nullif(c.name,''),c.peer_id) "
        "WHEN c.peer_id='filehelper' THEN '文件传输助手' WHEN c.peer_id='weixin' THEN '微信团队' "
        f"ELSE coalesce(nullif(btrim(({profile} #>> '{{remark}}'),{whitespace}),''),nullif(btrim(({profile} #>> '{{nickName}}'),{whitespace}),''),nullif(btrim(e.display_name),''),nullif(btrim(r.display_name),''),nullif(c.name,''),c.peer_id) END"
    )
    profile = (
        "CASE WHEN c.peer_id=a.wxid THEN a.profile_json WHEN c.peer_id='filehelper' THEN jsonb_build_object('nickName','文件传输助手') WHEN c.peer_id='weixin' THEN jsonb_build_object('nickName','微信团队') ELSE "
        + profile
        + " END"
    )
    source = """ FROM conversations c JOIN accounts a ON a.id=c.account_id
        LEFT JOIN contact_entries e ON e.account_id=c.account_id AND e.peer_id=c.peer_id
        LEFT JOIN contact_removals r ON r.account_id=c.account_id AND r.peer_id=c.peer_id
        LEFT JOIN chat_profiles p ON p.account_id=c.account_id AND p.peer_id=c.peer_id """
    return name, profile, source


def conversations(manager, aid=None, kind=None):
    store = manager.store
    name, profile, source = query_parts()
    eligible, identifiers = visibility_sql()
    where, args = [eligible], list(identifiers)
    for field, value in (("c.account_id", aid), ("c.kind", kind)):
        if value is not None:
            where.append(field + "=%s")
            args.append(value)
    rows = store.query(
        "SELECT c.*,a.remark AS account_remark,a.nickname AS account_nickname,"
        + RELATIONSHIP_COLUMNS
        + ","
        + name
        + " AS display_name,"
        + profile
        + " AS contact_profile"
        + source
        + ("WHERE " + " AND ".join(where) if where else ""),
        args,
    )
    with store.connect() as conn:
        for row in rows:
            row["profile"] = json.loads(row.pop("contact_profile") or "{}")
            decorate_monitor(conn, row, manager.clock())
    rows.sort(key=lambda row: (row["display_name"].casefold(), row["id"]))
    return rows


def reply_scope(manager, aid, kind="private", q="", status="all", page=1, page_size=10):
    """只查询当前页；计数与当页来自同一只读快照，不读取消息历史。"""
    now = manager.clock()
    name, profile, source = query_parts()
    eligible, identifiers = visibility_sql()
    where = [eligible, "c.account_id=%s", "c.kind=%s"]
    args = [*identifiers, aid, kind]
    q = q.strip()
    if q:
        pattern = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        expressions = [name, f"({profile} #>> '{{nickName}}')", f"({profile} #>> '{{remark}}')", "c.name"]
        where.append("(" + " OR ".join(f"{value} ILIKE %s ESCAPE '!'" for value in expressions) + ")")
        args.extend([pattern] * len(expressions))
    if status == "enabled":
        where.append("c.allowed=1")
    elif status == "disabled":
        where.append("c.allowed=0")
    elif status == "paused":
        where.extend(
            ["c.allowed=1", "c.pause_until>%s", "(r.removed_at IS NULL OR r.restored_at IS NOT NULL)"]
        )
        args.append(now)
    elif status == "removed":
        where.extend(["r.removed_at IS NOT NULL", "r.restored_at IS NULL"])
    clause = " WHERE " + " AND ".join(where)
    with manager.store.connect(snapshot=True) as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id=%s AND archived_at IS NULL", (aid,)).fetchone():
            raise ValueError("账号不存在")
        total = conn.execute("SELECT count(*)" + source + clause, args).fetchone()[0]
        page = min(page, max(1, (total + page_size - 1) // page_size))
        rows = conn.execute(
            "SELECT c.*,"
            + RELATIONSHIP_COLUMNS
            + ","
            + name
            + " AS display_name,"
            + profile
            + " AS contact_profile"
            + source
            + clause
            + " ORDER BY lower("
            + name
            + ') COLLATE "C",c.id LIMIT %s OFFSET %s',
            [*args, page_size, (page - 1) * page_size],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["profile"] = json.loads(item.pop("contact_profile") or "{}")
            items.append(decorate_monitor(conn, item, now))
    return {"items": items, "total": total, "page": page, "page_size": page_size, "server_time": now}
