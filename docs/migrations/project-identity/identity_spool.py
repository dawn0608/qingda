"""一次性改名窗口的独立持久回调缓冲；不属于应用启动入口。"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from psycopg import sql

MAX_REQUEST = 16 * 1024 * 1024
MAX_SPOOL = 512 * 1024 * 1024
MIN_FREE = 1024 * 1024 * 1024
SCHEMA = "qingda_migration_audit"


class CapacityError(RuntimeError):
    pass


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append(directory, user_id, raw, *, now=None, limit=MAX_SPOOL, min_free=MIN_FREE):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir() or len(raw) > MAX_REQUEST:
        raise ValueError("unsafe_spool_or_request")
    record_id = uuid.uuid4().hex
    record = {
        "id": record_id,
        "user_id": int(user_id),
        "received_at": time.time() if now is None else now,
        "raw": base64.b64encode(raw).decode(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    data = json.dumps(record, separators=(",", ":")).encode()
    with (directory / ".append.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        used = sum(p.stat().st_size for p in directory.iterdir() if p.is_file())
        if used + len(data) > limit or shutil.disk_usage(directory).free - len(data) < min_free:
            raise CapacityError("spool_capacity")
        temporary = directory / ("." + record_id + ".tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / (record_id + ".json"))
            sync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)
    return record_id


def records(directory):
    for path in sorted(Path(directory).glob("*.json"), key=lambda p: (p.lstat().st_mtime_ns, p.name)):
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
            raise ValueError("unsafe_spool_record")
        record = json.loads(path.read_bytes())
        raw = base64.b64decode(record["raw"], validate=True)
        if record["id"] != path.stem or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("spool_checksum")
        if len(raw) > MAX_REQUEST or not isinstance(record["user_id"], int) or record["user_id"] < 1:
            raise ValueError("unsafe_spool_record")
        yield record


def prepare_receipts(conn, schema=SCHEMA):
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.callback_imports(batch text NOT NULL, record_id text NOT NULL, "
            "sha256 text NOT NULL, imported_at timestamptz NOT NULL DEFAULT now(), "
            "PRIMARY KEY(batch,record_id))"
        ).format(sql.Identifier(schema))
    )


def replay(directory, conn, batch, receive, *, schema=SCHEMA, progress=None):
    if not conn.autocommit:
        raise ValueError("replay_requires_autocommit_connection")
    count = 0
    for record in records(directory):
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(17950,1)")
            inserted = conn.execute(
                sql.SQL(
                    "INSERT INTO {}.callback_imports(batch,record_id,sha256) VALUES(%s,%s,%s) "
                    "ON CONFLICT DO NOTHING RETURNING record_id"
                ).format(sql.Identifier(schema)),
                (batch, record["id"], record["sha256"]),
            ).fetchone()
            if inserted:
                receive(conn, record)
            else:
                digest = conn.execute(
                    sql.SQL("SELECT sha256 FROM {}.callback_imports WHERE batch=%s AND record_id=%s").format(
                        sql.Identifier(schema)
                    ),
                    (batch, record["id"]),
                ).fetchone()[0]
                if digest != record["sha256"]:
                    raise ValueError("receipt_checksum_mismatch")
        if inserted:
            count += 1
        if progress:
            progress(record["id"])
    return count


def run(directory, allowlist, *, host="127.0.0.1", port=17952):
    allowed = json.loads(Path(allowlist).read_text())
    if not all(re.fullmatch(r"[0-9a-f]{64}", k) and isinstance(v, int) and v > 0 for k, v in allowed.items()):
        raise ValueError("invalid_callback_allowlist")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code):
            self.send_response(code)
            self.send_header("X-Callback-Receiver", "qingda-cutover-buffer")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def user(self):
            prefix = "/wechatapi/callback/"
            if not self.path.startswith(prefix):
                return None
            key = self.path[len(prefix) :]
            if not key or any(c in key for c in "/?#%"):
                return None
            return allowed.get(hashlib.sha256(key.encode()).hexdigest())

        def do_GET(self):
            self.reply(200 if self.user() else 404)

        do_HEAD = do_GET

        def do_POST(self):
            uid = self.user()
            if not uid:
                return self.reply(404)
            if self.headers.get("Transfer-Encoding"):
                return self.reply(400)
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400)
            if not 0 <= size <= MAX_REQUEST:
                return self.reply(413)
            self.connection.settimeout(15)
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    return self.reply(400)
                if raw:
                    append(directory, uid, raw)
            except (OSError, CapacityError):
                return self.reply(503)
            self.reply(200)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = False
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    finally:
        server.server_close()
        sync_directory(directory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("allowlist", type=Path)
    parser.add_argument("--port", type=int, default=17952)
    args = parser.parse_args()
    os.umask(0o077)
    run(args.directory, args.allowlist, port=args.port)
