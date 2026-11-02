"""基础展示资料；回复专用的最小详情投影由reply_profiles维护。"""

import json
from urllib.parse import urlparse

from .accounts import public_profile
from .database_async import database_call


def media_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    host = parsed.hostname or ""
    if (
        parsed.scheme in ("http", "https")
        and not parsed.username
        and not parsed.password
        and any(host == domain or host.endswith("." + domain) for domain in ("qlogo.cn", "qpic.cn"))
    ):
        return parsed._replace(scheme="https").geturl()
    return None


def profile_value(value):
    result = public_profile(value)
    for key in (
        "remark",
        "nickName",
        "displayName",
        "description",
        "signature",
        "quanPin",
        "pyInitial",
        "remarkQuanPin",
        "remarkPyInitial",
    ):
        if isinstance(value.get(key), str):
            result[key] = value[key][:32000]
    for key in ("smallHeadImgUrl", "bigHeadImgUrl"):
        url = media_url(value.get(key))
        if url:
            result[key] = url
    return result


def merge_profile(base, addition):
    # 保留基础资料部分更新的语义，空备注可以清除。
    result = dict(base)
    for key, value in addition.items():
        if value is not None and (value or key in ("remark", "displayName", "sex")):
            result[key] = value
    return result


class ContactProfiles:
    def __init__(self, manager, gateway):
        self.manager, self.store, self.gateway = manager, manager.store, gateway

    def queue(self, conn, aid, peer):
        conn.execute(
            "INSERT INTO chat_profiles(account_id,peer_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (aid, peer)
        )
        conn.execute(
            "UPDATE chat_profiles SET state='queued' WHERE account_id=%s AND peer_id=%s AND (updated_at IS NULL OR updated_at<%s)",
            (aid, peer, self.manager.clock() - 600),
        )

    async def once(self):
        now = self.manager.clock()

        def _database_once_70():
            with self.store.transaction() as conn:
                row = conn.execute(
                    "SELECT p.*,a.current_app_id,a.epoch FROM chat_profiles p JOIN accounts a ON a.id=p.account_id WHERE (p.state='queued' OR (p.state='running' AND p.lease_until<%s)) AND a.enabled=1 AND a.archived_at IS NULL ORDER BY coalesce(p.attempted_at,0) LIMIT 1",
                    (now,),
                ).fetchone()
                if not row:
                    job = None
                else:
                    job = dict(row)
                    conn.execute(
                        "UPDATE chat_profiles SET state='running',attempted_at=%s,lease_until=%s WHERE account_id=%s AND peer_id=%s",
                        (now, now + 120, job["account_id"], job["peer_id"]),
                    )
            return ("normal", None, locals())

        _database_result = await database_call(_database_once_70)
        if "job" in _database_result[2]:
            job = _database_result[2]["job"]
        if job is None:
            return False
        aid, peer = job["account_id"], job["peer_id"]
        try:
            group = peer.endswith("@chatroom")
            result = await self.gateway.call(
                "/group/getChatroomInfo" if group else "/contacts/getDetailInfo",
                {"appId": job["current_app_id"], **({"chatroomId": peer} if group else {"wxids": [peer]})},
            )
            data = result.get("data")
            if not group and isinstance(data, list):
                data = next((v for v in data if v.get("userName", v.get("wxid")) == peer), None)
            if result.get("ret") != 200 or not isinstance(data, dict):
                raise ValueError("invalid_profile")
            profile = profile_value(data)
            if not profile.get("nickName"):
                raise ValueError("invalid_profile")
            members = data.get("memberList") if group else None
            if group and (
                not isinstance(members, list)
                or any(not isinstance(m, dict) or not (m.get("wxid") or m.get("userName")) for m in members)
            ):
                raise ValueError("invalid_members")

            def _database_once_106():
                with self.store.transaction() as conn:
                    account = self.manager.account(aid, conn)
                    if account["epoch"] != job["epoch"] or account["current_app_id"] != job["current_app_id"]:
                        conn.execute(
                            "UPDATE chat_profiles SET state='queued' WHERE account_id=%s AND peer_id=%s",
                            (aid, peer),
                        )
                        return ("return", True, locals())
                    for m in members or []:
                        mid = m.get("wxid") or m.get("userName")
                        old = conn.execute(
                            "SELECT profile_json FROM chat_member_profiles WHERE account_id=%s AND peer_id=%s AND member_id=%s",
                            (aid, peer, mid),
                        ).fetchone()
                        addition = profile_value(m)
                        if "displayName" in m and m["displayName"] is None:
                            addition["displayName"] = ""
                        merged = merge_profile(json.loads(old[0]) if old else {}, addition)
                        conn.execute(
                            "INSERT INTO chat_member_profiles VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,account_id,peer_id,member_id) DO UPDATE SET profile_json=excluded.profile_json,updated_at=excluded.updated_at",
                            (aid, peer, mid, json.dumps(merged), self.manager.clock()),
                        )
                    conn.execute(
                        "UPDATE chat_profiles SET profile_json=%s,member_count=%s,updated_at=%s,state='ready',lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                        (
                            json.dumps(profile),
                            len({m.get("wxid") or m.get("userName") for m in members}) if group else None,
                            self.manager.clock(),
                            aid,
                            peer,
                        ),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_106)
            if _database_result[0] == "return":
                return _database_result[1]
        except Exception:

            def _database_once_139():
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE chat_profiles SET state='failed',lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                        (aid, peer),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_139)
        return True
