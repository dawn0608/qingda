"""逐用户、逐会话的永久清空；短事务固定范围，执行排空后删除，文件在连接外续清。"""

import json
import re
import time
import uuid

from .chat_records import conversation, decoded
from .history_identity import identity
from .media_access import history_media_access
from .protocol import normalize
from .tenancy import current_user


def receipt(row):
    return {
        "id": row["id"],
        "state": row["state"],
        "error": row["error"],
        "message_count": row["message_count"],
    }


def status(manager, aid, cid, request_id=None):
    with manager.store.connect(snapshot=True) as conn:
        conversation(conn, aid, cid)
        row = conn.execute(
            "SELECT * FROM chat_history_deletions WHERE account_id=%s AND conversation_id=%s AND "
            + ("id=%s" if request_id else "state<>'complete'")
            + " ORDER BY created_at DESC LIMIT 1",
            (aid, cid, request_id) if request_id else (aid, cid),
        ).fetchone()
        return receipt(row) if row else None


def event_peer(payload, raw_xml="", raw_text=""):
    event = decoded(payload)
    if not isinstance(event, dict) or not isinstance(event.get("Data"), dict):
        return None
    if raw_xml or raw_text:
        data = event["Data"]
        data["Content"] = raw_xml or (data.get("_content_prefix", "") + raw_text)
    return normalize(event)["peer"]


def start(manager, aid, cid, request_id):
    now = time.time()
    with manager.store.transaction() as conn:
        conv = conversation(conn, aid, cid)
        previous = conn.execute("SELECT * FROM chat_history_deletions WHERE id=%s", (request_id,)).fetchone()
        if previous:
            if (previous["account_id"], previous["conversation_id"]) != (aid, cid):
                raise ValueError("清空请求与会话不一致")
            return receipt(previous)
        active = conn.execute(
            "SELECT * FROM chat_history_deletions WHERE account_id=%s AND conversation_id=%s AND state<>'complete'",
            (aid, cid),
        ).fetchone()
        if active:
            return receipt(active)
        if conn.execute(
            "SELECT 1 FROM tasks WHERE account_id=%s AND conversation_id=%s AND state='sending'", (aid, cid)
        ).fetchone():
            raise ValueError("当前会话正在发送，请稍后清空")
        messages = conn.execute(
            "SELECT id,event_id,content_id,platform_id FROM message_records WHERE account_id=%s AND conversation_id=%s",
            (aid, cid),
        ).fetchall()
        tasks = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM tasks WHERE account_id=%s AND conversation_id=%s", (aid, cid)
            )
        ]
        views = [
            r[0]
            for r in conn.execute(
                "SELECT key FROM message_views WHERE account_id=%s AND conversation_id=%s", (aid, cid)
            )
        ]
        aliases = [
            r[0]
            for r in conn.execute(
                "SELECT platform_id FROM message_aliases WHERE account_id=%s AND conversation_id=%s",
                (aid, cid),
            )
        ]
        linked_events = {r["event_id"] for r in messages if r["event_id"]}
        linked_events.update(
            r[0]
            for r in conn.execute(
                "SELECT event_id FROM message_views WHERE key=ANY(%s) AND event_id IS NOT NULL", (views,)
            )
        )
        events = []
        for row in conn.execute(
            "SELECT e.*,raw.xml,raw.text FROM events e LEFT JOIN message_contents raw ON raw.id=e.content_id WHERE e.id=ANY(%s) OR e.app_id IN (SELECT app_id FROM terminals WHERE account_id=%s)",
            (list(linked_events), aid),
        ):
            if (
                row["id"] in linked_events
                or event_peer(row["payload"], row["xml"] or "", row["text"] or "") == conv["peer_id"]
            ):
                events.append(row)
        inbox = [
            r
            for r in conn.execute(
                "SELECT id,body,raw_sha256 FROM migration_inbox WHERE user_id=%s AND body->>'Appid' IN (SELECT app_id FROM terminals WHERE account_id=%s)",
                (current_user.get(), aid),
            )
            if event_peer(r["body"]) == conv["peer_id"]
        ]
        dedup = [
            ("message", identity("message", aid, p))
            for p in {*aliases, *(r["platform_id"] for r in messages if r["platform_id"])}
        ]
        for row in events:
            dedup.append(("raw", identity("raw", row["raw_sha256"])))
            if row["app_id"] and row["message_id"]:
                dedup.append(("callback", identity("callback", row["app_id"], row["message_id"])))
                dedup.append(("message", identity("message", aid, row["message_id"])))
        for row in conn.execute(
            "SELECT raw_sha256 FROM callback_versions WHERE event_id=ANY(%s)", ([r["id"] for r in events],)
        ):
            dedup.append(("raw", identity("raw", row[0])))
        for row in inbox:
            event = decoded(row["body"])
            dedup.append(("raw", identity("raw", row["raw_sha256"])))
            platform = event.get("Data", {}).get("NewMsgId")
            if platform:
                dedup.extend(
                    (
                        ("callback", identity("callback", event["Appid"], str(platform))),
                        ("message", identity("message", aid, str(platform))),
                    )
                )
        if dedup:
            conn.cursor().executemany(
                "INSERT INTO history_dedup(scope,identity_hash) VALUES(%s,%s) ON CONFLICT DO NOTHING", dedup
            )
        manifest = dict(
            messages=[r["id"] for r in messages],
            tasks=tasks,
            views=views,
            aliases=aliases,
            events=[r["id"] for r in events],
            contents=list({r["content_id"] for r in [*messages, *events] if r["content_id"]}),
            inbox=[r["id"] for r in inbox],
        )
        # 既有全局业务事务锁同时挡住消息入库、任务领取和发送状态变更。
        conn.execute("UPDATE conversations SET epoch=epoch+1 WHERE id=%s", (cid,))
        conn.execute(
            "UPDATE tasks SET state='canceled',reason='chat_history_deleted',updated_at=%s WHERE id=ANY(%s) AND state IN ('queued','generating','preparing','ready','merged')",
            (manager.clock(), tasks),
        )
        conn.execute("UPDATE events SET processed=1 WHERE id=ANY(%s)", (manifest["events"],))
        conn.execute("DELETE FROM dialogue_windows WHERE account_id=%s AND conversation_id=%s", (aid, cid))
        conn.execute("DELETE FROM reply_input_waits WHERE account_id=%s AND conversation_id=%s", (aid, cid))
        row = conn.execute(
            "INSERT INTO chat_history_deletions(id,account_id,conversation_id,state,manifest,message_count,created_at) VALUES(%s,%s,%s,'waiting',%s,%s,%s) RETURNING *",
            (request_id, aid, cid, json.dumps(manifest), len(messages), now),
        ).fetchone()
        return receipt(row)


