"""可信回调发现对象；持久核实后发布到通讯录，不改变发送权限。"""

import asyncio
import json

from .contact_changes import enqueue_verification, mark_removed, record_evidence, system_relationship
from .contacts import SYSTEM_CONTACTS, publish_directory, snapshot
from .database_async import database_call
from .providers import ProviderError
from .store import load_event, string


def identity(conn, aid, peer):
    current = snapshot(conn, aid, require_online=False)
    if not current:
        return None
    removal = conn.execute(
        "SELECT version,restored_at FROM contact_removals WHERE account_id=%s AND peer_id=%s", (aid, peer)
    ).fetchone()
    return {
        **current,
        "removal_version": removal[0] if removal else 0,
        "removed": bool(removal and removal[1] is None),
    }


def enqueue(conn, aid, peer, now, event_id=None, source="callback"):
    current = identity(conn, aid, peer)
    if (
        not current
        or not peer
        or peer in SYSTEM_CONTACTS
        or peer == current["wxid"]
        or peer.startswith("gh_")
        or len(peer) > 200
        or any(c.isspace() or ord(c) < 32 for c in peer)
    ):
        return
    # 删除后的恢复必须经过原来的完整关系核实流程。
    if current["removed"]:
        enqueue_verification(conn, aid, peer, now)
        return
    old = conn.execute(
        "SELECT * FROM contact_discoveries WHERE account_id=%s AND peer_id=%s", (aid, peer)
    ).fetchone()
    same = old and json.loads(old["identity_json"]) == current
    if same and old["state"] in ("queued", "running"):
        return
    entry = conn.execute(
        "SELECT owner_wxid FROM contact_entries WHERE account_id=%s AND peer_id=%s", (aid, peer)
    ).fetchone()
    if same and old["state"] == "ready" and entry and entry[0] == current["wxid"]:
        return
    if same and (event_id is None or old["event_id"] == event_id or old["next_at"] > now):
        return
    conn.execute(
        "INSERT INTO contact_discoveries(account_id,peer_id,identity_json,event_id,source,next_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,account_id,peer_id) DO UPDATE SET identity_json=excluded.identity_json,generation=contact_discoveries.generation+1,event_id=excluded.event_id,source=excluded.source,state='queued',next_at=excluded.next_at,attempts=0,owner=NULL,lease_until=NULL,reason=''",
        (aid, peer, json.dumps(current, sort_keys=True), event_id, source, now),
    )


def group_members(result):
    data = result.get("data") if isinstance(result, dict) else None
    members = data.get("memberList") if isinstance(data, dict) else None
    if (
        not isinstance(result, dict)
        or result.get("ret") != 200
        or not isinstance(members, list)
        or not members
        or any(
            not isinstance(m, dict)
            or not isinstance(m.get("wxid"), str)
            or not m["wxid"].strip()
            or m["wxid"] != m["wxid"].strip()
            for m in members
        )
        or any(
            data.get(k)
            for k in ("hasMore", "has_more", "nextPage", "next_page", "continueFlag", "isCache", "is_cache")
        )
    ):
        raise ProviderError("invalid_members_response")
    return {m["wxid"] for m in members}


