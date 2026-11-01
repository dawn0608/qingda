"""取消与许可同时到达时不启动数据库副作用，也不泄漏本进程容量。"""

import asyncio

import pytest
from wechat_manager import database_async


async def test_cancellation_at_capacity_grant_is_not_swallowed(monkeypatch):
    parent = asyncio.current_task()
    calls = []

    class CancelOnGrant(asyncio.Semaphore):
        async def acquire(self):
            await super().acquire()
            parent.cancel()
            return True

    limit = CancelOnGrant(8)
    monkeypatch.setitem(database_async._limits, asyncio.get_running_loop(), limit)
    with pytest.raises(asyncio.CancelledError):
        await database_async.database_call(lambda: calls.append("unexpected"))
    assert calls == []
    assert limit._value == 8
