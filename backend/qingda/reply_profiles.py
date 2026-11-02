"""回复参与者的最小资料投影；持久按需查询，不占回复执行槽。"""

import asyncio
import hashlib
import json
import re
import uuid

from .contacts import snapshot
from .database_async import database_call

TTL = 600
VERSION = "participant-profiles-v1"
DETAIL = "/contacts/getDetailInfo"
MEMBER = "/group/getChatroomMemberDetail"
GROUP = "/group/getChatroomInfo"
KEY = "account_id=%s AND owner_wxid=%s AND conversation_id=%s AND member_id=%s"


def key(row):
    return tuple(row[k] for k in ("account_id", "owner_wxid", "conversation_id", "member_id"))


def clean(value, size):
    return re.sub(r"[\x00-\x1f\x7f]", "", value).strip()[:size] if isinstance(value, str) else ""


def normalized(value):
    # 完整详情响应形成新快照，缺失和非法性别均为未知，不继承历史猜测。
    sex = value.get("sex")
    if type(sex) is str and sex in ("0", "1", "2"):
        sex = int(sex)
    return {
        "nickname": clean(value.get("nickName"), 100),
        "gender": {1: "男", 2: "女"}.get(sex, "未知") if type(sex) is int else "未知",
        "profile_region": {k: clean(value.get(k), 80) for k in ("country", "province", "city")},
    }


def eligible(manager, conn, cid, now):
    conv = manager.conversation(cid, conn)
    current = snapshot(conn, conv["account_id"])
    if not current or conv["auto_reply_status"] != "enabled" or conv["pause_until"] > now:
        return None
    return {**current, "conversation_epoch": conv["epoch"], "peer_id": conv["peer_id"], "kind": conv["kind"]}


def referenced_member(manager, conn, msg):
    from .private_context import reference

    ref = reference(manager, conn, dict(msg))
    return (
        ref.get("sender")
        if ref and ref.get("speaker_type") == "member" and not ref.get("unavailable")
        else None
    )


def enqueue(manager, conn, task, now):
    if task["kind"] != "auto" or task["state"] != "queued":
        return
    current = eligible(manager, conn, task["conversation_id"], now)
    if not current:
        return
    msg = conn.execute("SELECT * FROM messages WHERE id=%s", (task["message_id"],)).fetchone()
    members = [msg["sender"]]
    if current["kind"] == "group":
        members.append(referenced_member(manager, conn, msg))
    for member in dict.fromkeys(members):
        if not member or member == current["wxid"] or member.endswith("@chatroom"):
            continue
        if current["kind"] == "private" and member != current["peer_id"]:
            continue
        values = (task["account_id"], current["wxid"], task["conversation_id"], member)
        cache = conn.execute(
            "SELECT updated_at,group_updated_at FROM reply_participant_profiles WHERE " + KEY, values
        ).fetchone()
        if cache and cache[0] > now - TTL and (current["kind"] != "group" or (cache[1] or 0) > now - TTL):
            continue
        previous = conn.execute("SELECT * FROM reply_profile_requests WHERE " + KEY, values).fetchone()
        same = previous and json.loads(previous["identity_json"]) == current
        if same and (previous["state"] in ("queued", "running") or previous["next_at"] > now):
            conn.execute("UPDATE reply_profile_requests SET source_mid=%s WHERE " + KEY, (msg["id"], *values))
            continue
        conn.execute(
            "INSERT INTO reply_profile_requests(account_id,owner_wxid,conversation_id,member_id,identity_json,source_mid,state,next_at) VALUES(%s,%s,%s,%s,%s,%s,'queued',%s) ON CONFLICT(user_id,account_id,owner_wxid,conversation_id,member_id) DO UPDATE SET identity_json=excluded.identity_json,source_mid=excluded.source_mid,state='queued',attempts=0,next_at=excluded.next_at,owner=NULL,lease_until=NULL,reason=''",
            (*values, json.dumps(current, sort_keys=True), msg["id"], now),
        )


