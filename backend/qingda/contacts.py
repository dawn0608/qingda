"""自动通讯录清单、数量与资料补齐；慢查询独立于消息任务。"""

import asyncio
import json

from .database_async import database_call
from .providers import ProviderError

SYSTEM_CONTACTS = {
    "fmessage",
    "tmessage",
    "medianote",
    "qmessage",
    "qqmail",
    "qqsafe",
    "weixin",
    "exmail_tool",
    "filehelper",
}
TASK_TIMEOUT = 90
SYNC_INTERVAL = 600


def contact_lists(result, wxid):
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(result, dict) or result.get("ret") != 200 or not isinstance(data, dict):
        raise ProviderError("contacts_unavailable")
    if any(
        data.get(key) or result.get(key)
        for key in ("hasMore", "has_more", "nextPage", "next_page", "continueFlag")
    ):
        raise ProviderError("incomplete_contacts_response")
    for key in ("friends", "chatrooms"):
        if not isinstance(data.get(key), list) or any(
            not isinstance(value, str) or not value.strip() or value != value.strip() for value in data[key]
        ):
            raise ProviderError("invalid_contacts_response")
    friends = {
        value
        for value in data["friends"]
        if value != wxid
        and value not in SYSTEM_CONTACTS
        and not value.startswith("gh_")
        and not value.endswith("@chatroom")
    }
    rooms = {value for value in data["chatrooms"] if value.endswith("@chatroom")}
    if len(rooms) != len(set(data["chatrooms"])):
        raise ProviderError("invalid_contacts_response")
    return sorted(friends), sorted(rooms)


def contact_counts(result, wxid):
    friends, rooms = contact_lists(result, wxid)
    return len(friends), len(rooms)


def snapshot(conn, aid, *, require_online=True):
    row = conn.execute(
        "SELECT a.id AS account_id,a.current_app_id AS app_id,a.wxid,a.epoch,t.operation_id FROM accounts a JOIN terminals t ON t.app_id=a.current_app_id AND t.account_id=a.id WHERE a.id=%s AND a.archived_at IS NULL AND a.wxid IS NOT NULL AND (%s=0 OR a.online=1) AND t.verified=1",
        (aid, int(require_online)),
    ).fetchone()
    return dict(row) if row else None


SNAPSHOT_FIELDS = ("account_id", "app_id", "wxid", "epoch", "operation_id")


def visible_contact(alias="e"):
    """新条目绑定微信身份；升级前条目仅在同身份的有效清单中可见。"""
    return f"(({alias}.owner_wxid=%s AND ({alias}.confirmed_at IS NOT NULL OR EXISTS(SELECT 1 FROM account_contacts s WHERE s.account_id={alias}.account_id AND s.wxid={alias}.owner_wxid AND s.list_updated_at IS NOT NULL))) OR ({alias}.owner_wxid='' AND EXISTS(SELECT 1 FROM account_contacts s WHERE s.account_id={alias}.account_id AND s.wxid=%s AND s.list_updated_at IS NOT NULL)))"


def publish_directory(conn, aid, now):
    """补齐回复范围；新条目默认开启，旧开关与人工暂停不变。"""
    from .chat_visibility import visibility_sql

    current = snapshot(conn, aid, require_online=False)
    if not current:
        return []
    visible, excluded = visibility_sql("e.peer_id", "a.wxid")
    source = " FROM contact_entries e JOIN accounts a ON a.id=e.account_id "
    where = (
        " WHERE e.account_id=%s AND e.kind IN ('friend','group') AND "
        + visible_contact()
        + " AND "
        + visible
        + " AND (e.kind!='group' OR e.saved_to_contacts=1 OR e.confirmed_at IS NOT NULL) "
        "AND NOT EXISTS(SELECT 1 FROM contact_removals r WHERE r.account_id=e.account_id "
        "AND r.peer_id=e.peer_id AND r.restored_at IS NULL) "
    )
    args = (aid, current["wxid"], current["wxid"], *excluded)
    rows = conn.execute(
        "INSERT INTO conversations(account_id,peer_id,kind,allowed,ai_since,updated_at) "
        "SELECT e.account_id,e.peer_id,CASE WHEN e.kind='group' THEN 'group' ELSE 'private' END,1,%s,%s "
        + source
        + where
        + " ON CONFLICT(user_id,account_id,peer_id) DO NOTHING RETURNING id",
        (now, now, *args),
    ).fetchall()
    # 保存初始范围及已有选择，后续默认初始化不得覆盖手动关闭。
    conn.execute(
        "INSERT INTO conversation_preferences(conversation_id,enabled) SELECT c.id,c.allowed "
        + source
        + " JOIN conversations c ON c.account_id=e.account_id AND c.peer_id=e.peer_id "
        + where
        + " ON CONFLICT(user_id,conversation_id) DO NOTHING",
        args,
    )
    return [row[0] for row in rows]


