"""仅服务器隔离用：历史回查进程崩溃后的清单恢复与旧所有者隔离。"""

import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

from qingda import context_snapshot
from qingda.history_tool import HistoryReader
from qingda.media import IncomingMedia
from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import current_user

current_user.set(int(os.getenv("ACTOR_USER_ID", "1")))
store = Store(os.environ["ACTOR_DATABASE_URL"])
try:
    manager = Manager(store)
    task = dict(store.query("SELECT * FROM tasks WHERE id=%s", (os.environ["ACTOR_TASK_ID"],))[0])
    task["_owner"] = os.environ["ACTOR_OWNER"]
    if sys.argv[1] == "pool-check":
        manager.media = IncomingMedia(
            manager, SimpleNamespace(data_dir=Path(os.environ["ACTOR_MEDIA_ROOT"])), transcriber=object()
        )
        task["_context_frozen"] = True
        # 三个槽模拟等待事务锁的后台作业；当前检查不得再申请第五个连接。
        with ExitStack() as stack:
            for _ in range(3):
                stack.enter_context(store.connect())
            print(json.dumps({"ready": True}), flush=True)
            assert sys.stdin.readline().strip() == "run"
            for _ in range(5):
                with store.transaction() as conn:
                    assert context_snapshot.check(manager, conn, task) == ""
        assert store.pool.get_stats()["pool_available"] == store.pool.get_stats()["pool_size"]
        print(json.dumps({"ok": True}), flush=True)
        sys.exit(0)
    with store.connect() as conn:
        manifest = json.loads(
            conn.execute(
                "SELECT manifest FROM reply_context_snapshots WHERE task_id=%s", (task["id"],)
            ).fetchone()[0]
        )
        context = context_snapshot.hydrate(manager, conn, task, manifest)
    reader = HistoryReader(manager, task, context)
    if sys.argv[1] == "resume":

        def unexpected(_):
            raise AssertionError("恢复应复用已核验的结果清单，不重新检索")

        reader.query = unexpected
    result = reader.run({"query": "历史预算1元"}, "same-read")
    print(json.dumps({"ok": result["ok"], "first": result["data"]["rounds"][0][0]["content"]}), flush=True)
    if sys.argv[1] == "hold":
        time.sleep(60)
finally:
    store.close()
