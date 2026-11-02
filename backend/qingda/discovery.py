"""回调候选身份异步核实；复用终端映射，不在回调请求内访问上游。"""

import asyncio
import json

from .accounts import AccountService
from .database_async import database_call
from .providers import ProviderError


def enqueue(conn, event, now):
    existing = conn.execute("SELECT * FROM account_discovery WHERE app_id=%s", (event["app_id"],)).fetchone()
    if existing and existing["wxid"] != event["wxid"]:
        return False
    if not existing:
        conn.execute(
            "INSERT INTO account_discovery(app_id,wxid,next_at,last_event_id,updated_at) VALUES(%s,%s,%s,%s,%s)",
            (event["app_id"], event["wxid"], now, event["id"], now),
        )
    elif (
        existing["state"] == "paused"
        and now >= existing["next_at"]
        and event["id"] > existing["last_event_id"]
    ):
        conn.execute(
            "UPDATE account_discovery SET state='pending',attempts=0,next_at=%s,last_event_id=%s,updated_at=%s,reason='' WHERE app_id=%s",
            (now, event["id"], now, event["app_id"]),
        )
    return True


class AccountDiscovery:
    def __init__(self, manager, gateway, owner, clock):
        self.manager, self.store, self.clock, self.owner = manager, manager.store, clock, owner
        self.accounts = AccountService(manager, gateway)
        self.gateway = gateway
        self.lock = asyncio.Lock()

    def leased(self, conn):
        return conn.execute(
            "SELECT 1 FROM worker_lock WHERE owner=%s AND expires_at>%s", (self.owner, self.clock())
        ).fetchone()

    async def once(self):
        async with self.lock:

            def _database_once_45():
                with self.store.transaction() as conn:
                    if not self.leased(conn):
                        return ("return", False, locals())
                    row = conn.execute(
                        "SELECT d.*,t.account_id,t.verified,t.operation_id,t.login_state,a.epoch,a.archived_at FROM account_discovery d JOIN terminals t ON t.app_id=d.app_id JOIN accounts a ON a.id=t.account_id WHERE d.state='pending' AND d.next_at<=%s ORDER BY d.next_at,d.last_event_id LIMIT 1",
                        (self.clock(),),
                    ).fetchone()
                    if not row:
                        return ("return", False, locals())
                    candidate = dict(row)
                    if row["verified"] or row["archived_at"] is not None:
                        conn.execute(
                            "UPDATE account_discovery SET state='blocked',reason='account_unavailable' WHERE app_id=%s",
                            (row["app_id"],),
                        )
                        return ("return", True, locals())
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_45)
            if "candidate" in _database_result[2]:
                candidate = _database_result[2]["candidate"]
            if _database_result[0] == "return":
                return _database_result[1]
            try:
                profile = await self.accounts.read_profile(candidate["app_id"])
                if profile["wxid"] != candidate["wxid"]:
                    raise ProviderError("discovery_identity_mismatch")
            except Exception as error:

                def _database_once_66(error):
                    with self.store.transaction() as conn:
                        if not self.leased(conn):
                            return ("return", True, locals())
                        attempts = candidate["attempts"] + 1
                        mismatch = (
                            isinstance(error, ProviderError) and error.reason == "discovery_identity_mismatch"
                        )
                        delays = (30, 120, 600)
                        state = "blocked" if mismatch else "paused" if attempts >= 4 else "pending"
                        delay = delays[attempts - 1] if attempts < 4 else 1800
                        conn.execute(
                            "UPDATE account_discovery SET state=%s,attempts=%s,next_at=%s,updated_at=%s,reason=%s WHERE app_id=%s AND state='pending'",
                            (
                                state,
                                attempts,
                                self.clock() + delay,
                                self.clock(),
                                "identity_mismatch" if mismatch else "lookup_failed",
                                candidate["app_id"],
                            ),
                        )
                    return ("normal", None, locals())

                _database_result = await database_call(_database_once_66, error)
                if _database_result[0] == "return":
                    return _database_result[1]
                return True
            try:
                online = await self.gateway.online(candidate["app_id"])
            except Exception:
                online = None

            def _database_once_92():
                with self.store.transaction() as conn:
                    current = conn.execute(
                        "SELECT t.*,a.epoch,a.archived_at FROM terminals t JOIN accounts a ON a.id=t.account_id WHERE app_id=%s",
                        (candidate["app_id"],),
                    ).fetchone()
                    if (
                        not self.leased(conn)
                        or not current
                        or current["verified"]
                        or (current["archived_at"] is not None)
                        or (current["account_id"] != candidate["account_id"])
                        or (current["epoch"] != candidate["epoch"])
                        or (current["operation_id"] != candidate["operation_id"])
                    ):
                        return ("return", True, locals())
                    archived = conn.execute(
                        "SELECT archived_at FROM accounts WHERE wxid=%s", (profile["wxid"],)
                    ).fetchone()
                    if archived and archived[0] is not None:
                        conn.execute(
                            "UPDATE account_discovery SET state='blocked',reason='account_archived' WHERE app_id=%s",
                            (candidate["app_id"],),
                        )
                        return ("return", True, locals())
                    account = self.manager.verify_identity(candidate["app_id"], profile, conn)
                    conn.execute(
                        "UPDATE accounts SET profile_json=%s,online=%s,online_checked_at=%s,last_successful_refresh_at=coalesce(%s,last_successful_refresh_at) WHERE id=%s",
                        (
                            json.dumps(profile, ensure_ascii=False),
                            online,
                            self.clock(),
                            self.clock() if online is not None else None,
                            account["id"],
                        ),
                    )
                    conn.execute(
                        "UPDATE terminals SET login_state=%s WHERE app_id=%s",
                        (
                            "logged_in" if online else "offline" if online is False else "unknown",
                            candidate["app_id"],
                        ),
                    )
                    conn.execute(
                        "UPDATE account_discovery SET state='done',reason='',updated_at=%s WHERE app_id=%s",
                        (self.clock(), candidate["app_id"]),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_92)
            if _database_result[0] == "return":
                return _database_result[1]
            return True
