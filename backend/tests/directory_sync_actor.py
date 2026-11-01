"""服务器隔离目录回归的独立进程；不连接微信或模型。"""

import asyncio
import json
import os
import sys

from wechat_manager.contacts import publish_directory
from wechat_manager.service import Manager
from wechat_manager.store import Store
from wechat_manager.tenancy import user_scope
from wechat_manager.worker import Worker


class Directory:
    def __init__(self, mode):
        self.mode, self.calls = mode, []

    async def call(self, path, payload, **kwargs):
        self.calls.append(path)
        if self.mode == "crash-sync" and path.endswith("fetchContactsList"):
            print("requested", flush=True)
            sys.stdin.readline()
            os._exit(23)
        return {"ret": 200, "data": {"friends": ["alice", "bob"], "chatrooms": ["saved@chatroom"]}}


async def run(store, manager, payload, mode):
    gateway = Directory(mode)
    worker = Worker(manager, gateway, clock=lambda: payload["now"])
    try:
        worked = await worker.contacts_once()
        return {"worked": worked, "calls": gateway.calls}
    except RuntimeError:
        return {"busy": True, "calls": gateway.calls}


if __name__ == "__main__":
    payload = json.loads(os.environ["DIRECTORY_ACTOR_PAYLOAD"])
    with user_scope(int(os.environ["DIRECTORY_ACTOR_USER"])):
        store = Store(os.environ["DIRECTORY_ACTOR_DATABASE"])
        manager = Manager(store, clock=lambda: payload["now"])
        try:
            if sys.argv[1] == "publish":
                sys.stdin.readline()
                with store.transaction() as conn:
                    changed = publish_directory(conn, payload["aid"], payload["now"])
                result = {"changed": len(changed)}
            else:
                result = asyncio.run(run(store, manager, payload, sys.argv[1]))
            print(json.dumps(result), flush=True)
        finally:
            store.close()
