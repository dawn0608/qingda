"""独立进程的旁路写入；仅隔离测试库，凭据只经环境传入。"""

import json
import os
import sys

from qingda.context_capture import Capture, Writer
from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import user_scope

if "test" not in os.environ["DATABASE_URL"].split("?")[0]:
    raise RuntimeError("only isolated test database")
with user_scope(1):
    store = Store(os.environ["DATABASE_URL"])
    task = dict(store.query("SELECT * FROM tasks WHERE id=%s", (os.environ["CAPTURE_TASK"],))[0])
    task["_owner"] = os.environ["CAPTURE_OWNER"]
    capture = Capture(Manager(store), task)
    capture.run_id = os.environ["CAPTURE_RUN"]
    Writer(store).write(
        (
            capture,
            "request",
            json.dumps(
                {
                    "run_id": capture.run_id,
                    "call_id": sys.argv[1],
                    "request": {"messages": [{"role": "user", "content": "shared original input"}]},
                }
            ),
        )
    )
    store.close()
print("recorded")
