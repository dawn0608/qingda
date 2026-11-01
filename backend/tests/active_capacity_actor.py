"""独立用户 Worker 领取隔离任务；在执行边界等待，不调用模型或微信。"""

import asyncio
import json
import os
import signal

from test_automation import NOW
from test_worker import Gateway
from wechat_manager.service import Manager
from wechat_manager.store import Store
from wechat_manager.tenancy import current_user, user_scope
from wechat_manager.worker import Worker

current_user.set(int(os.environ["ACTOR_USER_ID"]))
store = Store(os.environ["ACTOR_DATABASE_URL"])


class NativeGateway(Gateway):
    async def model_with_tools(self, *args, **kwargs):
        raise AssertionError("领取验证不得生成或发送")


class HoldingWorker(Worker):
    async def _execute(self, task):
        await asyncio.Event().wait()


worker = HoldingWorker(Manager(store, clock=lambda: NOW + 35), NativeGateway(), clock=lambda: NOW + 35)


async def main():
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    try:
        counts = []
        for uid in (1, 2):
            with user_scope(uid):
                counts.append(await worker.once(background=True))
        print(json.dumps(counts), flush=True)
        await asyncio.Event().wait()
    finally:
        for future in worker.running:
            future.cancel()
        await asyncio.gather(*worker.running, return_exceptions=True)
        worker.release()


try:
    asyncio.run(main())
except asyncio.CancelledError:
    pass
finally:
    store.close()