def delete_records(conn, job):
    scope = decoded(job["manifest"])
    mids, tids, eids = scope["messages"], scope["tasks"], scope["events"]
    # 其他会话的有效引用不删除；关联工作缓存只移除被清空消息的引用。
    for table, field in (
        ("media_understandings", "source_message_id"),
        ("reply_media_attempt_limits", "source_message_id"),
        ("reply_media_evidence_refs", "source_message_id"),
    ):
        conn.execute(f"DELETE FROM {table} WHERE task_id=ANY(%s) OR {field}=ANY(%s)", (tids, mids))
    conn.execute(
        "DELETE FROM reply_media_dependencies WHERE task_id=ANY(%s) OR source_message_id=ANY(%s) OR origin_message_id=ANY(%s)",
        (tids, mids, mids),
    )
    conn.execute(
        "DELETE FROM reply_input_links WHERE task_id=ANY(%s) OR message_id=ANY(%s) OR source_mid=ANY(%s)",
        (tids, mids, mids),
    )
    conn.execute("DELETE FROM reply_batch_items WHERE task_id=ANY(%s) OR batch_id=ANY(%s)", (tids, tids))
    for table in (
        "reply_batches",
        "reply_context_snapshots",
        "reply_decisions",
        "reply_execution_budgets",
        "reply_execution_events",
        "reply_generation_checkpoints",
        "reply_media_reads",
        "reply_not_before",
        "reply_resource_slots",
        "reply_view_versions",
        "private_reply_rounds",
        "group_reply_rounds",
    ):
        conn.execute(f"DELETE FROM {table} WHERE task_id=ANY(%s)", (tids,))
    conn.execute("DELETE FROM reply_profile_requests WHERE source_mid=ANY(%s)", (mids,))
    conn.execute(
        "DELETE FROM media_evidence_cache WHERE account_id=%s AND conversation_id=%s",
        (job["account_id"], job["conversation_id"]),
    )
    for table in ("incoming_media", "media_resources"):
        conn.execute(f"DELETE FROM {table} WHERE message_id=ANY(%s)", (mids,))
    conn.execute(
        "DELETE FROM message_aliases WHERE account_id=%s AND conversation_id=%s AND platform_id=ANY(%s)",
        (job["account_id"], job["conversation_id"], scope["aliases"]),
    )
    conn.execute("DELETE FROM message_views WHERE key=ANY(%s)", (scope["views"],))
    conn.execute("DELETE FROM message_effects WHERE event_id=ANY(%s)", (eids,))
    conn.execute("DELETE FROM message_records WHERE id=ANY(%s)", (mids,))
    conn.execute("DELETE FROM tasks WHERE id=ANY(%s)", (tids,))
    for table in ("contact_evidence", "contact_removals", "contact_verifications", "contact_discoveries"):
        conn.execute(f"UPDATE {table} SET event_id=NULL WHERE event_id=ANY(%s)", (eids,))
    for table in ("callback_versions", "callback_receipts", "callback_processing"):
        conn.execute(f"DELETE FROM {table} WHERE event_id=ANY(%s)", (eids,))
    conn.execute("DELETE FROM events WHERE id=ANY(%s)", (eids,))
    conn.execute(
        "DELETE FROM migration_inbox WHERE user_id=%s AND id=ANY(%s)", (current_user.get(), scope["inbox"])
    )
    conn.execute(
        "DELETE FROM message_contents c WHERE c.id=ANY(%s) AND NOT EXISTS(SELECT 1 FROM events e WHERE e.content_id=c.id) AND NOT EXISTS(SELECT 1 FROM message_records m WHERE m.content_id=c.id)",
        (scope["contents"],),
    )
    conn.execute(
        "DELETE FROM audit WHERE detail->>'conversation_id'=%s AND (account_id=%s OR account_id IS NULL)",
        (str(job["conversation_id"]), job["account_id"]),
    )