def valid_request(manager, conn, job, now):
    current = eligible(manager, conn, job["conversation_id"], now)
    if not current or current != json.loads(job["identity_json"]):
        return False
    latest = conn.execute(
        "SELECT source_mid,identity_json FROM reply_profile_requests WHERE " + KEY, key(job)
    ).fetchone()
    if not latest or latest["identity_json"] != job["identity_json"]:
        return False
    msg = conn.execute("SELECT * FROM messages WHERE id=%s", (latest["source_mid"],)).fetchone()
    if not msg or msg["account_id"] != job["account_id"] or msg["conversation_id"] != job["conversation_id"]:
        return False
    from .callbacks import revoked

    if revoked(conn, msg["id"]):
        return False
    return job["member_id"] == msg["sender"] or (
        current["kind"] == "group" and job["member_id"] == referenced_member(manager, conn, msg)
    )


def indexed(values, requested):
    """缺项、重复和错配逐人失败；意外对象不落库。"""
    result, duplicates = {}, set()
    if not isinstance(values, list):
        return result
    for value in values:
        if not isinstance(value, dict):
            continue
        member = value.get("userName") or value.get("wxid")
        if not isinstance(member, str) or member not in requested:
            continue
        if member in result:
            duplicates.add(member)
        result[member] = value
    return {k: v for k, v in result.items() if k not in duplicates}


def context_profiles(manager, conn, task, current):
    account = manager.account(task["account_id"], conn)
    target = current[0]["reply_target"]
    members = [target]
    if current[0]["conversation_kind"] == "group":
        members += [
            v["reference"].get("sender")
            for v in current
            if v.get("reference", {}).get("speaker_type") == "member"
        ]
    result = {}
    for member in dict.fromkeys(members):
        if not member or member == account["wxid"] or len(result) >= 8:
            continue
        row = conn.execute(
            "SELECT * FROM reply_participant_profiles WHERE " + KEY,
            (account["id"], account["wxid"], task["conversation_id"], member),
        ).fetchone()
        value = {"nickname": "", "gender": "未知", "profile_region": {}, "freshness": "missing"}
        if row:
            value = {
                **json.loads(row["profile_json"]),
                "source": row["source"],
                "updated_at": row["updated_at"],
                "freshness": "fresh" if row["updated_at"] > manager.clock() - TTL else "stale",
            }
            if current[0]["conversation_kind"] == "group" and row["group_updated_at"] is not None:
                value.update(
                    group_nickname=row["group_name"] or "",
                    group_source=GROUP,
                    group_updated_at=row["group_updated_at"],
                    group_freshness="fresh" if row["group_updated_at"] > manager.clock() - TTL else "stale",
                )
        value["role"] = "reply_target" if member == target else "referenced_member"
        result[member] = value
    return result


