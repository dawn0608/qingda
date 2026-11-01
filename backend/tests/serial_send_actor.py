"""服务器隔离进程：跨进程读回发送水位，不访问微信或模型。"""

import asyncio
import os
import time

from test_automation import NOW
from test_worker import Gateway
from wechat_manager.service import Manager
from wechat_manager.store import Store
from wechat_manager.tenancy import current_user
from wechat_manager.worker import Worker

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
