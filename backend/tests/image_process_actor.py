"""图片修订/崩溃接管的独立进程；不连接外部平台。"""

import asyncio
import os
import sys
from pathlib import Path

from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import current_user
from qingda.worker import Worker
from test_automation import NOW
from test_reply_images import Images, media

current_user.set(1)

store = Store(os.environ["ACTOR_DATABASE_URL"])
manager = Manager(store, clock=lambda: NOW + 20)
media(manager, Path(os.environ["ACTOR_MEDIA_DIR"]))


class ProcessImages(Images):
    async def download_media(self, *args, **kwargs):
        store.audit("image_download_started")
        if sys.argv[1] == "hold":
            await asyncio.sleep(30)
        return await super().download_media(*args, **kwargs)

    async def send(self, *args):
        store.audit("image_send_boundary")
        if sys.argv[1] == "crash_send":
            os._exit(23)
        return {"ret": 200, "data": {"newMsgId": "123456789"}}


worker = Worker(manager, ProcessImages(), clock=lambda: float(os.environ.get("ACTOR_CLOCK", NOW + 20)))
try:
    if sys.argv[1] == "lease":
        try:
            worker.lease()
        except RuntimeError:
            print("lease_busy")
        else:
            raise RuntimeError("unexpected lease takeover")
    else:
        asyncio.run(worker.once())
finally:
    worker.release()
    store.close()
