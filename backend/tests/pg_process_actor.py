"""服务器隔离用独立进程；发送仅记数据库证据，不调用外部服务。"""

import asyncio
import os
import sys

from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import current_user
from qingda.worker import Worker
from test_automation import NOW
from test_worker import Gateway

current_user.set(int(os.getenv("ACTOR_USER_ID", "1")))

store = Store(os.environ["ACTOR_DATABASE_URL"])


class ProcessGateway(Gateway):
    async def model(self, *args):
        if sys.argv[1] == "hold":
            store.audit("isolated_generation_started")
            await asyncio.sleep(30)
        return await super().model(*args)

    async def send(self, *args):
        store.audit("isolated_send_boundary")
        if sys.argv[1] == "crash":
            os._exit(23)
        return {"ret": 200, "data": {"newMsgId": "123456789"}}


worker = Worker(
    Manager(store, clock=lambda: NOW + 10),
    ProcessGateway(),
    clock=lambda: float(os.environ.get("ACTOR_CLOCK", NOW + 10)),
)
if sys.argv[1] == "lease":
    try:
        worker.lease()
    except RuntimeError:
        print("lease_busy")
        store.close()
        sys.exit(0)
    worker.release()
    store.close()
    sys.exit(2)
try:
    asyncio.run(worker.once())
finally:
    worker.release()
    store.close()
