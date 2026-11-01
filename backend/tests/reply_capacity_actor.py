"""独立进程共享容量验证；不调用任何外部模型或发送接口。"""

import asyncio
import os
import signal
import sys
from types import SimpleNamespace

from wechat_manager.reply_execution import Execution
from wechat_manager.store import Store
from wechat_manager.tenancy import current_user

current_user.set(int(os.getenv("ACTOR_USER_ID", "1")))
resource = os.getenv("ACTOR_RESOURCE", "test-capacity")
limit = int(os.getenv("ACTOR_LIMIT", "1"))
store = Store(os.environ["ACTOR_DATABASE_URL"])
task = store.query("SELECT * FROM tasks WHERE id=%s", (os.environ["ACTOR_TASK_ID"],))[0]
task.update(_owner=task["lease_owner"], _execution_deadline=10**12, _private_revision=1)


def commit(conn):
    row = conn.execute("SELECT state,lease_owner FROM tasks WHERE id=%s", (task["id"],)).fetchone()
    return (
        ""
        if row and row["state"] == "generating" and row["lease_owner"] == task["_owner"]
        else "task_canceled"
    )


execution = Execution(SimpleNamespace(store=store), task, lambda: "", commit)


async def main():
    async def occupied():
        store.audit("capacity_actor_entered")
        print("occupied", flush=True)
        if sys.argv[1] == "hold":
            await asyncio.Event().wait()

    if sys.argv[1] == "probe":
        token = "probe-" + str(os.getpid())
        got = execution.acquire(resource, limit, token)
        print("available" if got else "busy", flush=True)
        if got:
            execution.release(token)
    else:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
        await execution.slot(resource, limit, occupied)


try:
    asyncio.run(main())
except asyncio.CancelledError:
    pass
finally:
    store.close()
