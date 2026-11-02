"""仅隔离 PostgreSQL：在真实 Worker 副作用边界杀进程，不调用微信或模型。"""

import asyncio
import json
import os
import sys

from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import current_user
from qingda.worker import Worker
from test_automation import NOW
from test_input_waits import WaitingGateway

current_user.set(1)
store = Store(os.environ["ACTOR_DATABASE_URL"])
mode = sys.argv[1]


class ActorGateway(WaitingGateway):
    async def model_with_tools(self, config, prompt, context, **runtime):
        if mode == "hold_generation":
            print(
                json.dumps({"phase": "bound", "ids": [v["message_id"] for v in context if v.get("current")]}),
                flush=True,
            )
            await asyncio.Event().wait()
        return await super().model_with_tools(config, prompt, context, **runtime)

    async def send(self, *args):
        store.audit("wait_actor_send")
        if mode == "hold_send":
            print(json.dumps({"phase": "dispatch"}), flush=True)
            await asyncio.Event().wait()
        return await super().send(*args)


gateway = ActorGateway()
gateway.control = "resume"
worker = Worker(Manager(store, clock=lambda: NOW + 30), gateway, clock=lambda: NOW + 30)
try:
    asyncio.run(worker.once())
    print(json.dumps({"phase": "complete", "sends": len(gateway.sends)}), flush=True)
finally:
    worker.release()
    store.close()
