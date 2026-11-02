"""有界数据库阻塞调用；取消也等待已进入的短事务归还资源。"""

import asyncio
import weakref

_limits = weakref.WeakKeyDictionary()


async def database_call(function, *args, **kwargs):
    loop = asyncio.get_running_loop()
    limit = _limits.setdefault(loop, asyncio.Semaphore(8))
    waiter = asyncio.create_task(limit.acquire())
    try:
        done, _ = await asyncio.wait({waiter}, timeout=3)
        if not done:
            raise asyncio.TimeoutError()
        await waiter
    except BaseException:
        # Python 3.10 的 wait_for 在许可就绪与取消同轮发生时可能吞掉取消。
        # 独立等待并核对领取结果，取消不得启动新事务，也不能漏还许可。
        if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
            limit.release()
        else:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        raise
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        limit.release()