def claim(manager, request_id=None):
    now, owner = time.time(), uuid.uuid4().hex
    with manager.store.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM chat_history_deletions WHERE state<>'complete' AND next_at<=%s AND (owner IS NULL OR lease_until<%s)"
            + (" AND id=%s" if request_id else "")
            + " ORDER BY created_at LIMIT 1 FOR UPDATE",
            (now, now, request_id) if request_id else (now, now),
        ).fetchone()
        if not row:
            return None
        if row["state"] == "waiting":
            tids = decoded(row["manifest"])["tasks"]
            if conn.execute(
                "SELECT 1 FROM reply_resource_slots WHERE resource='active' AND task_id=ANY(%s) AND expires_at>%s",
                (tids, now),
            ).fetchone():
                conn.execute(
                    "UPDATE chat_history_deletions SET next_at=%s WHERE id=%s", (now + 0.5, row["id"])
                )
                return None
        return dict(
            conn.execute(
                "UPDATE chat_history_deletions SET owner=%s,lease_until=%s,error='' WHERE id=%s RETURNING *",
                (owner, now + 30, row["id"]),
            ).fetchone()
        )


def clean_files(manager, job):
    with history_media_access(manager.media.directory, job["conversation_id"], exclusive=True) as acquired:
        return remove_files(manager, job) if acquired else False


def remove_files(manager, job):
    mids = {str(mid) for mid in decoded(job["manifest"])["messages"]}
    directory = manager.media.directory
    pattern = re.compile(r"([1-9][0-9]*)(?:\.bin|-[a-f0-9]{64}\.png|\.[a-f0-9]{32}\.tmp)\Z")
    temporary = re.compile(r"(?:playback|voice|decode)-([1-9][0-9]*)-.+\Z")
    for index, path in enumerate(directory.iterdir()):
        if index % 50 == 0:
            with manager.store.connect() as conn:
                if not conn.execute(
                    "UPDATE chat_history_deletions SET lease_until=%s WHERE id=%s AND owner=%s AND lease_until>%s RETURNING id",
                    (time.time() + 30, job["id"], job["owner"], time.time()),
                ).fetchone():
                    return False
        match = pattern.fullmatch(path.name)
        nested = temporary.fullmatch(path.name)
        if match and match[1] in mids:
            path.unlink(missing_ok=True)
        elif nested and nested[1] in mids:
            if path.is_symlink():
                path.unlink(missing_ok=True)
            else:
                for child in path.iterdir():
                    # 解码目录只有文件；拒绝递归跟随异常目录或用户可控链接。
                    child.unlink(missing_ok=True)
                path.rmdir()
    return True


def advance(manager, request_id=None):
    job = None
    try:
        job = claim(manager, request_id)
        if not job:
            return
        if job["state"] == "waiting":
            with manager.store.transaction() as conn:
                owned = conn.execute(
                    "SELECT id FROM chat_history_deletions WHERE id=%s AND owner=%s AND lease_until>%s FOR UPDATE",
                    (job["id"], job["owner"], time.time()),
                ).fetchone()
                if not owned:
                    return
                delete_records(conn, job)
                conn.execute(
                    "UPDATE chat_history_deletions SET state='files' WHERE id=%s AND owner=%s",
                    (job["id"], job["owner"]),
                )
        if not clean_files(manager, job):
            with manager.store.connect() as conn:
                conn.execute(
                    "UPDATE chat_history_deletions SET owner=NULL,lease_until=NULL,next_at=%s WHERE id=%s AND owner=%s",
                    (time.time() + 0.2, job["id"], job["owner"]),
                )
            return
        with manager.store.connect() as conn:
            conn.execute(
                "UPDATE chat_history_deletions SET state='complete',manifest='{}',owner=NULL,lease_until=NULL,error='',completed_at=%s WHERE id=%s AND owner=%s AND lease_until>%s",
                (time.time(), job["id"], job["owner"], time.time()),
            )
    except Exception:
        if job:
            with manager.store.connect() as conn:
                conn.execute(
                    "UPDATE chat_history_deletions SET owner=NULL,lease_until=NULL,next_at=%s,error='cleanup_failed' WHERE id=%s AND owner=%s",
                    (time.time() + 5, job["id"], job["owner"]),
                )
