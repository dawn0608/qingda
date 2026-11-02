"""PostgreSQL 原生持久化；短事务、每进程有界连接池。"""

import hashlib
import json
import os
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

from psycopg.adapt import Dumper
from psycopg.types.json import set_json_loads
from psycopg_pool import ConnectionPool

from .tenancy import current_user

SCHEMA_VERSION = 59


class Record(dict):
    def __iter__(self):
        return iter(self.values())

    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def record_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Record(zip(names, values))


class FlagDumper(Dumper):
    oid = 20

    def dump(self, value):
        return b"1" if value else b"0"


def configure(conn):
    conn.adapters.register_dumper(bool, FlagDumper)
    set_json_loads(lambda value: bytes(value).decode("utf-8"), conn)


def string(value):
    if isinstance(value, dict):
        value = value.get("string")
    return value if isinstance(value, str) else None


def task_record(row):
    return dict(row)


def message_identity(event):
    data = event.get("Data")
    if not isinstance(data, dict):
        data = {}
    value = data.get("NewMsgId")
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str))
        or not str(value).isdigit()
        or not 1 <= len(str(value)) <= 80
        or int(value) == 0
    ):
        value = None
    return data, str(value) if value is not None else None


def compact_event(event):
    """保留处理和下载必要字段，移除内嵌媒体及重复摘要。"""
    result = {key: event[key] for key in ("Appid", "Wxid", "TypeName") if key in event}
    data = event.get("Data")
    if isinstance(data, dict):
        flags = {"IsHistory", "isHistory", "is_history", "History", "IsSync", "IsTest"}
        fields = {
            "AddMsg": {
                "MsgId",
                "NewMsgId",
                "MsgSeq",
                "FromUserName",
                "ToUserName",
                "MsgType",
                "Content",
                "CreateTime",
                "MsgSource",
                "Status",
                "ImgStatus",
            },
            "ModContacts": {
                "UserName",
                "NickName",
                "Remark",
                "Alias",
                "QuanPin",
                "PyInitial",
                "RemarkQuanPin",
                "RemarkPyinitial",
                "SmallHeadImgUrl",
                "BigHeadImgUrl",
                "Signature",
                "Sex",
            },
            "DelContacts": {"UserName"},
        }.get(event.get("TypeName"), {"MsgId", "NewMsgId", "MsgType"})
        result["Data"] = {key: value for key, value in data.items() if key in fields | flags}
        if "Content" in result["Data"] and (body := string(result["Data"]["Content"])) is not None:
            result["Data"]["Content"] = body
    return result


def load_event(conn, row):
    result = json.loads(row["payload"])
    if row.get("content_id"):
        content = conn.execute(
            "SELECT text,xml FROM message_contents WHERE id=%s", (row["content_id"],)
        ).fetchone()
        if content:
            data = result.setdefault("Data", {})
            prefix = data.pop("_content_prefix", "")
            data["Content"] = content["xml"] or (prefix + content["text"])
    elif result.get("Data", {}).get("_outbound_task_id"):
        data = result["Data"]
        task = conn.execute(
            "SELECT content FROM tasks WHERE id=%s", (data.pop("_outbound_task_id"),)
        ).fetchone()
        prefix = data.pop("_content_prefix", "")
        data["Content"] = prefix + (task["content"] if task else "")
    return result