def enqueue_contacts(conn, aid, now, *, force=False):
    current = snapshot(conn, aid)
    if not current:
        return
    old = conn.execute("SELECT * FROM account_contacts WHERE account_id=%s", (aid,)).fetchone()
    if old:
        same = all(old[key] == current[key] for key in SNAPSHOT_FIELDS)
        if same and old["state"] in ("queued", "running"):
            return
        if (
            same
            and not force
            and old["list_updated_at"] is not None
            and now - old["list_updated_at"] < SYNC_INTERVAL
        ):
            return
        if (
            same
            and not force
            and old["attempted_at"] is not None
            and now - old["attempted_at"] < SYNC_INTERVAL
        ):
            return
    conn.execute(
        "INSERT INTO account_contacts(account_id,app_id,wxid,epoch,operation_id) VALUES(%s,%s,%s,%s,%s) "
        "ON CONFLICT(user_id,account_id) DO UPDATE SET "
        "full_attempted_at=CASE WHEN account_contacts.app_id IS DISTINCT FROM excluded.app_id "
        "OR account_contacts.wxid IS DISTINCT FROM excluded.wxid OR account_contacts.operation_id IS DISTINCT FROM excluded.operation_id "
        "THEN NULL ELSE account_contacts.full_attempted_at END,"
        "app_id=excluded.app_id,wxid=excluded.wxid,epoch=excluded.epoch,operation_id=excluded.operation_id,"
        "state='queued',owner=NULL,lease_until=NULL,reason=''",
        tuple(current[key] for key in SNAPSHOT_FIELDS),
    )


