"""服务器隔离进程：跨进程读回发送水位，不访问微信或模型。"""

import asyncio
import os
import time

from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import current_user
from qingda.worker import Worker
from test_automation import NOW
from test_worker import Gateway

current_user.set(1)
store = Store(os.environ["ACTOR_DATABASE_URL"])
origin = float(os.environ["ACTOR_REAL_ORIGIN"])


class RecordingGateway(Gateway):
    async def send(self, app_id, peer, content):
        store.audit("isolated_serial_send_start", detail={"time": time.time(), "peer": peer})
        response = await super().send(app_id, peer, content)
        store.audit("isolated_serial_send_end", detail={"time": time.time(), "peer": peer})
        return response


def clock():
    return NOW + 5 + time.time() - origin


worker = Worker(Manager(store, clock=clock), RecordingGateway(), clock=clock)
try:
    asyncio.run(worker.once())
finally:
    worker.release()
    store.close()