def fingerprint(profiles):
    return hashlib.sha256(json.dumps(profiles, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check_weather_location(context, arguments):
    """所有会话逐字核对提问者地点证据，与资料缓存是否存在无关。"""
    from .reply_tools import ToolFailure

    target = next((v.get("reply_target", v.get("sender")) for v in context if v.get("current")), None)
    evidence = arguments.get("location_evidence", "")
    location = arguments.get("location", "")
    if isinstance(evidence, str) and 2 <= len(evidence.strip()) <= 80:
        evidence = evidence.strip()
        messages = [
            v
            for v in context
            if v.get("role") == "user"
            and v.get("current")
            and v.get("sender", target) == target
            and v.get("speaker_type", "member") == "member"
            and v.get("context_scope") != "background"
            and v.get("message_kind") != "pat"
        ]
        locations = [location.strip()]
        for suffix in ("特别行政区", "自治区", "自治州", "地区", "省", "市", "区", "县"):
            if location.endswith(suffix):
                stem = location[: -len(suffix)]
                if len(stem) >= 2:
                    locations.append(stem)
                break
        for message in messages:
            text = message.get("content", "")
            if not isinstance(text, str) or evidence.casefold() not in text.casefold():
                continue
            matched = (
                evidence
                if evidence.casefold() in location.casefold()
                else next((v for v in locations if len(v) >= 2 and v.casefold() in evidence.casefold()), None)
            )
            if matched:
                return {
                    "quote": evidence,
                    "matched_location": matched,
                    "message_id": message.get("message_id"),
                }
    raise ToolFailure(
        "location_confirmation_required",
        "地点证据未通过校验，尚未请求天气服务。若本轮提问者已提供地名，请纠正location及location_evidence后重新调用，例如重庆今天天气怎么样使用重庆，武汉呢使用武汉；确实缺地名才询问城市。证据须逐字取自本轮实际发言或相关候选，不能使用旧地点、资料、引用、他人或助手回复，不改用搜索猜地点。",
        stage="validation",
        request_sent=False,
    )


class ReplyProfiles:
    def __init__(self, manager, gateway, clock=None):
        self.manager, self.store, self.gateway = manager, manager.store, gateway
        self.clock = clock or manager.clock
        self.owner = str(uuid.uuid4())

    async def query(self, path, payload, jobs):
        def _database_query_209():
            with self.store.connect() as conn:
                clock = conn.execute("SELECT * FROM reply_profile_clock WHERE id=1").fetchone()
                if clock["owner"] != self.owner or clock["lease_until"] <= self.clock():
                    raise ValueError("profile_lease_lost")
                delay = max(0, clock["next_at"] - self.clock())
            return ("normal", None, locals())

        _database_result = await database_call(_database_query_209)
        if "delay" in _database_result[2]:
            delay = _database_result[2]["delay"]
        if delay:
            await asyncio.sleep(delay)

        def _database_query_216():
            with self.store.connect() as conn:
                if not all((valid_request(self.manager, conn, job, self.clock()) for job in jobs)):
                    raise ValueError("profile_identity_changed")
            return ("normal", None, locals())

        _database_result = await database_call(_database_query_216)
        try:
            return await self.gateway.call(path, payload, timeout=15)
        finally:

            def _database_query_222():
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE reply_profile_clock SET next_at=%s WHERE id=1 AND owner=%s",
                        (self.clock() + 1, self.owner),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_query_222)

    async def once(self):
        now = self.clock()

        def _database_once_230():
            with self.store.transaction() as conn:
                clock = conn.execute("SELECT * FROM reply_profile_clock WHERE id=1").fetchone()
                if clock["next_at"] > now or (clock["owner"] and clock["lease_until"] > now):
                    return ("return", False, locals())
                rows = conn.execute(
                    "SELECT * FROM reply_profile_requests WHERE (state='queued' OR (state='running' AND lease_until<=%s)) AND next_at<=%s ORDER BY next_at LIMIT 100",
                    (now, now),
                ).fetchall()
                jobs = []
                for row in rows:
                    job = dict(row)
                    if not valid_request(self.manager, conn, job, now):
                        conn.execute(
                            "UPDATE reply_profile_requests SET state='canceled',owner=NULL,lease_until=NULL,reason='context_changed' WHERE "
                            + KEY,
                            key(job),
                        )
                        continue
                    if job["attempts"] >= 3:
                        conn.execute(
                            "UPDATE reply_profile_requests SET state='failed',owner=NULL,lease_until=NULL,next_at=%s,reason='attempts_exhausted' WHERE "
                            + KEY,
                            (now + TTL, *key(job)),
                        )
                        continue
                    if jobs and (job["account_id"], job["conversation_id"], job["identity_json"]) != (
                        jobs[0]["account_id"],
                        jobs[0]["conversation_id"],
                        jobs[0]["identity_json"],
                    ):
                        continue
                    jobs.append(job)
                    conn.execute(
                        "UPDATE reply_profile_requests SET state='running',owner=%s,lease_until=%s,attempts=attempts+1 WHERE "
                        + KEY,
                        (self.owner, now + 120, *key(job)),
                    )
                    if len(jobs) == 20:
                        break
                if not jobs:
                    return ("return", False, locals())
                conn.execute(
                    "UPDATE reply_profile_clock SET owner=%s,lease_until=%s WHERE id=1",
                    (self.owner, now + 120),
                )
            return ("normal", None, locals())

        _database_result = await database_call(_database_once_230)
        if "jobs" in _database_result[2]:
            jobs = _database_result[2]["jobs"]
        if _database_result[0] == "return":
            return _database_result[1]
        identity = json.loads(jobs[0]["identity_json"])
        group = identity["kind"] == "group"
        requested = {j["member_id"] for j in jobs}
        details, names, group_ok = {}, {}, not group
        source = MEMBER if group else DETAIL
        try:
            payload = {
                "appId": identity["app_id"],
                **(
                    {"chatroomId": identity["peer_id"], "memberWxids": sorted(requested)}
                    if group
                    else {"wxids": sorted(requested)}
                ),
            }
            response = await self.query(source, payload, jobs)
            if type(response.get("ret")) is int and response["ret"] == 200:
                details = indexed(response.get("data"), requested)

            # 先到的昵称、性别立即可用，不等群昵称的第二次请求完成。
            def _database_once_292():
                with self.store.transaction() as conn:
                    for job in jobs:
                        row = conn.execute(
                            "SELECT owner FROM reply_profile_requests WHERE " + KEY, key(job)
                        ).fetchone()
                        detail = details.get(job["member_id"])
                        if (
                            row
                            and row["owner"] == self.owner
                            and (detail is not None)
                            and clean(detail.get("nickName"), 100)
                            and valid_request(self.manager, conn, job, self.clock())
                        ):
                            conn.execute(
                                "INSERT INTO reply_participant_profiles VALUES(%s,%s,%s,%s,%s,%s,%s,NULL,NULL) ON CONFLICT(user_id,account_id,owner_wxid,conversation_id,member_id) DO UPDATE SET profile_json=excluded.profile_json,source=excluded.source,updated_at=excluded.updated_at",
                                (
                                    *key(job),
                                    json.dumps(normalized(detail), ensure_ascii=False),
                                    source,
                                    self.clock(),
                                ),
                            )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_292)
            if group:
                response = await self.query(
                    GROUP, {"appId": identity["app_id"], "chatroomId": identity["peer_id"]}, jobs
                )
                data = response.get("data")
                if (
                    type(response.get("ret")) is int
                    and response["ret"] == 200
                    and isinstance(data, dict)
                    and data.get("chatroomId") == identity["peer_id"]
                ):
                    names = indexed(data.get("memberList"), requested)
                    group_ok = isinstance(data.get("memberList"), list)
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            # 不记录上游响应正文、标识或异常字符串。
        finally:

            def _database_once_332():
                with self.store.transaction() as conn:
                    for job in jobs:
                        row = conn.execute(
                            "SELECT * FROM reply_profile_requests WHERE " + KEY, key(job)
                        ).fetchone()
                        if not row or row["owner"] != self.owner:
                            continue
                        if not valid_request(self.manager, conn, job, self.clock()):
                            conn.execute(
                                "UPDATE reply_profile_requests SET state='canceled',owner=NULL,lease_until=NULL,reason='context_changed' WHERE "
                                + KEY,
                                key(job),
                            )
                            continue
                        detail = details.get(job["member_id"])
                        name = names.get(job["member_id"])
                        valid = detail is not None and bool(clean(detail.get("nickName"), 100))
                        if valid:
                            group_at = (
                                self.clock()
                                if group_ok
                                and name is not None
                                and ("displayName" in name)
                                and (name["displayName"] is None or isinstance(name["displayName"], str))
                                else None
                            )
                            if group_at is not None:
                                conn.execute(
                                    "UPDATE reply_participant_profiles SET group_name=%s,group_updated_at=%s WHERE "
                                    + KEY,
                                    (clean(name.get("displayName"), 100), group_at, *key(job)),
                                )
                        success = valid and (not group or (group_ok and name is not None))
                        attempts = row["attempts"]
                        state = "ready" if success else "queued" if attempts < 3 else "failed"
                        delay = TTL if success or attempts >= 3 else 60 * attempts
                        conn.execute(
                            "UPDATE reply_profile_requests SET state=%s,next_at=%s,owner=NULL,lease_until=NULL,reason=%s WHERE "
                            + KEY,
                            (
                                state,
                                self.clock() + delay,
                                "" if success else "profile_unavailable",
                                *key(job),
                            ),
                        )
                    conn.execute(
                        "UPDATE reply_profile_clock SET owner=NULL,lease_until=NULL WHERE id=1 AND owner=%s",
                        (self.owner,),
                    )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_332)
        return True
