"""任务修订的持久预算、共享容量和脱敏阶段回执；外部等待不持有事务。"""

import asyncio
import contextvars
import json
import math
import time
import uuid

from .database_async import database_call
from .model_settings import TEXT_TIMEOUT
from .providers import ProviderError
from .reply_media import revision

AUDIT = contextvars.ContextVar("reply_call_audit", default=None)
PRICE_VERSION = "public-cny-2026-10-17"
ACTIVE_TASK_LIMIT = 4
DEEPSEEK_REQUEST_LIMIT = 2


def estimate(model, usage):
    """仅公开标价估算；供应商未给用量时保留未知，不伪装免费。"""
    if not isinstance(usage, dict) or not isinstance(model, str):
        return None
    for name in (
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "prompt_cache_hit_tokens",
    ):
        if name in usage and (
            type(usage[name]) not in (int, float) or not math.isfinite(usage[name]) or usage[name] < 0
        ):
            return None
    if not usage or "prompt_tokens" not in usage or "completion_tokens" not in usage:
        return None
    rates = {}
    # DeepSeek采用高峰公开单价作保守估算，避免节假日表漂移；对照固定同一口径。
    rates["deepseek-flash"] = (2, 0.04, 8)
    rate = next((v for k, v in rates.items() if model.startswith(k)), None)
    if rate is None:
        return None
    details = usage.get("prompt_tokens_details") or {}
    cached = usage.get(
        "prompt_cache_hit_tokens", details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    )
    if type(cached) not in (int, float) or not math.isfinite(cached) or cached < 0:
        return None
    cached = min(usage["prompt_tokens"], cached)
    return round(
        (
            (usage["prompt_tokens"] - cached) * rate[0]
            + cached * rate[1]
            + usage["completion_tokens"] * rate[2]
        )
        / 1_000_000,
        8,
    )


