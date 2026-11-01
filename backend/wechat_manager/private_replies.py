"""私聊与群聊成员收集轮次：等待不占执行槽，输入修订与发送副作用边界分开。"""

import asyncio
import json

from .database_async import database_call
from .providers import ProviderError
from .reply_policy import collection_delay

ACTIVE = "('queued','generating','preparing','ready')"
TABLES = {"private": "private_reply_rounds", "group": "group_reply_rounds"}


def round_table(conn, task):
    row = conn.execute("SELECT kind FROM conversations WHERE id=%s", (task["conversation_id"],)).fetchone()
    return TABLES[row[0]]


def revision_key(table):
    return "_group_revision" if table == TABLES["group"] else "_private_revision"


def change_reason(task):
    return "group_input_changed" if "_group_revision" in task else "private_input_changed"


def register(conn, task, now):
    if task["kind"] != "auto" or task["state"] != "queued":
        return
    current = conn.execute("SELECT state FROM tasks WHERE id=%s", (task["id"],)).fetchone()
    if not current or current[0] != "queued":
        return
    msg = conn.execute(
        "SELECT m.*,c.kind FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.id=%s",
        (task["message_id"],),
    ).fetchone()
    if not msg or msg["kind"] not in TABLES:
        return
    group = msg["kind"] == "group"
    table = TABLES[msg["kind"]]
    if group and (not msg["sender"] or msg["sender"].endswith("@chatroom")):
        return
    kind = json.loads(msg["metadata"]).get("message_kind")
    if kind not in ("text", "image", "voice", "pat"):
        return
    if conn.execute("SELECT 1 FROM reply_batch_items WHERE task_id=%s", (task["id"],)).fetchone():
        return
    parent = conn.execute(
        f"SELECT t.id,r.first_at FROM tasks t JOIN {table} r ON r.task_id=t.id JOIN messages original ON original.id=t.message_id JOIN reply_batches b ON b.task_id=t.id WHERE t.account_id=%s AND t.conversation_id=%s AND t.kind='auto' AND t.state IN "
        + ACTIVE
        + " AND r.closed=0 AND t.id!=%s AND t.account_epoch=%s AND t.conversation_epoch=%s AND t.config_version=%s AND original.sender=%s ORDER BY t.created_at,t.ordinal LIMIT 1",
        (
            task["account_id"],
            task["conversation_id"],
            task["id"],
            task["account_epoch"],
            task["conversation_epoch"],
            task["config_version"],
            msg["sender"],
        ),
    ).fetchone()
    tid, first = (parent["id"], parent["first_at"]) if parent else (task["id"], msg["received_at"])
    delay = collection_delay(kind, group)
    due = min(first + (10 if group else 30), msg["received_at"] + delay)
    if not parent:
        conn.execute("INSERT INTO reply_batches VALUES(%s,%s) ON CONFLICT DO NOTHING", (tid, now))
        conn.execute(
            f"INSERT INTO {table} VALUES(%s,%s,%s,1,0,0) ON CONFLICT(user_id,task_id) DO UPDATE SET first_at=excluded.first_at,collect_until=excluded.collect_until,revision=excluded.revision,generation_revision=excluded.generation_revision,closed=excluded.closed",
            (tid, first, due),
        )
    else:
        conn.execute(f"UPDATE {table} SET collect_until=%s,revision=revision+1 WHERE task_id=%s", (due, tid))
    position = conn.execute(
        "SELECT coalesce(max(position),-1)+1 FROM reply_batch_items WHERE batch_id=%s", (tid,)
    ).fetchone()[0]
    conn.execute("INSERT INTO reply_batch_items VALUES(%s,%s,%s)", (task["id"], tid, position))
    if parent:
        conn.execute(
            "UPDATE tasks SET state='merged',reason='batch_joined',updated_at=%s WHERE id=%s",
            (now, task["id"]),
        )


