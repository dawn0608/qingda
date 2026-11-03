import asyncio
import hashlib
import json
import os
import signal
import time
import uuid

from . import dialogue_windows, private_replies, reply_decisions, reply_media, reply_queue
from .config import ROOT
from .contact_changes import ContactRecovery
from .contacts import ContactStatistics
from .database_async import database_call
from .discovery import AccountDiscovery
from .model_settings import available_tools, effective_model
from .protocol import redact
from .providers import ProviderError
from .reply_execution import ACTIVE_TASK_LIMIT, DEEPSEEK_REQUEST_LIMIT
from .tenancy import current_user, user_scope


class Worker:
    def __init__(self, manager, gateway, prompt_file=ROOT / "prompts/auto_reply.md", clock=time.time):
        self.manager, self.store, self.gateway, self.clock = manager, manager.store, gateway, clock
        self.owner = str(uuid.uuid4())
        self.prompt_file = prompt_file
        self.account_locks = {}
        self.last_send = {}
        self.started = False
        self.started_users = set()
        self.running = {}
        self.discovery = AccountDiscovery(manager, gateway, self.owner, clock)
        self.contact_recovery = ContactRecovery(manager, gateway, self.owner, clock)
        self.contacts = ContactStatistics(manager, gateway, self.owner, clock, self.contact_recovery)
        from .contact_discovery import ContactDiscovery
        from .contact_profiles import ContactProfiles

        self.profiles = ContactProfiles(manager, gateway)
        from .reply_profiles import ReplyProfiles

        self.reply_profiles = ReplyProfiles(manager, gateway, clock)
        self.contact_discovery = ContactDiscovery(manager, gateway, self.owner, clock, self.profiles)

    async def verify_contacts_once(self):
        await database_call(self.lease)
        return await self.contact_recovery.once()

    async def monitor_contacts_once(self):
        await database_call(self.lease)
        return await database_call(self.contact_recovery.schedule)

    async def contacts_once(self):
        await database_call(self.lease)
        return await self.contacts.once()

    async def contact_profiles_once(self):
        await database_call(self.lease)
        return await self.contacts.enrich_once()

    async def discover_once(self):
        await database_call(self.lease)
        return await self.discovery.once()

    def lease(self):
        now = self.clock()
        with self.store.transaction() as c:
            lock = c.execute("SELECT * FROM worker_lock WHERE id=1 FOR UPDATE").fetchone()
            if lock and lock["owner"] != self.owner and lock["expires_at"] > now:
                raise RuntimeError("另一个任务 worker 正在运行")
            if lock and lock["owner"] != self.owner:
                c.execute("DELETE FROM reply_resource_slots WHERE owner=%s", (lock["owner"],))
            c.execute(
                "INSERT INTO worker_lock VALUES(1,%s,%s) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                (self.owner, now + 120),
            )
            self.store.register_runtime("worker", c)
            c.execute(
                "UPDATE reply_resource_slots SET expires_at=%s WHERE resource='active' AND owner=%s",
                (time.time() + 120, self.owner),
            )
            if current_user.get() and current_user.get() not in self.started_users:
                # 已持有全局领导锁且旧领导租约失效；只回收旧任务所有者的计算租约。
                c.execute(
                    "DELETE FROM reply_resource_slots r USING tasks t WHERE r.user_id=t.user_id AND r.task_id=t.id AND r.owner=t.lease_owner AND r.owner<>%s",
                    (self.owner,),
                )
                c.execute(
                    "UPDATE incoming_media im SET state='failed',reason='recovered_image_preparation',updated_at=%s WHERE im.kind='image' AND im.state='downloading' AND EXISTS(SELECT 1 FROM reply_media_dependencies d JOIN tasks t ON t.id=d.task_id WHERE d.source_message_id=im.message_id AND t.state IN ('generating','preparing','ready','queued','canceled'))",
                    (now,),
                )
                from .speech import FINGERPRINT as SPEECH_POLICY

                legacy = c.execute(
                    "SELECT t.* FROM tasks t WHERE kind='auto' AND state IN ('queued','generating','preparing','ready') "
                    "AND (state='ready' OR trace_json<>'{}'::jsonb) AND (trace_json->>'image_policy' IS DISTINCT FROM %s "
                    "OR trace_json->>'message_format' IS DISTINCT FROM %s "
                    "OR (trace_json->>'speech_policy' IS DISTINCT FROM %s AND ("
                    "EXISTS(SELECT 1 FROM messages m WHERE m.id=t.message_id AND m.msg_type='34') OR "
                    "EXISTS(SELECT 1 FROM reply_media_dependencies d JOIN messages m ON m.id=d.source_message_id "
                    "WHERE d.task_id=t.id AND m.msg_type='34'))))",
                    (reply_media.VERSION, reply_decisions.VERSION, SPEECH_POLICY),
                ).fetchall()
                for old in legacy:
                    generated = {old["message_id"], *json.loads(old["handled_message_ids"])}
                    generated.update(
                        value.get("message_id")
                        for value in json.loads(old["context_json"])
                        if value.get("current")
                    )
                    for member in reply_queue.members(c, old):
                        canceled = member["id"] == old["id"] or member["message_id"] in generated
                        c.execute(
                            "UPDATE tasks SET state=%s,reason='reply_policy_changed',lease_owner=NULL,updated_at=%s WHERE id=%s AND state IN ('queued','generating','preparing','ready','merged')",
                            ("canceled" if canceled else "queued", now, member["id"]),
                        )
                        if not canceled:
                            c.execute("DELETE FROM reply_batch_items WHERE task_id=%s", (member["id"],))
                for row in c.execute(
                    "SELECT DISTINCT account_id FROM tasks WHERE state='sending'"
                ).fetchall():
                    reply_queue.ended(c, row[0], now)
                c.execute(
                    "UPDATE tasks SET state='unknown',reason='restart_after_dispatch',updated_at=%s WHERE state='sending'",
                    (now,),
                )
                c.execute(
                    "UPDATE tasks SET state='queued',reason='recovered_before_dispatch',lease_owner=NULL WHERE state IN ('generating','ready','preparing')"
                )
                self.started = True
                self.started_users.add(current_user.get())

    def release(self):
        with self.store.connect() as c:
            c.execute("DELETE FROM worker_lock WHERE owner=%s", (self.owner,))

    def update(self, tid, state, reason="", **fields):
        permitted = {
            "content",
            "context_json",
            "response_json",
            "usage_json",
            "trace_json",
            "duration_ms",
            "completed_at",
        }
        fields = {k: v for k, v in fields.items() if k in permitted}
        with self.store.connect() as c:
            columns = ",".join(k + "=%s" for k in fields)
            c.execute(
                "UPDATE tasks SET state=%s,reason=%s,updated_at=%s"
                + ("," + columns if columns else "")
                + " WHERE id=%s AND lease_owner=%s AND state NOT IN ('canceled','accepted','unknown')",
                (state, reason, self.clock(), *fields.values(), tid, self.owner),
            )

    def check(self, task, conn, sending=False, quick=False):
        a = self.manager.account(task["account_id"], conn)
        conv = self.manager.conversation(task["conversation_id"], conn)
        if conv["removed_at"] is not None:
            return "contact_removed"
        if not a["enabled"] or a["archived_at"] is not None:
            return "account_disabled"
        if a["epoch"] != task["account_epoch"]:
            return "rules_changed"
        terminal = conn.execute(
            "SELECT verified FROM terminals WHERE app_id=%s AND account_id=%s", (a["current_app_id"], a["id"])
        ).fetchone()
        if not terminal or not terminal[0]:
            return "unverified_identity"
        if task["kind"] == "auto":
            if sending:
                row = conn.execute(
                    "SELECT trace_json->>'image_policy' AS image_policy,trace_json->>'message_format' AS message_format FROM tasks WHERE id=%s",
                    (task["id"],),
                ).fetchone()
                if not row or row[0] != reply_media.VERSION or row[1] != reply_decisions.VERSION:
                    return "reply_policy_changed"
            if a["epoch"] != task["account_epoch"] or conv["epoch"] != task["conversation_epoch"]:
                return "rules_changed"
            if self.store.setting("model_revision", 0, conn) != task["config_version"]:
                return "model_changed"
            if private_replies.changed(conn, task, self.owner):
                return private_replies.change_reason(task)
            if reason := reply_media.check(conn, task):
                return reason
            from .context_snapshot import check as check_context

            if not quick and check_context(self.manager, conn, task):
                return private_replies.change_reason(task)
            if quick and task.get("_context_frozen"):
                from .callbacks import revocation_revision

                used = conn.execute(
                    "SELECT revision FROM reply_view_versions WHERE task_id=%s", (task["id"],)
                ).fetchone()
                if used and used[0] != revocation_revision(conn, task["conversation_id"]):
                    return private_replies.change_reason(task)
            for msg in dialogue_windows.inputs(conn, reply_queue.members(conn, task)):
                if not msg:
                    return "message_missing"
                from .callbacks import revoked

                if revoked(conn, msg["id"]):
                    return "message_revoked"
                from .input_waits import authorized

                deferred = msg.get("_input_kind") == "deferred"
                if deferred and not authorized(self.manager, conn, task, msg, self.clock()):
                    return "input_wait_ended"
                reason = self.manager.guard(
                    a,
                    conv,
                    json.loads(msg["metadata"]),
                    msg["created_at"],
                    self.clock(),
                    conn,
                    allow_old=deferred,
                )
                if reason:
                    return reason
        if task.get("_execution_deadline") and time.time() > task["_execution_deadline"]:
            return "execution_budget_exceeded"
        if sending or quick:
            lock = conn.execute("SELECT * FROM worker_lock WHERE id=1").fetchone()
            if not lock or lock["owner"] != self.owner or lock["expires_at"] < self.clock():
                return "worker_lease_lost"
        return ""

    def reject(self, task, reason):
        if reason in ("private_input_changed", "group_input_changed"):
            with self.store.transaction() as conn:
                private_replies.requeue(conn, task, self.clock())
        else:
            self.update(task["id"], "canceled", reason)

    def prune_emoji(self, task):
        from .emoji_policy import prune

        with self.store.transaction() as conn:
            return prune(self.manager, conn, task, self.clock())

    async def execute(self, task):
        try:
            return await self._execute(task)
        finally:
            await database_call(
                self.store.query,
                "DELETE FROM reply_resource_slots WHERE resource='active' AND task_id=%s AND user_id=%s AND owner=%s RETURNING token",
                (task["id"], task["user_id"], self.owner),
            )

    async def _execute(self, task):
        start = time.monotonic()
        tid = task["id"]
        if await database_call(self.prune_emoji, task):
            return

        def _database_execute_169():
            with self.store.connect() as c:
                reason = self.check(task, c)
            return ("normal", None, locals())

        _database_result = await database_call(_database_execute_169)
        if "reason" in _database_result[2]:
            reason = _database_result[2]["reason"]
        if reason:
            await database_call(self.reject, task, reason)
            return
        if task["kind"] != "auto":
            await database_call(self.update, tid, "canceled", "unsupported_task_kind")
            return
        content = task["content"]
        if task["kind"] == "auto":
            try:
                account = await database_call(self.manager.account, task["account_id"])
                kind = (await database_call(self.manager.conversation, task["conversation_id"]))["kind"]
                from .reply_prompt import load

                template, prompt = load(
                    account["prompt"], kind, self.prompt_file, owner_profile=account["owner_profile"]
                )
                from . import context_budget, context_snapshot
                from .history_tool import TOOL as HISTORY_TOOL
                from .history_tool import HistoryReader
                from .reply_tools import TOOLS

                def _database_execute_185():
                    with self.store.connect() as c:
                        batch = reply_queue.members(c, task)
                    return ("normal", None, locals())

                _database_result = await database_call(_database_execute_185)
                if "batch" in _database_result[2]:
                    batch = _database_result[2]["batch"]

                def media_commit(conn, quick=False):
                    lock = conn.execute(
                        "SELECT owner,expires_at FROM worker_lock WHERE id=1 FOR SHARE"
                    ).fetchone()
                    if not lock or lock["owner"] != self.owner or lock["expires_at"] < self.clock():
                        return "worker_lease_lost"
                    owned = conn.execute(
                        "SELECT state,lease_owner FROM tasks WHERE id=%s FOR UPDATE", (task["id"],)
                    ).fetchone()
                    if not owned or owned["state"] != "generating" or owned["lease_owner"] != self.owner:
                        return "task_canceled"
                    return self.check(task, conn, quick=quick)

                config = effective_model(await database_call(self.store.setting, "model", {}))
                from .reply_execution import Execution

                def runtime_guard():
                    with self.store.connect() as conn:
                        return media_commit(conn)

                execution = Execution(
                    self.manager,
                    task,
                    runtime_guard,
                    media_commit,
                    config["timeout"],
                    quick_commit=lambda conn: media_commit(conn, quick=True),
                )
                task["_execution_deadline"] = await database_call(execution.initialize)
                task["_execution"] = execution
                from . import input_waits

                if await input_waits.process(self, task, batch, config, execution, media_commit):
                    return
                extra_images = await database_call(reply_media.freeze, self.manager, task, batch)
                # 语音先转写，静态图片直接提供像素；所有来源复用同一授权和持久预算。
                if getattr(self.manager, "media", None):
                    has_voice = False

                    def preparation_ids():
                        nonlocal has_voice
                        with self.store.connect() as conn:
                            inputs = dialogue_windows.inputs(conn, batch)
                            has_voice = any(
                                json.loads(m["metadata"]).get("message_kind") == "voice" for m in inputs
                            )
                            ids = {
                                m["id"]
                                for m in inputs
                                if json.loads(m["metadata"]).get("message_kind") in ("voice", "image")
                            }
                            ids.update(
                                mid
                                for mid in extra_images
                                if (row := reply_media.visible(conn, task, mid))
                                and json.loads(row["metadata"]).get("message_kind") in ("voice", "image")
                            )
                            return sorted(ids)

                    async def prepare_mid(mid):
                        async def prepare_input():
                            return await self.manager.media.prepare(
                                {**task, "message_id": mid, "_media_commit": media_commit},
                                self.gateway,
                                runtime_guard,
                            )

                        try:
                            await private_replies.current_call(
                                self, task, execution.run("media", prepare_input)
                            )
                        except (ProviderError, OSError, ValueError):
                            if reason := await database_call(runtime_guard):
                                raise ProviderError(reason)

                    prepared = await database_call(preparation_ids)
                    for mid in prepared:
                        await prepare_mid(mid)
                        if await database_call(self.prune_emoji, task):
                            return
                    # 当前语音转写完成后才可确认其素材承接关系；只补准备新选中的素材。
                    if has_voice:
                        extra_images = await database_call(reply_media.freeze, self.manager, task, batch)
                        for mid in set(await database_call(preparation_ids)) - set(prepared):
                            await prepare_mid(mid)
                context = await database_call(self.manager.context, task)
                if len(batch) > 1 and not any(v.get("input_scope") == "deferred" for v in context):
                    # 只在当前批次超限时拆出完整消息；不裁掉问题或额外读取媒体。
                    def fit_batch():
                        schemas = available_tools(config, TOOLS) + [
                            reply_decisions.TOOL,
                            HISTORY_TOOL,
                        ]
                        try:
                            context_budget.fit_prompt(context, prompt, schemas)
                            return context
                        except ProviderError as error:
                            if error.reason != "current_batch_too_large":
                                raise
                        count = 0
                        for end in range(1, len(batch)):
                            try:
                                context_budget.fit_prompt(
                                    self.manager.context(task, batch[:end]), prompt, schemas
                                )
                                count = end
                            except ProviderError as error:
                                if error.reason != "current_batch_too_large":
                                    raise
                                break
                        if not count:
                            raise ProviderError("current_batch_too_large")
                        with self.store.transaction() as conn:
                            if reason := media_commit(conn):
                                raise ProviderError(reason)
                            reply_queue.split(conn, task, count, self.clock())
                            private_replies.split(conn, task, self.clock())
                            valid = [v["id"] for v in dialogue_windows.inputs(conn, batch[:count])]
                            conn.execute(
                                "DELETE FROM reply_media_dependencies WHERE task_id=%s AND NOT(origin_message_id=ANY(%s))",
                                (tid, valid),
                            )
                        return self.manager.context(task)

                    context = await database_call(fit_batch)
                context = await database_call(
                    reply_media.context_images, self.manager, task, context, media_commit
                )
                context = await database_call(
                    reply_queue.fit_context, context, getattr(self.manager, "media", None)
                )
                context = await database_call(
                    context_budget.fit_prompt,
                    context,
                    prompt,
                    available_tools(config, TOOLS) + [reply_decisions.TOOL, HISTORY_TOOL],
                )
                from .callbacks import revocation_revision

                def freeze_view_version():
                    with self.store.transaction() as conn:
                        if reason := media_commit(conn):
                            raise ProviderError(reason)
                        conn.execute(
                            "INSERT INTO reply_view_versions VALUES(%s,%s) ON CONFLICT(user_id,task_id) DO UPDATE SET revision=excluded.revision",
                            (tid, revocation_revision(conn, task["conversation_id"])),
                        )

                await database_call(freeze_view_version)
                context = await database_call(context_snapshot.freeze, self.manager, task, context)
                task["_context_frozen"] = True
                await database_call(
                    self.update,
                    tid,
                    "generating",
                    context_json=json.dumps(
                        [
                            {
                                "message_id": item.get("message_id"),
                                "role": item.get("role"),
                                "sender": item.get("sender"),
                                "current": item.get("current", False),
                                "message_kind": item.get("message_kind"),
                                "input_scope": item.get("input_scope"),
                                "context_scope": item.get("context_scope"),
                                "media_status": item.get("media_status"),
                                "reference": item.get("reference"),
                                "recent_images": item.get("recent_images"),
                                "recent_media": item.get("recent_media"),
                                "images": item.get("images", []),
                            }
                            for item in context
                        ],
                        ensure_ascii=False,
                    ),
                )
                trace = {
                    "model": config["model"],
                    "provider": config["provider"],
                    "config_version": task["config_version"],
                    "template_sha256": hashlib.sha256(template.encode()).hexdigest(),
                    "account_prompt_sha256": hashlib.sha256(account["prompt"].encode()).hexdigest(),
                    "owner_profile_version": account["owner_profile_version"],
                    "owner_profile_sha256": hashlib.sha256(account["owner_profile"].encode()).hexdigest(),
                    "code_revision": os.getenv("APP_REVISION", "development"),
                    "message_format": reply_decisions.VERSION,
                    "temperature": 0.2,
                    "conversation_kind": kind,
                }
                trace["input_revision"] = task.get("_private_revision", task.get("_group_revision", 0))
                trace["wait_model_rounds"] = task.get("_wait_rounds", 0)
                trace["wait_usage"] = task.get("_wait_usage", {})
                trace["context_budget"] = await database_call(context_budget.statistics, context)
                trace["image_policy"] = reply_media.VERSION
                from .speech import FINGERPRINT as SPEECH_POLICY

                trace["speech_policy"] = SPEECH_POLICY
                trace["model_images"] = [frame for item in context for frame in item.get("images", [])]
                trace["media_dependencies"] = await database_call(reply_media.snapshot, self.manager, task)
                from .reply_profiles import VERSION
                from .reply_profiles import fingerprint as profile_fingerprint

                trace["profile_schema_version"] = VERSION
                trace["profile_snapshot_sha256"] = profile_fingerprint(
                    context[reply_queue.current_start(context)].get("participant_profiles", {})
                )
                await database_call(self.update, tid, "generating", trace_json=json.dumps(trace))
                if hasattr(self.gateway, "model_with_tools"):
                    from .tool_generation import Checkpoint, fingerprint

                    def generation_guard():
                        with self.store.connect() as conn:
                            current = conn.execute(
                                "SELECT state,lease_owner FROM tasks WHERE id=%s", (tid,)
                            ).fetchone()
                            if (
                                not current
                                or current["state"] != "generating"
                                or current["lease_owner"] != self.owner
                            ):
                                return "task_canceled"
                            # 循环和检查点只核对版本；每次模型请求的预算入口、
                            # 媒体读取/落库、生成完成和发送边界仍执行完整校验。
                            return self.check(task, conn, quick=True)

                    def generation_progress(details, usage):
                        from .tool_generation import add_usage

                        combined = json.loads(json.dumps(usage))
                        trace.update(details)
                        add_usage(combined, task.get("_wait_usage", {}))
                        self.update(
                            tid,
                            "generating",
                            trace_json=json.dumps(
                                {
                                    **trace,
                                    **details,
                                    "context_capture": execution.capture.manifest(),
                                    "reply_model_rounds": details.get("model_rounds", 0),
                                    "model_rounds": details.get("model_rounds", 0)
                                    + task.get("_wait_rounds", 0),
                                },
                                ensure_ascii=False,
                            ),
                            usage_json=json.dumps(combined),
                        )

                    generation = self.gateway.model_with_tools(
                        config,
                        prompt,
                        context,
                        checkpoint=Checkpoint(
                            self.store, tid, fingerprint(config, prompt, context), execution.capture
                        ),
                        guard=generation_guard,
                        progress=generation_progress,
                        decision_mode=True,
                        execution=execution,
                        history=await database_call(
                            HistoryReader, self.manager, task, context, generation_guard
                        ),
                    )
                else:
                    generation = self.gateway.model(config, prompt, context)
                call = asyncio.wait_for(
                    generation,
                    timeout=max(0.01, task["_execution_deadline"] - time.time())
                    if execution
                    else config["timeout"],
                )
                response = await private_replies.current_call(self, task, call)
                from .tool_generation import add_usage

                response["usage"] = response.get("usage") or {}
                add_usage(response["usage"], task.get("_wait_usage", {}))
                if await database_call(self.prune_emoji, task):
                    return
                decision = reply_decisions.validate(response.get("reply_decision"), context)
                choices = response.get("choices") or []
                answer = choices[0] if choices else {}
                message = answer.get("message") or {}
                content = message.get("content")
                if (
                    answer.get("finish_reason") != "stop"
                    or not isinstance(content, str)
                    or (decision["action"] == "reply" and not content.strip())
                    or message.get("tool_calls")
                    or len(content) > 10000
                ):
                    raise ProviderError("invalid_model_output")
                if content != decision["content"]:
                    raise ProviderError("invalid_reply_decision")
                await database_call(self.manager.process_events, now=self.clock())

                def _database_execute_313():
                    with self.store.transaction() as c:
                        reason = self.check(task, c)
                        if reason:
                            raise ProviderError(reason)
                        if c.execute(
                            "SELECT 1 FROM events e JOIN terminals t ON t.app_id=e.app_id WHERE e.processed=0 AND t.account_id=%s LIMIT 1",
                            (task["account_id"],),
                        ).fetchone():
                            c.execute(
                                "UPDATE tasks SET state='queued',reason='waiting_for_inbox',lease_owner=NULL WHERE id=%s",
                                (tid,),
                            )
                            return ("return", None, locals())
                        reply_decisions.record(c, task, decision, self.clock())
                        if decision["action"] == "wait":
                            input_waits.start(self.manager, c, task, decision, self.clock())
                        if decision["action"] in ("silent", "wait"):
                            c.execute(
                                "UPDATE tasks SET state='skipped',reason=%s,content='',completed_at=%s,updated_at=%s,usage_json=%s,duration_ms=%s WHERE id=%s AND state='generating' AND lease_owner=%s",
                                (
                                    "reply_waiting_input"
                                    if decision["action"] == "wait"
                                    else "reply_silent_" + decision["reason"],
                                    self.clock(),
                                    self.clock(),
                                    json.dumps(response.get("usage") or {}),
                                    int((time.monotonic() - start) * 1000),
                                    tid,
                                    self.owner,
                                ),
                            )
                            reply_decisions.apply_dialogue(c, task, self.clock())
                            return ("return", None, locals())
                    return ("normal", None, locals())

                _database_result = await database_call(_database_execute_313)
                if "reason" in _database_result[2]:
                    reason = _database_result[2]["reason"]
                if _database_result[0] == "return":
                    return _database_result[1]
                await database_call(
                    self.update,
                    tid,
                    "ready",
                    content=content,
                    trace_json=json.dumps(
                        {
                            **trace,
                            "reply_model_rounds": trace.get("model_rounds", 0),
                            "model_rounds": trace.get("model_rounds", 0) + task.get("_wait_rounds", 0),
                            "context_capture": execution.capture.manifest(),
                        }
                    ),
                    usage_json=json.dumps(response.get("usage") or {}),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            except Exception as e:

                def _database_execute_350():
                    with self.store.connect() as c:
                        canceled = self.check(task, c) if task["kind"] == "auto" else ""
                    return ("normal", None, locals())

                _database_result = await database_call(_database_execute_350)
                if "canceled" in _database_result[2]:
                    canceled = _database_result[2]["canceled"]
                if canceled in ("private_input_changed", "group_input_changed"):
                    await database_call(self.reject, task, canceled)
                    return
                await database_call(
                    self.update,
                    tid,
                    "canceled" if canceled else "failed",
                    canceled or (e.reason if isinstance(e, ProviderError) else "generation_failed"),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
                return
        account_key = (task["user_id"], task["account_id"])
        lock = self.account_locks.setdefault(account_key, asyncio.Lock())
        send_wait_started = time.monotonic()
        async with lock:

            def _database_execute_364():
                with self.store.connect() as c:
                    wait = reply_queue.wait_seconds(c, task["account_id"], self.clock())
                return ("normal", None, locals())

            _database_result = await database_call(_database_execute_364)
            if "wait" in _database_result[2]:
                wait = _database_result[2]["wait"]
            wait = max(
                wait,
                self.last_send.get(account_key, 0) + reply_queue.SEND_INTERVAL_SECONDS - time.monotonic(),
            )
            if wait:
                await asyncio.sleep(wait)
            if current_execution := task.get("_execution"):
                await database_call(
                    current_execution.event, "send_wait", {"seconds": time.monotonic() - send_wait_started}
                )
            try:
                account = await database_call(self.manager.account, task["account_id"])
                online = await self.gateway.online(account["current_app_id"])

                def _database_execute_372():
                    with self.store.connect() as c:
                        c.execute(
                            "UPDATE accounts SET online=%s,online_checked_at=%s,last_successful_refresh_at=%s WHERE id=%s AND epoch=%s AND current_app_id=%s",
                            (
                                online,
                                self.clock(),
                                self.clock(),
                                account["id"],
                                account["epoch"],
                                account["current_app_id"],
                            ),
                        )
                    return ("normal", None, locals())

                _database_result = await database_call(_database_execute_372)
                if not online:
                    await database_call(self.update, tid, "failed", "account_offline")
                    return
            except Exception:
                await database_call(self.update, tid, "failed", "online_check_failed")
                return
            # 生成期间手机发出的人工消息也须及时接管；先处理已落库回调。
            await database_call(self.manager.process_events, now=self.clock())

            def _database_execute_392():
                with self.store.transaction() as c:
                    from .callbacks import cancel_revoked_batches

                    cancel_revoked_batches(c, self.clock())
                    current = c.execute("SELECT * FROM tasks WHERE id=%s", (tid,)).fetchone()
                    if (
                        not current
                        or current["state"] not in ("preparing", "ready")
                        or current["lease_owner"] != self.owner
                    ):
                        return ("return", None, locals())
                    if (
                        task["kind"] == "auto"
                        and c.execute(
                            "SELECT 1 FROM events e JOIN terminals t ON t.app_id=e.app_id WHERE e.processed=0 AND t.account_id=%s LIMIT 1",
                            (task["account_id"],),
                        ).fetchone()
                    ):
                        c.execute(
                            "UPDATE tasks SET state='queued',reason='waiting_for_inbox',lease_owner=NULL WHERE id=%s",
                            (tid,),
                        )
                        return ("return", None, locals())
                    checking = dict(current)
                    checking["_context_frozen"] = task.get("_context_frozen", False)
                    checking["_execution_deadline"] = task.get("_execution_deadline")
                    if "_private_revision" in task:
                        checking["_private_revision"] = task["_private_revision"]
                    if "_group_revision" in task:
                        checking["_group_revision"] = task["_group_revision"]
                    reason = self.check(checking, c, sending=True)
                    if reason:
                        if reason in ("private_input_changed", "group_input_changed"):
                            private_replies.requeue(c, task, self.clock())
                            return ("return", None, locals())
                        c.execute(
                            "UPDATE tasks SET state='canceled',reason=%s,updated_at=%s WHERE id=%s",
                            (reason, self.clock(), tid),
                        )
                        return ("return", None, locals())
                    c.execute(
                        "UPDATE tasks SET state='sending',updated_at=%s WHERE id=%s", (self.clock(), tid)
                    )
                    account = self.manager.account(task["account_id"], c)
                    conv = self.manager.conversation(task["conversation_id"], c)
                return ("normal", None, locals())

            _database_result = await database_call(_database_execute_392)
            if "account" in _database_result[2]:
                account = _database_result[2]["account"]
            if "conv" in _database_result[2]:
                conv = _database_result[2]["conv"]
            if "reason" in _database_result[2]:
                reason = _database_result[2]["reason"]
            if _database_result[0] == "return":
                return _database_result[1]
            # 副作用边界已提交数据库，之后任何无法确认的异常都不得重发。
            try:

                async def send_reply():
                    return await self.gateway.send(account["current_app_id"], conv["peer_id"], content)

                current_execution = task.get("_execution")
                response = (
                    await asyncio.wait_for(
                        current_execution.measure("send", send_reply),
                        max(0.01, task["_execution_deadline"] - time.time()),
                    )
                    if current_execution
                    else await send_reply()
                )
                state = "accepted" if response.get("ret") == 200 else "failed"

                def _database_execute_458():
                    with self.store.connect() as c:
                        reply_queue.ended(c, task["account_id"], self.clock())
                        c.execute(
                            "UPDATE tasks SET state=%s,reason=%s,response_json=%s,updated_at=%s,completed_at=%s,duration_ms=%s WHERE id=%s AND state='sending'",
                            (
                                state,
                                "" if state == "accepted" else "platform_rejected",
                                json.dumps(redact(response), ensure_ascii=False),
                                self.clock(),
                                self.clock(),
                                int((time.monotonic() - start) * 1000),
                                tid,
                            ),
                        )
                        if state == "accepted":
                            from .media_references import record_outbound

                            record_outbound(self.store, c, task, response, state, self.clock())
                        from .callbacks import reconcile

                        reconcile(c, task["account_id"], task["conversation_id"])
                        if task["kind"] == "auto" and state == "accepted":
                            reply_decisions.apply_dialogue(c, task, self.clock())
                    return ("normal", None, locals())

                _database_result = await database_call(_database_execute_458)
            except Exception as e:
                state = "failed" if isinstance(e, ProviderError) and not e.uncertain else "unknown"

                def _database_execute_493(e):
                    with self.store.connect() as c:
                        reply_queue.ended(c, task["account_id"], self.clock())
                        c.execute(
                            "UPDATE tasks SET state=%s,reason=%s,updated_at=%s WHERE id=%s AND state='sending'",
                            (
                                state,
                                e.reason if state == "failed" else "send_response_unknown",
                                self.clock(),
                                tid,
                            ),
                        )
                        if state == "unknown":
                            from .media_references import record_outbound

                            record_outbound(self.store, c, task, {}, state, self.clock())
                    return ("normal", None, locals())

                _database_result = await database_call(_database_execute_493, e)
            finally:
                self.last_send[account_key] = time.monotonic()

                def _database_execute_506():
                    with self.store.transaction() as c:
                        reply_queue.ended(c, task["account_id"], self.clock())
                    return ("normal", None, locals())

                _database_result = await database_call(_database_execute_506)

    async def once(self, background=False):
        await database_call(self.lease)
        from .history_deletion import advance

        await database_call(advance, self.manager)
        await database_call(self.manager.process_events, now=self.clock())
        from .automation_defaults import maintain

        def _database_once_514():
            with self.store.transaction() as conn:
                maintain(self.manager, conn)
            return ("normal", None, locals())

        _database_result = await database_call(_database_once_514)
        for future in list(self.running):
            if future.done() and self.running[future].get("user_id") == current_user.get():
                task = self.running.pop(future)
                try:
                    future.result()
                except Exception:

                    def _database_once_524():
                        with self.store.transaction() as c:
                            row = c.execute("SELECT state FROM tasks WHERE id=%s", (task["id"],)).fetchone()
                            if row and row["state"] in ("generating", "preparing", "ready", "sending"):
                                uncertain = row["state"] == "sending"
                                c.execute(
                                    "UPDATE tasks SET state=%s,reason=%s,updated_at=%s WHERE id=%s AND lease_owner=%s",
                                    (
                                        "unknown" if uncertain else "failed",
                                        "send_response_unknown" if uncertain else "worker_task_failed",
                                        self.clock(),
                                        task["id"],
                                        self.owner,
                                    ),
                                )
                                if uncertain:
                                    reply_queue.ended(c, task["account_id"], self.clock())
                        return ("normal", None, locals())

                    _database_result = await database_call(_database_once_524)
                    await database_call(self.store.audit, "worker_task_failed")
        binding = await database_call(
            self.store.query, "SELECT binding_state FROM users WHERE id=%s", (current_user.get(),)
        )
        if not binding or binding[0]["binding_state"] != "configured":
            return 0
        task_limit = (
            ACTIVE_TASK_LIMIT if hasattr(self.gateway, "model_with_tools") else DEEPSEEK_REQUEST_LIMIT
        )
        capacity = task_limit - len(self.running)
        if capacity <= 0:
            return 0
        selected = []

        def _database_once_545():
            with self.store.transaction() as c:
                lock = c.execute("SELECT owner,expires_at FROM worker_lock WHERE id=1 FOR UPDATE").fetchone()
                if not lock or lock["owner"] != self.owner or lock["expires_at"] < self.clock():
                    return ("normal", None, {})
                c.execute("DELETE FROM reply_resource_slots WHERE expires_at<%s", (time.time(),))
                active = c.execute(
                    "SELECT count(*) FROM reply_resource_slots WHERE resource='active'"
                ).fetchone()[0]
                available = min(
                    capacity,
                    task_limit - active,
                )
                if available <= 0:
                    return ("normal", None, {})
                private_replies.synchronize(c, self.clock())
                rows = c.execute(
                    "SELECT * FROM tasks t WHERE state='queued' AND NOT EXISTS(SELECT 1 FROM chat_history_deletions d WHERE d.account_id=t.account_id AND d.conversation_id=t.conversation_id AND d.state<>'complete') ORDER BY created_at,ordinal"
                ).fetchall()
                busy = {
                    (r[0], r[1])
                    for r in c.execute(
                        "SELECT account_id,conversation_id FROM tasks WHERE state IN ('generating','preparing','ready','sending')"
                    )
                }
                busy.update((t["account_id"], t["conversation_id"]) for t in self.running.values())
                for index, row in enumerate(rows):
                    if (row["account_id"], row["conversation_id"]) in busy:
                        continue
                    if dialogue_windows.waiting(c, row, self.clock()):
                        continue
                    if private_replies.waiting(c, row, self.clock()) and (not self.check(dict(row), c)):
                        continue
                    reply_queue.freeze(
                        c, dict(row), [dict(r) for r in rows[index:]], self.clock(), self.check
                    )
                    if not self.check(dict(row), c) and dialogue_windows.absorb_answered_pat(
                        c, dict(row), self.clock()
                    ):
                        continue
                    state = "generating"
                    c.execute(
                        "UPDATE tasks SET state=%s,lease_owner=%s,started_at=%s WHERE id=%s AND state='queued'",
                        (state, self.owner, self.clock(), row["id"]),
                    )
                    selected.append(private_replies.claim(c, {**dict(row), "_owner": self.owner}))
                    c.execute(
                        "INSERT INTO reply_resource_slots(resource,token,task_id,owner,expires_at) VALUES('active',%s,%s,%s,%s)",
                        (uuid.uuid4().hex, row["id"], self.owner, time.time() + 120),
                    )
                    busy.add((row["account_id"], row["conversation_id"]))
                    if len(selected) >= available:
                        break
            return ("normal", None, locals())

        _database_result = await database_call(_database_once_545)
        if background:
            for task in selected:
                future = asyncio.create_task(self.execute(task))
                self.running[future] = task
        elif selected:
            await asyncio.gather(*(self.execute(t) for t in selected))
        return len(selected)

    async def run(self):
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()
        registered = False
        try:
            loop.add_signal_handler(signal.SIGTERM, current.cancel)
            registered = True
        except NotImplementedError:
            pass

        async def each(operation, *args, **kwargs):
            users = await database_call(self.store.query, "SELECT id FROM users ORDER BY id")
            for user in users:
                with user_scope(user["id"]):
                    await database_call(self.lease)
                    try:
                        await operation(*args, **kwargs)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await database_call(self.store.audit, "tenant_cycle_failed")

        async def heartbeat():
            while True:
                await database_call(self.lease)
                await asyncio.sleep(10)

        await database_call(self.lease)
        heart = asyncio.create_task(heartbeat())

        async def inbox():
            while True:
                try:
                    await each(database_call, self.manager.process_events, now=self.clock())
                except Exception:
                    pass
                await asyncio.sleep(0.1)

        ingestion = asyncio.create_task(inbox())

        async def discover():
            while True:
                try:
                    await each(self.discover_once)
                except Exception:
                    pass
                await asyncio.sleep(1)

        discovery = asyncio.create_task(discover())

        async def contacts():
            while True:
                try:
                    await each(self.contacts_once)
                    await each(self.contact_profiles_once)
                    await each(self.profiles.once)
                except Exception:
                    pass
                await asyncio.sleep(1)

        statistics = asyncio.create_task(contacts())

        async def participant_profiles():
            while True:
                try:
                    await each(self.reply_profiles.once)
                except Exception:
                    pass
                await asyncio.sleep(0.2)

        participants = asyncio.create_task(participant_profiles())

        async def discover_contacts():
            while True:
                try:
                    await database_call(self.lease)
                    await each(self.contact_discovery.once)
                except Exception:
                    pass
                await asyncio.sleep(0.2)

        contact_discovery = asyncio.create_task(discover_contacts())

        async def recover_contacts():
            while True:
                try:
                    await each(self.monitor_contacts_once)
                    await each(self.verify_contacts_once)
                except Exception:
                    pass
                await asyncio.sleep(1)

        relationships = asyncio.create_task(recover_contacts())

        try:
            while True:
                if heart.done():
                    await heart
                try:
                    await each(self.once, background=True)
                except Exception:
                    # 不输出携带凭据或聊天内容的异常对象；单次事务失败不丢失收件箱。
                    pass
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            for future in self.running:
                future.cancel()
            await asyncio.gather(*self.running, return_exceptions=True)
            self.running.clear()
            heart.cancel()
            ingestion.cancel()
            discovery.cancel()
            statistics.cancel()
            participants.cancel()
            contact_discovery.cancel()
            relationships.cancel()
            await asyncio.gather(
                heart,
                ingestion,
                discovery,
                statistics,
                participants,
                contact_discovery,
                relationships,
                return_exceptions=True,
            )
            self.release()
            await self.gateway.close()
            if registered:
                loop.remove_signal_handler(signal.SIGTERM)