class Execution:
    def __init__(self, manager, task, guard, commit, text_limit=TEXT_TIMEOUT, quick_commit=None):
        self.manager, self.store, self.task = manager, manager.store, task
        self.guard, self.commit = guard, commit
        self.quick_commit = quick_commit or commit
        self.key = (task["id"], revision(task))
        self.owner = task["_owner"]
        self.text_limit = min(TEXT_TIMEOUT, text_limit)

    def initialize(self):
        now = time.time()
        with self.store.transaction() as conn:
            if reason := self.commit(conn):
                raise ProviderError(reason)
            inserted = conn.execute(
                "INSERT INTO reply_execution_budgets(task_id,input_revision,started_at,deadline,owner) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING task_id",
                (*self.key, now, now + 240, self.owner),
            ).fetchone()
            if inserted:
                from .private_replies import round_table

                window = conn.execute(
                    f"SELECT first_at,collect_until FROM {round_table(conn, self.task)} WHERE task_id=%s",
                    (self.task["id"],),
                ).fetchone()
                if window:
                    conn.execute(
                        "INSERT INTO reply_execution_events(task_id,input_revision,phase,details,created_at) VALUES(%s,%s,'collection_and_queue',%s,%s)",
                        (
                            *self.key,
                            json.dumps(
                                {
                                    "collection_seconds": max(
                                        0, window["collect_until"] - window["first_at"]
                                    ),
                                    "queue_seconds": max(0, self.manager.clock() - window["collect_until"]),
                                }
                            ),
                            now,
                        ),
                    )
            row = conn.execute(
                "SELECT * FROM reply_execution_budgets WHERE task_id=%s AND input_revision=%s FOR UPDATE",
                self.key,
            ).fetchone()
            # 进程崩溃时，未结算时间仍消耗原预算。正常修订只读取本修订的记录。
            if row["phase"]:
                elapsed = max(0, now - row["phase_started"])
                field = "media_seconds" if row["phase"] == "media" else "text_seconds"
                conn.execute(
                    f"UPDATE reply_execution_budgets SET {field}={field}+%s,phase='',phase_started=NULL WHERE task_id=%s AND input_revision=%s",
                    (elapsed, *self.key),
                )
            conn.execute(
                "UPDATE reply_execution_budgets SET owner=%s WHERE task_id=%s AND input_revision=%s",
                (self.owner, *self.key),
            )
            return row["deadline"]

    def event(self, phase, details):
        # 调用已发生后，即使取消，也必须记费用；此表不改变任务结果或证据缓存。
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO reply_execution_events(task_id,input_revision,phase,details,created_at) VALUES(%s,%s,%s,%s,%s)",
                (*self.key, phase, json.dumps(details, ensure_ascii=False), time.time()),
            )

    async def audit(self, details):
        details = {
            **details,
            "price_version": PRICE_VERSION,
            "estimated_yuan": estimate(details.get("model", ""), details.get("usage", {})),
        }
        await database_call(self.event, "provider", details)

    async def measure(self, phase, factory, **details):
        started = time.monotonic()
        outcome = "completed"
        try:
            return await factory()
        except BaseException:
            outcome = "interrupted"
            raise
        finally:
            await database_call(
                self.event, phase, {**details, "seconds": time.monotonic() - started, "outcome": outcome}
            )

    def phase(self, phase, start):
        now = time.time()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM reply_execution_budgets WHERE task_id=%s AND input_revision=%s FOR UPDATE",
                self.key,
            ).fetchone()
            if not row or row["owner"] != self.owner:
                raise ProviderError("task_canceled")
            if start:
                if reason := self.commit(conn):
                    raise ProviderError(reason)
                if row["phase"]:
                    raise ProviderError("execution_phase_overlap")
                field = "media_seconds" if phase == "media" else "text_seconds"
                remaining = min(
                    row["deadline"] - now, (180 if phase == "media" else self.text_limit) - row[field]
                )
                if remaining <= 0:
                    raise ProviderError(
                        "media_budget_exceeded" if phase == "media" else "generation_budget_exceeded"
                    )
                conn.execute(
                    "UPDATE reply_execution_budgets SET phase=%s,phase_started=%s WHERE task_id=%s AND input_revision=%s",
                    (phase, now, *self.key),
                )
                return remaining
            if row["phase"] == phase:
                field = "media_seconds" if phase == "media" else "text_seconds"
                elapsed = max(0, now - row["phase_started"])
                conn.execute(
                    f"UPDATE reply_execution_budgets SET {field}={field}+%s,phase='',phase_started=NULL WHERE task_id=%s AND input_revision=%s",
                    (elapsed, *self.key),
                )
                return elapsed
            return 0

    async def run(self, phase, factory):
        remaining = await database_call(self.phase, phase, True)
        token = AUDIT.set(self.audit)
        outcome = "completed"
        try:
            return await asyncio.wait_for(factory(), remaining)
        except asyncio.TimeoutError as exc:
            outcome = "timeout"
            raise ProviderError(
                "media_budget_exceeded" if phase == "media" else "generation_budget_exceeded"
            ) from exc
        except BaseException:
            outcome = "interrupted"
            raise
        finally:
            AUDIT.reset(token)
            elapsed = await database_call(self.phase, phase, False)
            await database_call(self.event, phase, {"seconds": elapsed, "outcome": outcome})

    def acquire(self, resource, limit, token):
        now = time.time()
        with self.store.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(17950,44)")
            if reason := self.quick_commit(conn):
                raise ProviderError(reason)
            conn.execute("DELETE FROM reply_resource_slots WHERE expires_at<%s", (now,))
            count = conn.execute(
                "SELECT count(*) FROM reply_resource_slots WHERE resource=%s", (resource,)
            ).fetchone()[0]
            if count >= limit:
                return False
            conn.execute(
                "INSERT INTO reply_resource_slots VALUES(%s,%s,%s,%s,%s)",
                (resource, token, self.task["id"], self.owner, now + 30),
            )
            return True

    def renew(self, token):
        with self.store.transaction() as conn:
            if reason := self.quick_commit(conn):
                raise ProviderError(reason)
            if not conn.execute(
                "UPDATE reply_resource_slots SET expires_at=%s WHERE token=%s AND owner=%s AND expires_at>%s RETURNING token",
                (time.time() + 30, token, self.owner, time.time()),
            ).fetchone():
                raise ProviderError("resource_lease_lost")

    def release(self, token):
        with self.store.connect() as conn:
            conn.execute("DELETE FROM reply_resource_slots WHERE token=%s AND owner=%s", (token, self.owner))

    async def slot(self, resource, limit, factory):
        token, started = uuid.uuid4().hex, time.monotonic()
        while not await database_call(self.acquire, resource, limit, token):
            if time.time() >= self.task["_execution_deadline"]:
                raise ProviderError("execution_budget_exceeded")
            await asyncio.sleep(0.2)
        await database_call(
            self.event, "resource_wait", {"resource": resource, "seconds": time.monotonic() - started}
        )

        async def heartbeat():
            while True:
                await asyncio.sleep(5)
                await database_call(self.renew, token)

        future, renewal = asyncio.create_task(factory()), asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait((future, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
            return await future
        finally:
            for call in (future, renewal):
                if not call.done():
                    call.cancel()
            await asyncio.gather(future, renewal, return_exceptions=True)
            await database_call(self.release, token)