class ContactStatistics:
    def __init__(self, manager, gateway, owner, clock, recovery):
        self.manager = manager
        self.store, self.gateway, self.owner, self.clock = manager.store, gateway, owner, clock
        self.recovery = recovery
        self.lock = asyncio.Lock()
        self.profiles_lock = asyncio.Lock()
        self.cache_normalized = False

    def leased(self, conn):
        return conn.execute(
            "SELECT 1 FROM worker_lock WHERE owner=%s AND expires_at>%s", (self.owner, self.clock())
        ).fetchone()

    def current(self, conn, job):
        current = snapshot(conn, job["account_id"])
        row = conn.execute(
            "SELECT owner,state FROM account_contacts WHERE account_id=%s", (job["account_id"],)
        ).fetchone()
        return bool(
            current
            and row
            and row["owner"] == self.owner
            and row["state"] == "running"
            and self.leased(conn)
            and all(current[key] == job[key] for key in SNAPSHOT_FIELDS)
        )

    async def cached(self, job):
        result = await self.gateway.call(
            "/contacts/fetchContactsListCache", {"appId": job["app_id"]}, timeout=5
        )
        return contact_lists(result, job["wxid"])

    def schedule(self, conn):
        """领导 Worker 自动安排到期账号；不依赖网页或新消息。"""
        row = conn.execute(
            "SELECT a.id FROM accounts a JOIN terminals t ON t.app_id=a.current_app_id AND t.account_id=a.id "
            "LEFT JOIN account_contacts s ON s.account_id=a.id "
            "WHERE a.enabled=1 AND a.online=1 AND a.archived_at IS NULL AND a.wxid IS NOT NULL AND t.verified=1 "
            "AND (s.account_id IS NULL OR s.app_id IS DISTINCT FROM a.current_app_id OR s.wxid IS DISTINCT FROM a.wxid "
            "OR s.epoch IS DISTINCT FROM a.epoch OR s.operation_id IS DISTINCT FROM t.operation_id "
            "OR (s.state IN ('idle','failed') AND coalesce(s.attempted_at,0)<=%s AND coalesce(s.list_updated_at,0)<=%s)) "
            "ORDER BY coalesce(s.attempted_at,0),a.id LIMIT 1",
            (self.clock() - SYNC_INTERVAL, self.clock() - SYNC_INTERVAL),
        ).fetchone()
        if row:
            enqueue_contacts(conn, row[0], self.clock())

    async def lookup(self, job):
        job["fresh_started_at"] = None

        def claim_full_pull():
            with self.store.transaction() as conn:
                if not self.current(conn, job):
                    raise ProviderError("contacts_superseded")
                last = conn.execute(
                    "SELECT full_attempted_at FROM account_contacts WHERE account_id=%s", (job["account_id"],)
                ).fetchone()[0]
                if last is not None and self.clock() - last < SYNC_INTERVAL:
                    return False
                conn.execute(
                    "UPDATE account_contacts SET full_attempted_at=%s WHERE account_id=%s",
                    (self.clock(), job["account_id"]),
                )
                return True

        if not await database_call(claim_full_pull):
            return await self.cached(job)
        try:
            started_at = self.clock()
            result = await self.gateway.call(
                "/contacts/fetchContactsList", {"appId": job["app_id"]}, timeout=30
            )
            # 部分平台先返回成功占位，真正结果随后写入缓存。
            if result.get("ret") == 200 and result.get("data") is None:
                raise ProviderError("contacts_pending", uncertain=True)
            lists = contact_lists(result, job["wxid"])
            from .contact_changes import fresh_friends, remember_friends

            try:
                friends = fresh_friends(result, job["wxid"])
            except ProviderError:
                pass
            else:
                job["fresh_started_at"] = started_at

                def remember():
                    with self.store.transaction() as conn:
                        if self.current(conn, job):
                            remember_friends(conn, job, friends, started_at, self.clock())
                            self.recovery.inherit_account(conn, job["account_id"])

                await database_call(remember)
            return lists
        except ProviderError as error:
            if not error.uncertain:
                raise
        # 外层 TASK_TIMEOUT 约束总时长；未知结果不重复发起全量请求。
        while True:
            try:
                return await self.cached(job)
            except ProviderError:
                await asyncio.sleep(5)

    async def once(self):
        async with self.lock:
            # 离线状态也由后台低频核对，恢复后沿用账号服务的身份校验与排队。
            def offline_account():
                with self.store.connect() as conn:
                    if not self.leased(conn):
                        return None
                    row = conn.execute(
                        "SELECT a.id FROM accounts a JOIN terminals t ON t.app_id=a.current_app_id AND t.account_id=a.id "
                        "WHERE a.enabled=1 AND a.archived_at IS NULL AND a.wxid IS NOT NULL AND t.verified=1 "
                        "AND a.online IS DISTINCT FROM 1 AND coalesce(a.online_checked_at,0)<=%s "
                        "ORDER BY coalesce(a.online_checked_at,0),a.id LIMIT 1",
                        (self.clock() - SYNC_INTERVAL,),
                    ).fetchone()
                    return row[0] if row else None

            offline = await database_call(offline_account)
            if offline is not None:
                from .accounts import AccountService

                try:
                    await AccountService(self.manager, self.gateway).online(offline)
                except (ProviderError, ValueError):
                    pass

            def _database_once_171():
                with self.store.transaction() as conn:
                    if not self.leased(conn):
                        return ("return", False, locals())
                    if not self.cache_normalized:
                        placeholders = ",".join(("%s" for _ in SYSTEM_CONTACTS))
                        affected = conn.execute(
                            f"SELECT DISTINCT account_id FROM contact_entries WHERE kind='friend' AND peer_id IN ({placeholders})",
                            tuple(SYSTEM_CONTACTS),
                        ).fetchall()
                        conn.execute(
                            f"DELETE FROM contact_entries WHERE kind='friend' AND peer_id IN ({placeholders})",
                            tuple(SYSTEM_CONTACTS),
                        )
                        for account in affected:
                            conn.execute(
                                "UPDATE account_contacts SET friend_count=(SELECT count(*) FROM contact_entries WHERE account_id=%s AND kind='friend') WHERE account_id=%s AND list_updated_at IS NOT NULL",
                                (account[0], account[0]),
                            )
                        self.cache_normalized = True
                    self.schedule(conn)
                    row = conn.execute(
                        "SELECT * FROM account_contacts WHERE state='queued' OR (state='running' AND (owner!=%s OR lease_until<=%s)) ORDER BY coalesce(attempted_at,0),account_id LIMIT 1",
                        (self.owner, self.clock()),
                    ).fetchone()
                    if not row:
                        return ("return", False, locals())
                    job = dict(row)
                    current = snapshot(conn, job["account_id"])
                    if not current or any((current[key] != job[key] for key in SNAPSHOT_FIELDS)):
                        conn.execute(
                            "UPDATE account_contacts SET state='failed',reason='contacts_superseded',owner=NULL,lease_until=NULL WHERE account_id=%s",
                            (job["account_id"],),
                        )
                        return ("return", True, locals())
                    conn.execute(
                        "UPDATE account_contacts SET state='running',attempted_at=%s,owner=%s,lease_until=%s,reason='' WHERE account_id=%s",
                        (self.clock(), self.owner, self.clock() + 100, job["account_id"]),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_171)
            if "_" in _database_result[2]:
                _ = _database_result[2]["_"]
            if "job" in _database_result[2]:
                job = _database_result[2]["job"]
            if _database_result[0] == "return":
                return _database_result[1]
            counts, reason = None, ""
            try:
                counts = await asyncio.wait_for(self.lookup(job), timeout=TASK_TIMEOUT)
            except (ProviderError, asyncio.TimeoutError) as error:
                reason = error.reason if isinstance(error, ProviderError) else "contacts_timeout"
            except Exception:
                reason = "contacts_lookup_failed"

            def _database_once_215():
                with self.store.transaction() as conn:
                    if self.current(conn, job):
                        if counts is not None:
                            friends, rooms = counts
                            removed = {
                                r[0]
                                for r in conn.execute(
                                    "SELECT peer_id FROM contact_removals WHERE account_id=%s AND restored_at IS NULL",
                                    (job["account_id"],),
                                )
                            }
                            friends = [peer for peer in friends if peer not in removed]
                            rooms = [peer for peer in rooms if peer not in removed]
                            wanted = set(friends + rooms)
                            existing = conn.execute(
                                "SELECT * FROM contact_entries WHERE account_id=%s", (job["account_id"],)
                            ).fetchall()
                            conn.cursor().executemany(
                                "DELETE FROM contact_entries WHERE account_id=%s AND peer_id=%s",
                                [
                                    (job["account_id"], r["peer_id"])
                                    for r in existing
                                    if r["peer_id"] not in wanted
                                    and (
                                        r["owner_wxid"] not in ("", job["wxid"])
                                        or (
                                            r["kind"] == "friend"
                                            and (
                                                job.get("fresh_started_at") is not None
                                                and (
                                                    r["confirmed_at"] is None
                                                    or r["confirmed_at"] <= job["fresh_started_at"]
                                                )
                                            )
                                        )
                                    )
                                ],
                            )
                            conn.cursor().executemany(
                                "UPDATE contact_entries SET saved_to_contacts=0 WHERE account_id=%s AND peer_id=%s",
                                [
                                    (job["account_id"], r["peer_id"])
                                    for r in existing
                                    if r["kind"] == "group"
                                    and r["peer_id"] not in wanted
                                    and (
                                        job.get("fresh_started_at") is not None
                                        and (
                                            r["confirmed_at"] is None
                                            or r["confirmed_at"] <= job["fresh_started_at"]
                                        )
                                    )
                                ],
                            )
                            conn.cursor().executemany(
                                "INSERT INTO contact_entries(account_id,peer_id,kind,display_name,sort_key,owner_wxid) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,account_id,peer_id) DO UPDATE SET kind=excluded.kind,profile_state='pending',owner_wxid=excluded.owner_wxid,saved_to_contacts=1",
                                [
                                    (job["account_id"], peer, kind, peer, peer.casefold(), job["wxid"])
                                    for kind, peers in (("friend", friends), ("group", rooms))
                                    for peer in peers
                                ],
                            )
                            conn.execute(
                                "UPDATE account_contacts SET friend_count=(SELECT count(*) FROM contact_entries WHERE account_id=%s AND kind='friend' AND owner_wxid=%s),chatroom_count=(SELECT count(*) FROM contact_entries WHERE account_id=%s AND kind='group' AND owner_wxid=%s AND saved_to_contacts=1),updated_at=%s,list_updated_at=%s,state='idle',owner=NULL,lease_until=NULL,reason='' WHERE account_id=%s",
                                (
                                    job["account_id"],
                                    job["wxid"],
                                    job["account_id"],
                                    job["wxid"],
                                    self.clock(),
                                    self.clock(),
                                    job["account_id"],
                                ),
                            )
                        else:
                            conn.execute(
                                "UPDATE account_contacts SET state='failed',reason=%s,attempted_at=%s,owner=NULL,lease_until=NULL WHERE account_id=%s",
                                (reason, self.clock(), job["account_id"]),
                            )
                        if counts is not None:
                            from .callbacks import refresh_greetings

                            publish_directory(conn, job["account_id"], self.clock())
                            refresh_greetings(conn, job["account_id"])
                            from .automation_defaults import seed_directory

                            seed_directory(self.manager, conn, job["account_id"])
                    elif self.leased(conn):
                        conn.execute(
                            "UPDATE account_contacts SET state='failed',reason='contacts_superseded',owner=NULL,lease_until=NULL WHERE account_id=%s AND owner=%s",
                            (job["account_id"], self.owner),
                        )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_215)
            return True

    async def enrich_once(self):
        from .contact_profiles import merge_profile, profile_value

        async with self.profiles_lock:

            def claim():
                with self.store.transaction() as conn:
                    if not self.leased(conn):
                        return None
                    row = conn.execute(
                        "SELECT s.* FROM account_contacts s JOIN accounts a ON a.id=s.account_id "
                        "JOIN terminals t ON t.app_id=a.current_app_id AND t.account_id=a.id "
                        "WHERE s.state IN ('idle','failed') AND s.list_updated_at IS NOT NULL "
                        "AND a.enabled=1 AND a.online=1 AND a.archived_at IS NULL AND t.verified=1 "
                        "AND a.current_app_id=s.app_id AND a.wxid=s.wxid AND a.epoch=s.epoch AND t.operation_id=s.operation_id "
                        "AND (s.profile_owner IS NULL OR s.profile_lease_until<=%s) "
                        "AND EXISTS(SELECT 1 FROM contact_entries e WHERE e.account_id=s.account_id AND e.owner_wxid=s.wxid "
                        "AND (e.profile_state='pending' OR (e.profile_state='failed' AND e.profile_attempted_at<=%s))) "
                        "ORDER BY s.list_updated_at,s.account_id LIMIT 1",
                        (self.clock(), self.clock() - SYNC_INTERVAL),
                    ).fetchone()
                    if not row:
                        return None
                    job = dict(row)
                    due = "account_id=%s AND owner_wxid=%s AND (profile_state='pending' OR (profile_state='failed' AND profile_attempted_at<=%s))"
                    args = (job["account_id"], job["wxid"], self.clock() - SYNC_INTERVAL)
                    first = conn.execute(
                        "SELECT kind FROM contact_entries WHERE "
                        + due
                        + " ORDER BY coalesce(profile_attempted_at,0),peer_id LIMIT 1",
                        args,
                    ).fetchone()
                    kind = first[0]
                    peers = [
                        r[0]
                        for r in conn.execute(
                            "SELECT peer_id FROM contact_entries WHERE " + due + " AND kind=%s "
                            "ORDER BY coalesce(profile_attempted_at,0),peer_id LIMIT %s",
                            (*args, kind, 1 if kind == "group" else 20),
                        )
                    ]
                    conn.execute(
                        "UPDATE account_contacts SET profile_owner=%s,profile_lease_until=%s WHERE account_id=%s",
                        (self.owner, self.clock() + 25, job["account_id"]),
                    )
                    conn.cursor().executemany(
                        "UPDATE contact_entries SET profile_state='pending',profile_attempted_at=%s WHERE account_id=%s AND peer_id=%s",
                        [(self.clock(), job["account_id"], peer) for peer in peers],
                    )
                    return job, peers, kind

            claimed = await database_call(claim)
            if not claimed:
                return False
            job, peers, kind = claimed
            profiles = {}
            try:
                group = kind == "group"
                result = await self.gateway.call(
                    "/group/getChatroomInfo" if group else "/contacts/getDetailInfo",
                    {"appId": job["app_id"], **({"chatroomId": peers[0]} if group else {"wxids": peers})},
                    timeout=15,
                )
                data = result.get("data")
                if result.get("ret") != 200 or not isinstance(data, dict if group else list):
                    raise ProviderError("invalid_contact_profiles")
                for value in [data] if group else data:
                    if not isinstance(value, dict):
                        continue
                    peer = value.get("chatroomId") if group else value.get("userName", value.get("wxid"))
                    if peer not in peers:
                        continue
                    profile = profile_value(value)
                    if profile.get("nickName") or profile.get("remark"):
                        profiles[peer] = profile
            except (ProviderError, asyncio.TimeoutError):
                pass

            def commit():
                with self.store.transaction() as conn:
                    current = snapshot(conn, job["account_id"])
                    saved = conn.execute(
                        "SELECT * FROM account_contacts WHERE account_id=%s", (job["account_id"],)
                    ).fetchone()
                    if (
                        not self.leased(conn)
                        or not current
                        or not saved
                        or saved["profile_owner"] != self.owner
                        or saved["profile_lease_until"] <= self.clock()
                    ):
                        return
                    if saved["list_updated_at"] != job["list_updated_at"] or any(
                        current[k] != job[k] for k in SNAPSHOT_FIELDS
                    ):
                        conn.execute(
                            "UPDATE account_contacts SET profile_owner=NULL,profile_lease_until=NULL WHERE account_id=%s AND profile_owner=%s",
                            (job["account_id"], self.owner),
                        )
                        return
                    for peer in peers:
                        previous = conn.execute(
                            "SELECT profile_json,owner_wxid FROM contact_entries WHERE account_id=%s AND peer_id=%s",
                            (job["account_id"], peer),
                        ).fetchone()
                        if not previous or previous["owner_wxid"] != job["wxid"]:
                            continue
                        profile = profiles.get(peer)
                        if not profile:
                            conn.execute(
                                "UPDATE contact_entries SET profile_state='failed',profile_attempted_at=%s WHERE account_id=%s AND peer_id=%s",
                                (self.clock(), job["account_id"], peer),
                            )
                            continue
                        profile = merge_profile(json.loads(previous["profile_json"]), profile)
                        name = (
                            profile.get("remark", "").strip() or profile.get("nickName", "").strip() or peer
                        )
                        pinyin = (
                            profile.get("remarkQuanPin") if profile.get("remark") else profile.get("quanPin")
                        ) or name
                        initial = pinyin[:1].upper()
                        group_key = initial if initial.isascii() and initial.isalpha() else "#"
                        conn.execute(
                            "UPDATE contact_entries SET profile_json=%s,display_name=%s,sort_key=%s,group_key=%s,profile_state='ready',profile_updated_at=%s,profile_attempted_at=%s WHERE account_id=%s AND peer_id=%s",
                            (
                                json.dumps(profile),
                                name,
                                pinyin.casefold(),
                                group_key,
                                self.clock(),
                                self.clock(),
                                job["account_id"],
                                peer,
                            ),
                        )
                    conn.execute(
                        "UPDATE account_contacts SET profile_owner=NULL,profile_lease_until=NULL WHERE account_id=%s AND profile_owner=%s",
                        (job["account_id"], self.owner),
                    )

            await database_call(commit)
            return True