class ContactDiscovery:
    def __init__(self, manager, gateway, owner, clock, profiles):
        self.manager, self.store, self.gateway = manager, manager.store, gateway
        self.owner, self.clock, self.profiles = owner, clock, profiles
        self.lock = asyncio.Lock()

    def leased(self, conn):
        return conn.execute(
            "SELECT 1 FROM worker_lock WHERE owner=%s AND expires_at>%s", (self.owner, self.clock())
        ).fetchone()

    def seed(self, conn):
        for account in conn.execute(
            "SELECT id,wxid FROM accounts WHERE archived_at IS NULL AND online=1 AND wxid IS NOT NULL"
        ).fetchall():
            aid, wxid = account
            if (
                not snapshot(conn, aid)
                or conn.execute(
                    "SELECT 1 FROM contact_discovery_seeds WHERE account_id=%s AND wxid=%s", (aid, wxid)
                ).fetchone()
            ):
                continue
            peers = conn.execute(
                "SELECT peer_id FROM contact_entries WHERE account_id=%s AND kind='group' AND owner_wxid=%s UNION SELECT peer_id FROM conversations WHERE account_id=%s AND kind='group' UNION SELECT peer_id FROM chat_profiles WHERE account_id=%s AND peer_id LIKE '%%@chatroom'",
                (aid, wxid, aid, aid),
            ).fetchall()
            for (peer,) in peers:
                current = identity(conn, aid, peer)
                if current and not current["removed"]:
                    enqueue(conn, aid, peer, self.clock(), source="known_group")
            conn.execute("INSERT INTO contact_discovery_seeds VALUES(%s,%s)", (aid, wxid))

    def current(self, conn, job):
        row = conn.execute(
            "SELECT * FROM contact_discoveries WHERE account_id=%s AND peer_id=%s",
            (job["account_id"], job["peer_id"]),
        ).fetchone()
        return bool(
            self.leased(conn)
            and row
            and row["state"] == "running"
            and row["owner"] == self.owner
            and row["generation"] == job["generation"]
            and snapshot(conn, job["account_id"])
            and identity(conn, job["account_id"], job["peer_id"]) == json.loads(job["identity_json"])
            and not self.pending_change(conn, job)
        )

    def pending_change(self, conn, job):
        for row in conn.execute(
            "SELECT * FROM events WHERE app_id=%s AND wxid=%s AND origin='live' AND processed=0 AND parse_error IS NULL AND event_type IN ('DelContacts','ModContacts','AddMsg') AND id>%s",
            (job["identity"]["app_id"], job["identity"]["wxid"], job["watermark"]),
        ):
            event = load_event(conn, row)
            data = event.get("Data")
            if (
                row["event_type"] in ("DelContacts", "ModContacts")
                and isinstance(data, dict)
                and string(data.get("UserName")) == job["peer_id"]
            ):
                return True
            if (
                row["event_type"] == "AddMsg"
                and job["peer_id"] in (row["sender"], row["recipient"])
                and system_relationship(event, job["identity"]["wxid"])
            ):
                return True
        return False

    async def once(self):
        async with self.lock:

            def _database_once_159():
                with self.store.transaction() as conn:
                    if not self.leased(conn):
                        return ("return", False, locals())
                    self.seed(conn)
                    row = conn.execute(
                        "SELECT * FROM contact_discoveries WHERE (state IN ('queued','failed') AND next_at<=%s) OR (state='running' AND (owner!=%s OR lease_until<=%s)) ORDER BY next_at,account_id,peer_id LIMIT 1",
                        (self.clock(), self.owner, self.clock()),
                    ).fetchone()
                    if not row:
                        return ("return", False, locals())
                    job = dict(row)
                    job["identity"] = json.loads(job["identity_json"])
                    current = identity(conn, job["account_id"], job["peer_id"])
                    if current is None or not snapshot(conn, job["account_id"]):
                        conn.execute(
                            "UPDATE contact_discoveries SET state='queued',next_at=%s,owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                            (self.clock() + 5, job["account_id"], job["peer_id"]),
                        )
                        return ("return", True, locals())
                    if current != job["identity"]:
                        conn.execute(
                            "UPDATE contact_discoveries SET state='superseded',owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                            (job["account_id"], job["peer_id"]),
                        )
                        if current and (not current["removed"]):
                            enqueue(
                                conn,
                                job["account_id"],
                                job["peer_id"],
                                self.clock(),
                                job["event_id"],
                                job["source"],
                            )
                        return ("return", True, locals())
                    started = self.clock()
                    job["watermark"] = conn.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
                    conn.execute(
                        "UPDATE contact_discoveries SET state='running',attempted_at=%s,owner=%s,lease_until=%s WHERE account_id=%s AND peer_id=%s",
                        (started, self.owner, started + 40, job["account_id"], job["peer_id"]),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_159)
            if "current" in _database_result[2]:
                current = _database_result[2]["current"]
            if "job" in _database_result[2]:
                job = _database_result[2]["job"]
            if "started" in _database_result[2]:
                started = _database_result[2]["started"]
            if _database_result[0] == "return":
                return _database_result[1]
            group = job["peer_id"].endswith("@chatroom")
            present, reason, relation = None, "", None
            try:
                if group:
                    response = await self.gateway.call(
                        "/group/getChatroomMemberList",
                        {"appId": current["app_id"], "chatroomId": job["peer_id"]},
                        timeout=30,
                    )
                    present = current["wxid"] in group_members(response)
                else:
                    response = await self.gateway.call(
                        "/contacts/checkRelation",
                        {"appId": current["app_id"], "wxids": [job["peer_id"]]},
                        timeout=15,
                    )
                    values = response.get("data") if isinstance(response, dict) else None
                    matches = (
                        [v for v in values if isinstance(v, dict) and v.get("wxid") == job["peer_id"]]
                        if isinstance(values, list)
                        else []
                    )
                    if (
                        not isinstance(response, dict)
                        or response.get("ret") != 200
                        or len(matches) != 1
                        or type(matches[0].get("relation")) is not int
                    ):
                        raise ProviderError("invalid_relation_response")
                    relation = matches[0]["relation"]
                    if relation != 0:
                        raise ProviderError("relation_pending")
                    present = True
            except ProviderError as error:
                reason = error.reason
            except Exception:
                reason = "contact_discovery_failed"

            def _database_once_236():
                with self.store.transaction() as conn:
                    if not self.current(conn, job):
                        conn.execute(
                            "UPDATE contact_discoveries SET state='queued',next_at=%s,owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s AND generation=%s AND owner=%s",
                            (self.clock(), job["account_id"], job["peer_id"], job["generation"], self.owner),
                        )
                        return ("return", True, locals())
                    evidence_id = record_evidence(
                        conn,
                        current,
                        job["peer_id"],
                        "group_membership" if group else "contact_discovery_friend",
                        started,
                        self.clock(),
                        {
                            "valid": present is not None,
                            "present": present,
                            "error": bool(reason),
                            "relation": relation,
                        },
                        job["event_id"],
                    )
                    if present:
                        from .automation_defaults import confirmed

                        if confirmed(self.manager, conn, job["account_id"], job["peer_id"]):
                            from .contact_changes import monitor_result

                            monitor_result(
                                conn,
                                {**current, "peer_id": job["peer_id"], "attempted_at": started},
                                self.clock(),
                                "verified",
                                "",
                                evidence_id,
                            )
                        cached = conn.execute(
                            "SELECT profile_json FROM chat_profiles WHERE account_id=%s AND peer_id=%s",
                            (job["account_id"], job["peer_id"]),
                        ).fetchone()
                        profile = json.loads(cached[0]) if cached else {}
                        name = (
                            profile.get("remark", "").strip()
                            or profile.get("nickName", "").strip()
                            or job["peer_id"]
                        )
                        conn.execute(
                            "INSERT INTO contact_entries(account_id,peer_id,kind,display_name,profile_json,sort_key,owner_wxid,saved_to_contacts,confirmed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,account_id,peer_id) DO UPDATE SET owner_wxid=excluded.owner_wxid,confirmed_at=excluded.confirmed_at",
                            (
                                job["account_id"],
                                job["peer_id"],
                                "group" if group else "friend",
                                name,
                                json.dumps(profile),
                                name.casefold(),
                                current["wxid"],
                                int(not group),
                                self.clock(),
                            ),
                        )
                        conn.execute(
                            "UPDATE account_contacts SET friend_count=(SELECT count(*) FROM contact_entries WHERE account_id=%s AND kind='friend' AND owner_wxid=%s) WHERE account_id=%s AND wxid=%s AND list_updated_at IS NOT NULL",
                            (job["account_id"], current["wxid"], job["account_id"], current["wxid"]),
                        )
                        from .callbacks import refresh_greetings

                        publish_directory(conn, job["account_id"], self.clock())
                        refresh_greetings(conn, job["account_id"], job["peer_id"])
                        self.profiles.queue(conn, job["account_id"], job["peer_id"])
                        conn.execute(
                            "UPDATE contact_discoveries SET state='ready',reason='',confirmed_at=%s,owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                            (self.clock(), job["account_id"], job["peer_id"]),
                        )
                    elif present is False:
                        account = self.manager.account(job["account_id"], conn)
                        mark_removed(
                            conn, account, job["peer_id"], self.clock(), "group_left", evidence_id=evidence_id
                        )
                        conn.execute(
                            "UPDATE contact_discoveries SET state='absent',reason='group_left',owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                            (job["account_id"], job["peer_id"]),
                        )
                    else:
                        attempt = job["attempts"] + 1
                        delay = max(60 if relation == 5 else 2, min(600, 2 ** min(attempt, 10)))
                        conn.execute(
                            "UPDATE contact_discoveries SET state='failed',reason=%s,attempts=%s,next_at=%s,owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                            (reason, attempt, self.clock() + delay, job["account_id"], job["peer_id"]),
                        )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_236)
            if _database_result[0] == "return":
                return _database_result[1]
            return True