class Store:
    def __init__(self, url):
        if not isinstance(url, str) or not url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL 必须使用 PostgreSQL，不能回退到 SQLite")
        self.url = url
        self.pool = ConnectionPool(
            url,
            min_size=1,
            max_size=4,
            max_waiting=16,
            timeout=3,
            kwargs={
                "row_factory": record_factory,
                "autocommit": True,
                "connect_timeout": 5,
                "options": parse_qs(urlsplit(url).query).get("options", [""])[0]
                + " -c statement_timeout=10000 -c lock_timeout=3000",
            },
            configure=configure,
            open=False,
        )
        self.pool.open()
        self.pool.wait(timeout=10)
        try:
            with self.connect() as conn:
                if conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0] != SCHEMA_VERSION:
                    raise RuntimeError("数据库版本不匹配，请先执行 migrate-db")
        except Exception:
            self.close()
            raise

    def close(self):
        self.pool.close()

    @contextmanager
    def connect(self, *, snapshot=False):
        with self.pool.connection() as conn:
            with conn.transaction():
                if snapshot:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                conn.execute("SELECT set_config('app.user_id',%s,true)", (str(current_user.get() or ""),))
                yield conn

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(17950,1)")
            yield conn

    def receive(self, raw, origin="live", now=None, conn=None, raw_digest=None):
        if conn is None:
            with self.transaction() as connection:
                return self.receive(raw, origin, now, connection, raw_digest)
        now = time.time() if now is None else now
        error = None
        try:
            event = json.loads(
                raw.decode("utf-8-sig"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
            if not isinstance(event, dict):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            event, error = {}, "invalid_json"
        data, mid = message_identity(event)
        aid, digest = string(event.get("Appid")), raw_digest or hashlib.sha256(raw).hexdigest()
        from .history_identity import next_id, seen

        if origin == "live" and (
            seen(conn, "callback", aid, mid) if aid and mid else seen(conn, "raw", digest)
        ):
            return None
        compact = compact_event(event)
        existing = (
            conn.execute("SELECT * FROM events WHERE app_id=%s AND message_id=%s", (aid, mid)).fetchone()
            if aid and mid
            else conn.execute(
                "SELECT * FROM events WHERE raw_sha256=%s AND (app_id IS NULL OR parse_error='invalid_json' OR processed=0) ORDER BY id LIMIT 1",
                (digest,),
            ).fetchone()
        )
        if existing:
            conn.execute(
                "UPDATE events SET deliveries=deliveries+1,last_received_at=%s WHERE id=%s",
                (now, existing["id"]),
            )
            original = compact_event(load_event(conn, existing))
            if existing["raw_sha256"] != digest and compact != original:
                if compact.get("TypeName") == "AddMsg" and compact.get("Data", {}).get(
                    "Content"
                ) == original.get("Data", {}).get("Content"):
                    compact["Data"].pop("Content", None)
                    compact["Data"]["_content_event_id"] = existing["id"]
                conn.execute(
                    "INSERT INTO callback_versions(event_id,payload,raw_sha256,received_at) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (existing["id"], json.dumps(compact, ensure_ascii=False), digest, now),
                )
            return existing["id"]
        content_id = None
        if event.get("TypeName") == "AddMsg":
            from .protocol import normalize

            normalized = normalize(event)
            body = string(data.get("Content")) or ""
            sent = None
            if normalized["outgoing"] and normalized["msg_type"] == "1" and mid:
                sent = conn.execute(
                    "SELECT t.id,t.content FROM message_records m JOIN tasks t ON t.id=m.outbound_task_id JOIN accounts a ON a.id=m.account_id JOIN terminals x ON x.account_id=a.id WHERE x.app_id=%s AND x.verified=1 AND a.wxid=%s AND m.platform_id=%s",
                    (aid, event.get("Wxid"), mid),
                ).fetchone()
            if sent and sent["content"] == normalized["text"]:
                compact.setdefault("Data", {})["_outbound_task_id"] = sent["id"]
            else:
                content_id = conn.execute(
                    "INSERT INTO message_contents(text,xml) VALUES(%s,%s) RETURNING id",
                    (normalized["text"], "" if normalized["msg_type"] == "1" else body),
                ).fetchone()[0]
            compact.setdefault("Data", {}).pop("Content", None)
            if normalized["msg_type"] == "1" and normalized["kind"] == "group" and ":\n" in body:
                compact["Data"]["_content_prefix"] = body.split(":\n", 1)[0] + ":\n"
        eid = next_id(conn, "events")
        conn.execute(
            "INSERT INTO events(id,received_at,last_received_at,event_type,app_id,wxid,message_id,message_type,sender,recipient,payload,content_id,raw_sha256,origin,parse_error) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                eid,
                now,
                now,
                string(event.get("TypeName")),
                aid,
                string(event.get("Wxid")),
                mid,
                str(data.get("MsgType", "")),
                string(data.get("FromUserName")),
                string(data.get("ToUserName")),
                json.dumps(compact, ensure_ascii=False),
                content_id,
                digest,
                origin,
                error,
            ),
        )
        return eid

    def query(self, sql, params=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params or None).fetchall()]

    def events(self, limit=100, account_id=None):
        where = " WHERE app_id IN (SELECT app_id FROM terminals WHERE account_id=%s)" if account_id else ""
        params = (account_id, limit) if account_id else (limit,)
        return self.query(
            "SELECT id,received_at,last_received_at,deliveries,event_type,app_id,wxid,message_id,message_type,sender,recipient,origin,parse_error,processed,raw_sha256 FROM events"
            + where
            + " ORDER BY received_at DESC,id DESC LIMIT %s",
            params,
        )

    def event(self, event_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=%s", (event_id,)).fetchone()
            return load_event(conn, row)

    def setting(self, key, default=None, conn=None):
        if conn is None:
            with self.connect() as connection:
                return self.setting(key, default, connection)
        row = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value, conn=None):
        if conn is None:
            with self.connect() as connection:
                return self.set_setting(key, value, connection)
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value,version=settings.version+1",
            (key, json.dumps(value)),
        )

    def health(self):
        with self.connect() as conn:
            version = conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
            result = {
                "database_backend": "postgresql",
                "database_ready": version == SCHEMA_VERSION,
                "schema_version": version,
            }
            if os.getenv("APP_REVISION"):
                result["revision"] = os.environ["APP_REVISION"]
            return result

    def register_runtime(self, service, conn=None):
        if conn is None:
            with self.connect() as connection:
                return self.register_runtime(service, connection)
        conn.execute(
            "INSERT INTO runtime_versions VALUES(%s,%s,%s,%s) ON CONFLICT(service) DO UPDATE SET revision=excluded.revision,pid=excluded.pid,updated_at=excluded.updated_at",
            (service, os.getenv("APP_REVISION", ""), os.getpid(), time.time()),
        )

    def audit(self, action, account_id=None, detail=None, conn=None):
        if conn is None:
            with self.connect() as connection:
                return self.audit(action, account_id, detail, connection)
        conn.execute(
            "INSERT INTO audit(created_at,action,account_id,detail) VALUES(%s,%s,%s,%s)",
            (time.time(), action, account_id, json.dumps(detail or {}, ensure_ascii=False)),
        )
