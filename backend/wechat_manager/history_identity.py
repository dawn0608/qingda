"""历史消息去重与单调 ID 分配。"""

import hashlib
import json


def identity(scope, *parts):
    return hashlib.sha256(json.dumps([scope, *parts], separators=(",", ":")).encode()).hexdigest()


def seen(conn, scope, *parts):
    return bool(
        conn.execute(
            "SELECT 1 FROM history_dedup WHERE scope=%s AND identity_hash=%s",
            (scope, identity(scope, *parts)),
        ).fetchone()
    )


def remember(conn, scope, *parts):
    conn.execute(
        "INSERT INTO history_dedup VALUES(%s,%s) ON CONFLICT DO NOTHING", (scope, identity(scope, *parts))
    )


def next_id(conn, name):
    if name not in ("events", "messages"):
        raise ValueError("invalid_history_sequence")
    return conn.execute(
        "UPDATE history_id_watermarks SET value=value+1 WHERE name=%s RETURNING value", (name,)
    ).fetchone()[0]
