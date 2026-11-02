import json
import time
import uuid
from contextlib import nullcontext

from .contact_changes import (
    RELATIONSHIP_COLUMNS,
    apply_system_relationship,
    decorate_monitor,
    enqueue_verification,
    remove_contact,
    require_active_contact,
    trusted_peer,
    verification,
)
from .discovery import enqueue
from .model_settings import DEFAULT_MODEL as DEFAULT_MODEL
from .model_settings import EDITABLE_FIELDS, effective_model, validate_model
from .protocol import normalize
from .store import load_event, string, task_record


class Manager:
    def __init__(self, store, vault=None, clock=time.time):
        self.store, self.vault, self.clock = store, vault, clock

    def account(self, account_id, conn=None):
        if conn is None:
            with self.store.connect() as c:
                return self.account(account_id, c)
        row = conn.execute("SELECT * FROM accounts WHERE id=%s", (account_id,)).fetchone()
        if not row:
            raise ValueError("账号不存在")
        result = dict(row)
        result.update(self.automation_summary(account_id, conn))
        from .owner_profile import decorate

        decorate(self, result, conn)
        return result

    def automation_summary(self, account_id, conn):
        now = self.clock()
        count, until = conn.execute(
            "SELECT count(*),min(c.pause_until) FROM conversations c WHERE c.account_id=%s AND c.pause_until>%s AND c.allowed=1 AND NOT EXISTS (SELECT 1 FROM contact_removals r WHERE r.account_id=c.account_id AND r.peer_id=c.peer_id AND r.restored_at IS NULL)",
            (account_id, now),
        ).fetchone()
        return {"paused_conversation_count": count, "next_pause_until": until, "server_time": now}

    def accounts(self, view=None):
        where = {
            "active": " WHERE archived_at IS NULL AND wxid IS NOT NULL",
            "archived": " WHERE archived_at IS NOT NULL",
        }.get(view, "")
        rows = self.store.query("SELECT * FROM accounts" + where + " ORDER BY enabled DESC,id")
        for row in rows:
            with self.store.connect() as conn:
                row.update(self.automation_summary(row["id"], conn))
                from .owner_profile import decorate

                decorate(self, row, conn)
            row["profile"] = json.loads(row.pop("profile_json"))
            stats = self.store.query(
                "SELECT friend_count,chatroom_count,updated_at,state FROM account_contacts WHERE account_id=%s",
                (row["id"],),
            )
            row["contacts"] = (
                stats[0]
                if stats
                else {"friend_count": None, "chatroom_count": None, "updated_at": None, "state": "idle"}
            )
            row["terminals"] = self.store.query(
                "SELECT app_id,verified,login_state,created_at FROM terminals WHERE account_id=%s",
                (row["id"],),
            )
            current = next((t for t in row["terminals"] if t["app_id"] == row["current_app_id"]), {})
            row["login_state"] = current.get("login_state", "unknown")
            config = self.store.query(
                "SELECT config_json FROM terminals WHERE app_id=%s", (row["current_app_id"],)
            )
            saved = json.loads(config[0]["config_json"]) if config else {}
            row["login_config"] = {
                k: saved[k] for k in ("device_type", "region_id", "use_proxy") if k in saved
            }
            row["login_config"]["proxy_configured"] = bool(saved.get("proxy_cipher"))
        return rows

    def add_account(self, app_id, remark="", conn=None):
        app_id = app_id.strip()
        if not app_id or len(app_id) > 200 or any(x.isspace() for x in app_id):
            raise ValueError("appId 格式无效")
        if conn is None:
            with self.store.transaction() as c:
                return self.add_account(app_id, remark, c)
        row = conn.execute("SELECT account_id FROM terminals WHERE app_id=%s", (app_id,)).fetchone()
        if row:
            return self.account(row[0], conn)
        now = self.clock()
        aid = conn.execute(
            "INSERT INTO accounts(remark,current_app_id,created_at) VALUES(%s,%s,%s) RETURNING id",
            (remark[:200], app_id, now),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO terminals(app_id,account_id,created_at) VALUES(%s,%s,%s)", (app_id, aid, now)
        )
        self.store.audit("account_imported", aid, conn=conn)
        return self.account(aid, conn)

    def verify_identity(self, app_id, profile, conn=None):
        wxid = profile.get("wxid")
        if not isinstance(wxid, str) or not wxid:
            raise ValueError("平台没有返回可靠的微信身份")
        with self.store.transaction() if conn is None else nullcontext(conn) as c:
            old = self.add_account(app_id, conn=c)
            if old["wxid"] and old["wxid"] != wxid:
                raise ValueError("设备对应的微信身份发生变化，请核对后使用原账号重新扫码")
            existing = c.execute("SELECT id FROM accounts WHERE wxid=%s", (wxid,)).fetchone()
            if existing and existing[0] != old["id"]:
                # 原终端及会话历史永久保留；其发送能力停用。跨终端去重使用验证后的 wxid。
                self.stop_account(old["id"], "identity_linked", c)
                c.execute("UPDATE accounts SET archived_at=%s WHERE id=%s", (self.clock(), old["id"]))
                c.execute(
                    "UPDATE terminals SET account_id=%s,verified=1 WHERE app_id=%s", (existing[0], app_id)
                )
                aid = existing[0]
                # 确认同一微信身份后，将该终端的可派生历史接入稳定账号。
                # 冲突的原消息仍留在停用档案，原始事件与来源映射永不删除。
                for old_conv in c.execute(
                    "SELECT * FROM conversations WHERE account_id=%s", (old["id"],)
                ).fetchall():
                    canonical = self.ensure_conversation(aid, old_conv["peer_id"], c)
                    c.execute(
                        "UPDATE conversations SET pause_until=greatest(pause_until,%s),ai_since=%s,epoch=epoch+1 WHERE id=%s",
                        (old_conv["pause_until"], self.clock(), canonical["id"]),
                    )
                    for message in c.execute(
                        "SELECT * FROM messages WHERE conversation_id=%s", (old_conv["id"],)
                    ).fetchall():
                        duplicate = (
                            message["platform_id"]
                            and c.execute(
                                "SELECT 1 FROM messages WHERE account_id=%s AND platform_id=%s",
                                (aid, message["platform_id"]),
                            ).fetchone()
                        )
                        if not duplicate:
                            c.execute(
                                "UPDATE message_records SET account_id=%s,conversation_id=%s WHERE id=%s",
                                (aid, canonical["id"], message["id"]),
                            )
                    c.execute(
                        "UPDATE tasks SET account_id=%s,conversation_id=%s WHERE conversation_id=%s",
                        (aid, canonical["id"], old_conv["id"]),
                    )
                c.execute(
                    "UPDATE accounts SET ai_enabled=0,ai_since=%s,epoch=epoch+1 WHERE id=%s",
                    (self.clock(), aid),
                )
                self.cancel_auto(c, "identity_linked", account_id=aid)
            else:
                aid = old["id"]
                c.execute(
                    "UPDATE accounts SET wxid=%s,nickname=%s,enabled=%s WHERE id=%s",
                    (
                        wxid,
                        (profile.get("nickName") or "")[:200],
                        old["enabled"] if old["wxid"] else int(old["archived_at"] is None),
                        aid,
                    ),
                )
                c.execute("UPDATE terminals SET verified=1 WHERE app_id=%s", (app_id,))
            c.execute("UPDATE accounts SET current_app_id=%s WHERE id=%s", (app_id, aid))
            self.store.audit("identity_verified", aid, conn=c)
            from .automation_defaults import initialize, initialize_prompt

            initialize_prompt(self, c, aid)
            initialize(self, c, aid)
            return self.account(aid, c)

    def cancel_auto(self, conn, reason, account_id=None, conversation_id=None):
        field, value = (
            ("conversation_id", conversation_id)
            if conversation_id is not None
            else ("account_id", account_id)
        )
        where, args = (f" AND {field}=%s", (value,)) if value is not None else ("", ())
        conn.execute(
            "UPDATE dialogue_windows SET closed_reason=%s WHERE closed_reason=''" + where, (reason, *args)
        )
        conn.execute(
            "UPDATE reply_input_waits SET state='closed',closed_reason=%s WHERE state<>'closed'" + where,
            (reason, *args),
        )
        conn.execute(
            "UPDATE tasks SET state='canceled',reason=%s,updated_at=%s WHERE kind='auto' AND state IN ('queued','generating','ready','preparing')"
            + where,
            (reason, self.clock(), *args),
        )

    def stop_account(self, aid, reason, conn):
        self.account(aid, conn)
        conn.execute(
            "UPDATE accounts SET enabled=0,ai_enabled=0,epoch=epoch+1,ai_since=%s WHERE id=%s",
            (self.clock(), aid),
        )
        conn.execute(
            "UPDATE tasks SET state='canceled',reason=%s,updated_at=%s WHERE account_id=%s AND state IN ('queued','generating','ready','preparing')",
            (reason, self.clock(), aid),
        )

    def configure_model(self, changes, api_key=None):
        if set(changes) - EDITABLE_FIELDS:
            raise ValueError("模型配置包含未知字段")
        validate_model(changes)
        with self.store.transaction() as c:
            model = effective_model({**self.store.setting("model", {}, c), **changes})
            validate_model(model)
            self.store.set_setting("model", model, c)
            self.store.set_setting("model_revision", self.store.setting("model_revision", 0, c) + 1, c)
            if api_key:
                if not self.vault:
                    raise ValueError("密钥保存不可用")
                self.store.set_setting("model_key", self.vault.encrypt(api_key), c)
            self.cancel_auto(c, "model_changed")
            self.store.audit(
                "model_updated",
                detail={
                    "model": model["model"],
                    "thinking_enabled": model["thinking_enabled"],
                    "web_search_enabled": model["web_search_enabled"],
                },
                conn=c,
            )
        return model

    def configure_asr(self, api_key):
        from .speech import validate_key

        api_key = validate_key(api_key)
        if not api_key:
            return
        if not self.vault:
            raise ValueError("密钥保存不可用")
        with self.store.transaction() as c:
            c.execute("SELECT id FROM users WHERE id=current_setting('app.user_id')::bigint FOR UPDATE")
            # 相同密钥回执保持幂等，不重复取消任务。
            saved = self.store.setting("asr_key", None, c)
            if saved and self.vault.decrypt(saved) == api_key:
                return
            self.store.set_setting("asr_key", self.vault.encrypt(api_key), c)
            c.execute(
                "UPDATE tasks t SET state='canceled',reason='asr_key_changed',updated_at=%s "
                "WHERE kind='auto' AND state IN ('queued','generating','ready','preparing') AND ("
                "EXISTS(SELECT 1 FROM messages m WHERE m.id=t.message_id AND m.msg_type='34') OR "
                "EXISTS(SELECT 1 FROM reply_media_dependencies d JOIN messages m ON m.id=d.source_message_id "
                "WHERE d.task_id=t.id AND m.msg_type='34') OR "
                "EXISTS(SELECT 1 FROM reply_batch_items i JOIN tasks member ON member.id=i.task_id "
                "JOIN messages m ON m.id=member.message_id WHERE i.batch_id=t.id AND m.msg_type='34'))",
                (self.clock(),),
            )
            self.store.audit("asr_key_updated", conn=c)

    def configure_automation(self, aid, prompt=None, ai_enabled=None, conn=None):
        resume_paused = ai_enabled is True
        prompt_only = ai_enabled is None
        with self.store.transaction() if conn is None else nullcontext(conn) as c:
            a = self.account(aid, c)
            if ai_enabled is not None:
                c.execute("UPDATE account_automation_state SET manual_ai=1 WHERE account_id=%s", (aid,))
            prompt = a["prompt"] if prompt is None else prompt
            ai_enabled = bool(a["ai_enabled"]) if ai_enabled is None else ai_enabled
            if ai_enabled and not prompt.strip():
                raise ValueError("启用前请填写账号提示词")
            if ai_enabled and self.vault and not self.store.setting("model_key", None, c):
                raise ValueError("请先保存模型 Key")
            if a["archived_at"] is not None:
                raise ValueError("账号已移除，请重新扫码登录后恢复")
            if ai_enabled and not a["enabled"]:
                raise ValueError("账号未启用")
            if prompt_only and prompt == (a["prompt"] or ""):
                return a
            c.execute(
                "UPDATE accounts SET prompt=%s,ai_enabled=%s,ai_since=%s,epoch=epoch+1 WHERE id=%s",
                (prompt, bool(ai_enabled), self.clock(), aid),
            )
            self.cancel_auto(c, "account_ai_changed", account_id=aid)
            if resume_paused:
                now = self.clock()
                c.execute(
                    "UPDATE conversations SET pause_until=0,ai_since=%s,epoch=epoch+1 WHERE account_id=%s AND pause_until>%s",
                    (now, aid, now),
                )
                self.store.audit("account_pauses_resumed", aid, conn=c)
            self.store.audit("account_ai_changed", aid, {"enabled": bool(ai_enabled)}, c)
        return self.account(aid, conn)

    def ensure_conversation(self, aid, peer, conn=None):
        if not peer:
            raise ValueError("会话身份缺失")
        if conn is None:
            with self.store.connect() as c:
                return self.ensure_conversation(aid, peer, c)
        from .chat_visibility import chat_visible

        own_wxid = conn.execute("SELECT wxid FROM accounts WHERE id=%s", (aid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO conversations(account_id,peer_id,kind,allowed,updated_at) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                aid,
                peer,
                "group" if peer.endswith("@chatroom") else "private",
                int(chat_visible(peer, own_wxid)),
                self.clock(),
            ),
        )
        cid = conn.execute(
            "SELECT id FROM conversations WHERE account_id=%s AND peer_id=%s", (aid, peer)
        ).fetchone()[0]
        return self.conversation(cid, conn)

    def conversation(self, cid, conn=None):
        if conn is None:
            with self.store.connect() as c:
                return self.conversation(cid, c)
        row = conn.execute(
            "SELECT c.*,"
            + RELATIONSHIP_COLUMNS
            + " FROM conversations c LEFT JOIN contact_removals r ON r.account_id=c.account_id AND r.peer_id=c.peer_id WHERE c.id=%s",
            (cid,),
        ).fetchone()
        if not row:
            raise ValueError("会话不存在")
        return decorate_monitor(conn, dict(row), self.clock())

    def set_auto_reply(self, cid, enabled):
        with self.store.transaction() as conn:
            from .automation_defaults import remember_choice

            remember_choice(conn, cid, enabled)
            conv = self.conversation(cid, conn)
            require_active_contact(conv)
            now = self.clock()
            conn.execute(
                "UPDATE conversations SET allowed=%s,pause_until=0,ai_since=%s,epoch=epoch+1 WHERE id=%s",
                (bool(enabled), now, cid),
            )
            self.cancel_auto(
                conn, "resumed_without_replay" if enabled else "allowlist_changed", conversation_id=cid
            )
            self.store.audit(
                "conversation_auto_reply_changed",
                conv["account_id"],
                {"conversation_id": cid, "enabled": bool(enabled)},
                conn,
            )
            return self.conversation(cid, conn)

    def takeover(self, cid, conn, now=None):
        now = self.clock() if now is None else now
        conn.execute(
            "UPDATE conversations SET pause_until=%s,ai_since=%s,epoch=epoch+1 WHERE id=%s",
            (now + 1800, now + 1800, cid),
        )
        self.cancel_auto(conn, "human_takeover", conversation_id=cid)
        self.store.audit("human_takeover", detail={"conversation_id": cid}, conn=conn)

    def create_task(
        self,
        conn,
        kind,
        account,
        conv,
        message_id=None,
        state="queued",
        reason="",
        content="",
        request_id=None,
        now=None,
    ):
        now = self.clock() if now is None else now
        tid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO tasks(id,kind,account_id,conversation_id,message_id,request_id,state,reason,content,\n          account_epoch,conversation_epoch,config_version,created_at,updated_at)\n          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tid,
                kind,
                account["id"],
                conv["id"],
                message_id,
                request_id,
                state,
                reason,
                content,
                account["epoch"],
                conv["epoch"],
                self.store.setting("model_revision", 0, conn),
                now,
                now,
            ),
        )
        task = task_record(conn.execute("SELECT * FROM tasks WHERE id=%s", (tid,)).fetchone())
        if kind == "auto" and state == "queued" and message_id:
            from .reply_profiles import enqueue

            enqueue(self, conn, task, now)
        return task

    def guard(self, account, conv, metadata, created, now, conn, *, allow_old=False):
        if conv["removed_at"] is not None:
            return "contact_removed"
        if account["archived_at"] is not None:
            return "account_removed"
        if not account["enabled"]:
            return "account_disabled"
        if not account["ai_enabled"]:
            return "ai_disabled"
        if not conv["allowed"]:
            return "not_allowed"
        if metadata.get("history"):
            return "historical_sync"
        if metadata.get("test"):
            return "platform_test"
        from .emoji_policy import CUSTOM

        if metadata.get("message_kind") == "emoji":
            return CUSTOM
        if metadata.get("message_kind") == "video":
            return "video_disabled"
        if metadata.get("auto_reply_exclusion"):
            return metadata["auto_reply_exclusion"]
        if not created or created > now + 10:
            return "unreliable_time"
        if conv["pause_until"] > now:
            return "paused"
        if created < max(account["ai_since"], conv["ai_since"]):
            return "before_watermark"
        if now - created > 300 and not allow_old:
            return "expired"
        from .callbacks import revoked
        from .group_wakeup import awakened

        if metadata.get("window_source_id"):
            source = conn.execute(
                "SELECT CASE WHEN msg_type='47' THEN 'custom_emoji_disabled' WHEN msg_type IN ('43','62') THEN 'video_disabled' ELSE metadata->>'auto_reply_exclusion' END FROM message_records WHERE id=%s AND msg_type IN ('3','47','43','62')",
                (metadata["window_source_id"],),
            ).fetchone()
            if source and source[0]:
                return "emoji_wakeup_disabled"
        if metadata.get("window_source_id") and revoked(conn, metadata["window_source_id"]):
            return "wake_revoked"
        if (
            metadata.get("wakeup_reason") == "wait_continuation"
            and not conn.execute(
                "SELECT 1 FROM reply_input_waits WHERE session_id=%s AND account_id=%s AND conversation_id=%s "
                "AND account_epoch=%s AND conversation_epoch=%s AND owner_wxid=%s AND app_id=%s "
                "AND (state='releasing' OR (state='waiting' AND expires_at>%s))",
                (
                    metadata.get("wait_session_id"),
                    account["id"],
                    conv["id"],
                    account["epoch"],
                    conv["epoch"],
                    account["wxid"],
                    account["current_app_id"],
                    now,
                ),
            ).fetchone()
        ):
            return "input_wait_ended"
        if metadata.get("reference_task_id"):
            from .reply_references import assistant_reply

            referenced = assistant_reply(conn, account["id"], conv["id"], metadata.get("reference_id"), now)
            if not referenced or referenced["id"] != metadata["reference_task_id"]:
                return "reference_unavailable"
        if (
            metadata.get("wakeup_reason") == "continuation"
            and conn.execute(
                "SELECT 1 FROM dialogue_windows WHERE account_id=%s AND conversation_id=%s AND source_mid=%s AND closed_reason='social_close'",
                (account["id"], conv["id"], metadata.get("window_source_id")),
            ).fetchone()
        ):
            return "dialogue_closed"
        if conv["kind"] == "group" and not awakened(metadata):
            return "not_explicitly_mentioned"
        if not account["prompt"].strip():
            return "missing_prompt"
        if verification(conn, conv, now)["state"] not in ("verified", "excluded"):
            return "relationship_unverified"
        return ""

    def process_events(self, limit=50, now=None):
        from .callbacks import cancel_revoked_batches, process_versions, project

        now = self.clock() if now is None else now
        processed = 0
        for item in self.store.query(
            "SELECT id FROM events WHERE processed=0 ORDER BY received_at,id LIMIT %s", (limit,)
        ):
            with self.store.transaction() as c:
                row = c.execute("SELECT * FROM events WHERE id=%s AND processed=0", (item["id"],)).fetchone()
                if not row:
                    continue
                c.execute("UPDATE events SET processed=1 WHERE id=%s", (row["id"],))
                processed += 1
                if row["parse_error"] or not row["app_id"]:
                    continue
                try:
                    if row["app_id"] != row["app_id"].strip():
                        raise ValueError("回调 appId 不能包含首尾空白")
                    a = self.add_account(row["app_id"], conn=c)
                except ValueError:
                    c.execute("UPDATE events SET parse_error='invalid_app_id' WHERE id=%s", (row["id"],))
                    continue
                candidate = c.execute(
                    "SELECT wxid FROM account_discovery WHERE app_id=%s", (row["app_id"],)
                ).fetchone()
                expected_wxid = a["wxid"] or (candidate[0] if candidate else None)
                if row["wxid"] and expected_wxid and row["wxid"] != expected_wxid:
                    c.execute(
                        "UPDATE events SET parse_error='account_identity_mismatch' WHERE id=%s", (row["id"],)
                    )
                    continue
                event = load_event(c, row)
                if row["event_type"] == "DelContacts":
                    remove_contact(c, a, row, event, now)
                    continue
                if row["event_type"] == "ModContacts":
                    data = event.get("Data")

                    peer = string(data.get("UserName")) if isinstance(data, dict) else None
                    if trusted_peer(c, a, row, peer, require_wxid=True) and not any(
                        data.get(k)
                        for k in ("IsHistory", "isHistory", "is_history", "History", "IsSync", "IsTest")
                    ):
                        from .callbacks import contact_profile

                        contact_profile(c, a, data, now)
                        enqueue_verification(c, a["id"], peer, now)
                        from .contact_discovery import enqueue as discover_contact

                        discover_contact(c, a["id"], peer, now, row["id"])
                    continue
                if (
                    row["event_type"] == "Offline"
                    and row["origin"] == "live"
                    and row["app_id"] == a["current_app_id"]
                    and row["wxid"] in (None, a["wxid"])
                    and c.execute(
                        "SELECT 1 FROM terminals WHERE app_id=%s AND account_id=%s AND verified=1",
                        (row["app_id"], a["id"]),
                    ).fetchone()
                ):
                    c.execute("UPDATE accounts SET online=0,online_checked_at=%s WHERE id=%s", (now, a["id"]))
                    self.cancel_auto(c, "platform_offline", account_id=a["id"])
                if row["event_type"] != "AddMsg":
                    continue
                msg = normalize(event)
                if not msg["peer"]:
                    continue
                if not a["wxid"] and row["origin"] == "live":
                    if (
                        not row["wxid"]
                        or not row["wxid"].strip()
                        or len(row["wxid"]) > 200
                        or any(char.isspace() for char in row["wxid"])
                    ):
                        continue
                    if msg["metadata"]["history"] or msg["metadata"]["test"] or not row["message_id"]:
                        continue
                    if not enqueue(c, row, now):
                        c.execute(
                            "UPDATE events SET parse_error='account_identity_mismatch' WHERE id=%s",
                            (row["id"],),
                        )
                        continue
                from .history_identity import next_id, seen

                if row["message_id"] and seen(c, "message", a["id"], row["message_id"]):
                    continue
                conv = self.ensure_conversation(a["id"], msg["peer"], c)
                relationship_reason = apply_system_relationship(c, a, row, event, msg, now)
                if relationship_reason:
                    conv = self.conversation(conv["id"], c)
                from .group_wakeup import annotate

                annotate(c, a, row, event, msg)
                from . import dialogue_windows, input_waits

                input_waits.annotate(self, c, a, conv, msg, now)
                dialogue_windows.annotate(c, a, conv, msg, now)
                if row["content_id"]:
                    c.execute(
                        "UPDATE message_contents SET text=%s WHERE id=%s", (msg["text"], row["content_id"])
                    )
                # 未验证事件中的 Wxid 不能授权发送，也不能改变已验证账号身份。
                try:
                    cursor = c.execute(
                        "INSERT INTO message_records(id,event_id,account_id,conversation_id,platform_id,direction,\n                        source,msg_type,content_id,sender,created_at,received_at,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                        (
                            next_id(c, "messages"),
                            row["id"],
                            a["id"],
                            conv["id"],
                            row["message_id"],
                            "outbound" if msg["outgoing"] else "inbound",
                            "phone" if msg["outgoing"] else row["origin"],
                            msg["msg_type"],
                            row["content_id"],
                            msg["sender"],
                            msg["created_at"] or row["received_at"],
                            row["received_at"],
                            json.dumps(msg["metadata"]),
                        ),
                    )
                except (TypeError, ValueError):
                    continue
                if not cursor.rowcount:
                    if msg["outgoing"] and row["message_id"]:
                        c.execute(
                            "UPDATE message_records SET event_id=coalesce(event_id,%s) WHERE account_id=%s AND platform_id=%s AND outbound_task_id IS NOT NULL",
                            (row["id"], a["id"], row["message_id"]),
                        )
                    continue
                if (
                    not relationship_reason
                    and not msg["metadata"]["history"]
                    and not msg["metadata"]["test"]
                    and msg["created_at"]
                    and 0 <= now - msg["created_at"] <= 300
                    and trusted_peer(c, a, row, msg["peer"], require_wxid=True)
                ):
                    enqueue_verification(c, a["id"], msg["peer"], now)
                    from .contact_discovery import enqueue as discover_contact

                    discover_contact(c, a["id"], msg["peer"], now, row["id"])
                mid = cursor.fetchone()[0]
                stored = c.execute("SELECT * FROM messages WHERE id=%s", (mid,)).fetchone()
                from .media_references import remember

                remember(c, stored, event)
                project(c, a, row, event, stored)
                cancel_revoked_batches(c, now)
                if c.execute(
                    "SELECT 1 FROM message_views WHERE key=('m:' || lpad((%s)::text,20,'0')) AND (revoked=1 OR visible=0)",
                    (mid,),
                ).fetchone():
                    continue
                c.execute(
                    "UPDATE conversations SET updated_at=greatest(updated_at,%s) WHERE id=%s",
                    (row["received_at"], conv["id"]),
                )
                if row["origin"] != "live" or not row["message_id"]:
                    continue
                if msg["outgoing"] and msg["msg_type"] not in (
                    "1",
                    "3",
                    "34",
                    "43",
                    "47",
                    "48",
                    "49",
                    "42",
                    "62",
                ):
                    continue
                verified = c.execute(
                    "SELECT verified FROM terminals WHERE app_id=%s", (row["app_id"],)
                ).fetchone()[0]
                if not verified or a["wxid"] != msg["me"]:
                    reason = "unverified_identity"
                elif msg["outgoing"]:
                    if (
                        msg["msg_type"] in ("1", "3", "34", "43", "47", "48", "49", "42", "62")
                        and msg["created_at"]
                        and 0 <= now - msg["created_at"] <= 300
                        and not msg["metadata"]["history"]
                        and not msg["metadata"]["test"]
                    ):
                        self.takeover(conv["id"], c, now)
                    reason = "self_message"
                else:
                    if msg["metadata"]["message_kind"] not in (
                        "text",
                        "image",
                        "voice",
                        "emoji",
                        "video",
                        "pat",
                    ):
                        continue
                    reason = self.guard(a, conv, msg["metadata"], msg["created_at"], now, c)
                # 同一 wxid 的不同 appId 可能投递同一消息；以确认身份跨终端核对。
                if (
                    a["wxid"]
                    and c.execute(
                        "SELECT 1 FROM tasks t JOIN messages m ON m.id=t.message_id\n                    JOIN accounts ac ON ac.id=t.account_id WHERE t.kind='auto' AND m.platform_id=%s\n                    AND ac.wxid=%s LIMIT 1",
                        (row["message_id"], a["wxid"]),
                    ).fetchone()
                ):
                    reason = "duplicate_logical_message"
                task = self.create_task(
                    c, "auto", a, conv, mid, "skipped" if reason else "queued", reason, now=now
                )
                dialogue_windows.register(self, c, a, conv, msg, task, now)
                from .private_replies import register as register_private

                register_private(c, task, now)
        process_versions(self, limit)
        with self.store.transaction() as c:
            cancel_revoked_batches(c, now)
        return processed

    def context(self, task, batch=None):
        from .reply_queue import members

        if batch is None:
            with self.store.connect() as conn:
                batch = members(conn, task)
        from .private_context import build

        return build(self, task, batch)