def synchronize(conn, now):
    # 撤回会由原有机制拆开批次；将剩余有效原任务重新组织，不创建第二条消息任务。
    for table in TABLES.values():
        conn.execute(
            f"DELETE FROM {table} WHERE NOT EXISTS(SELECT 1 FROM reply_batches b WHERE b.task_id={table}.task_id)"
        )
    for row in conn.execute(
        "SELECT t.* FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE c.kind IN ('private','group') AND t.kind='auto' AND t.state='queued' AND NOT EXISTS(SELECT 1 FROM reply_batch_items b WHERE b.task_id=t.id) ORDER BY t.created_at,t.ordinal"
    ).fetchall():
        register(conn, dict(row), now)


def waiting(conn, task, now):
    row = conn.execute(
        f"SELECT collect_until FROM {round_table(conn, task)} WHERE task_id=%s", (task["id"],)
    ).fetchone()
    return bool(row and row[0] > now)


def claim(conn, task):
    # 重新领取会重建素材快照；旧尝试的撤回依赖不能阻止下一次整理输入。
    conn.execute("DELETE FROM reply_media_dependencies WHERE task_id=%s", (task["id"],))
    table = round_table(conn, task)
    row = conn.execute(f"SELECT revision FROM {table} WHERE task_id=%s", (task["id"],)).fetchone()
    if row:
        task[revision_key(table)] = row[0]
        conn.execute(f"UPDATE {table} SET generation_revision=revision WHERE task_id=%s", (task["id"],))
    return task


def changed(conn, task, owner):
    if "_private_revision" not in task and "_group_revision" not in task:
        return False
    table = round_table(conn, task)
    row = conn.execute(
        f"SELECT r.revision,t.lease_owner,t.state FROM {table} r JOIN tasks t ON t.id=r.task_id WHERE r.task_id=%s",
        (task["id"],),
    ).fetchone()
    return not row or row[0] != task[revision_key(table)] or row[1] != owner or row[2] == "queued"


def requeue(conn, task, now):
    conn.execute(
        "UPDATE tasks SET state='queued',reason=%s,content='',context_json='[]',lease_owner=NULL,updated_at=%s WHERE id=%s AND state IN ('generating','preparing','ready') AND lease_owner=%s",
        (change_reason(task), now, task["id"], task.get("_owner")),
    )


async def current_call(worker, task, call):
    """过时调用退出前仍占用原执行槽，避免取消期间并发放大。"""
    future = asyncio.ensure_future(call)
    try:
        while not future.done():
            await asyncio.wait({future}, timeout=0.1)
            await database_call(worker.manager.process_events, now=worker.clock())

            def _database_current_call_135():
                with worker.store.connect() as conn:
                    reason = worker.check(task, conn, quick=True)
                    current = conn.execute(
                        "SELECT state,lease_owner FROM tasks WHERE id=%s", (task["id"],)
                    ).fetchone()
                    if not reason and (
                        not current
                        or current["state"] != "generating"
                        or current["lease_owner"] != worker.owner
                    ):
                        reason = "task_canceled"
                return ("normal", None, locals())

            _database_result = await database_call(_database_current_call_135)
            if "reason" in _database_result[2]:
                reason = _database_result[2]["reason"]
            if reason:
                raise ProviderError(reason)
        return await future
    finally:
        if not future.done():
            future.cancel()
        await asyncio.gather(future, return_exceptions=True)


def split(conn, task, now):
    # 容量拆出的尾批有固定成员，不吞并后到消息。
    table = round_table(conn, task)
    conn.execute(f"UPDATE {table} SET closed=1 WHERE task_id=%s", (task["id"],))
    for row in conn.execute(
        f"SELECT b.task_id,min(m.received_at) AS first_at FROM reply_batches b JOIN tasks t ON t.id=b.task_id JOIN reply_batch_items i ON i.batch_id=b.task_id JOIN tasks member ON member.id=i.task_id JOIN messages m ON m.id=member.message_id WHERE t.account_id=%s AND t.conversation_id=%s AND t.state='queued' AND NOT EXISTS(SELECT 1 FROM {table} p WHERE p.task_id=b.task_id) GROUP BY b.task_id",
        (task["account_id"], task["conversation_id"]),
    ).fetchall():
        conn.execute(f"INSERT INTO {table} VALUES(%s,%s,%s,1,0,1)", (row["task_id"], row["first_at"], now))
