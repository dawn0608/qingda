"""数据库切换期间的持久回调缓冲；通过原路径校验，提交后才返回200。"""

import argparse
import hashlib
import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from psycopg_pool import ConnectionPool

from .config import Config
from .security import digest
from .store import compact_event


def run(port=17952):
    config = Config.from_env()
    pool = ConnectionPool(
        config.database_url,
        min_size=1,
        max_size=2,
        max_waiting=16,
        timeout=3,
        kwargs={"autocommit": True, "connect_timeout": 5},
        open=True,
    )
    pool.wait(timeout=10)

    def user_for(path):
        if not path.startswith("/wechatapi/callback/") or "/" in path[len("/wechatapi/callback/") :]:
            return None
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE callback_key_hash=%s", (digest(path.rsplit("/", 1)[-1]),)
            ).fetchone()
            return row[0] if row else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status):
            self.send_response(status)
            self.send_header("X-Callback-Receiver", "qingda-postgres-buffer")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self.reply(200 if user_for(self.path) else 404)

        do_HEAD = do_GET

        def do_POST(self):
            user_id = user_for(self.path)
            if user_id is None:
                return self.reply(404)
            if self.headers.get("Transfer-Encoding"):
                return self.reply(400)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.reply(400)
            if length < 0 or length > 16 * 1024 * 1024:
                return self.reply(413)
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.reply(400)
            if not raw:
                return self.reply(200)
            error = None
            try:
                event = json.loads(
                    raw.decode("utf-8-sig"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
                )
                if not isinstance(event, dict):
                    raise ValueError()
            except (ValueError, UnicodeError, RecursionError):
                event = {}
                error = "invalid_json"
            try:
                with pool.connection() as conn:
                    with conn.transaction():
                        conn.execute(
                            "INSERT INTO migration_inbox(received_at,body,raw_sha256,parse_error,user_id) VALUES(%s,%s,%s,%s,%s)",
                            (
                                time.time(),
                                json.dumps(compact_event(event), ensure_ascii=False),
                                hashlib.sha256(raw).hexdigest(),
                                error,
                                user_id,
                            ),
                        )
            except Exception:
                return self.reply(503)
            self.reply(200)

    server = ThreadingHTTPServer((config.web_bind_host, port), Handler)
    server.daemon_threads = False
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    finally:
        server.server_close()
        pool.close()


def replay(store):
    count = 0
    while True:
        with store.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM migration_inbox WHERE imported=0 ORDER BY received_at,id FOR UPDATE LIMIT 100"
            ).fetchall()
            if not rows:
                return count
            for row in rows:
                conn.execute("SELECT set_config('app.user_id',%s,true)", (str(row["user_id"]),))
                raw = b"{" if row["parse_error"] else row["body"].encode()
                store.receive(raw, now=row["received_at"], conn=conn, raw_digest=row["raw_sha256"])
                conn.execute("UPDATE migration_inbox SET imported=1 WHERE id=%s", (row["id"],))
                count += 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=17952)
    run(parser.parse_args().port)
