"""网页请求时钟的独立进程验收；只连接服务器隔离库。"""

import asyncio
import json
import os
import sys
import time

from wechat_manager.web_tools import WebRateLimiter


async def main():
    limiter = WebRateLimiter(os.environ["ACTOR_DATABASE_URL"])
    try:
        if sys.argv[1] == "crash":
            limiter.acquire("search")
            os._exit(23)
        starts = []
        for _ in range(3):
            await limiter.wait("search")
            starts.append(time.monotonic())
        print(json.dumps(starts), flush=True)
    finally:
        limiter.close()


asyncio.run(main())
