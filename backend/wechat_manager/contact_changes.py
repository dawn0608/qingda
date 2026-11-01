"""联系人关系标记及核实；保留同一会话、消息与原始回调。"""

import asyncio
import hashlib
import json
import re

from .contacts import SNAPSHOT_FIELDS, SYSTEM_CONTACTS, contact_lists, snapshot
from .database_async import database_call
from .protocol import safe_xml
from .providers import ProviderError
from .store import load_event, string

# 回复规则版本用于取消发送任务；微信关系仅随真实账号与终端身份变化失效。
RELATIONSHIP_IDENTITY_FIELDS = ("account_id", "app_id", "wxid", "operation_id")


def monitored(peer, wxid):
    return peer not in SYSTEM_CONTACTS and peer != wxid and not peer.startswith("gh_")


def verification(conn, conv, now):
    account = conn.execute("SELECT * FROM accounts WHERE id=%s", (conv["account_id"],)).fetchone()
    row = conn.execute(
        "SELECT * FROM contact_monitor WHERE account_id=%s AND peer_id=%s",
        (conv["account_id"], conv["peer_id"]),
    ).fetchone()
    current = snapshot(conn, conv["account_id"])
    version = conn.execute(
        "SELECT version FROM contact_removals WHERE account_id=%s AND peer_id=%s",
        (conv["account_id"], conv["peer_id"]),
    ).fetchone()
    valid = bool(
        row
        and current
        and all(row[k] == current[k] for k in RELATIONSHIP_IDENTITY_FIELDS)
        and row["relationship_version"] == (version[0] if version else 0)
    )
    state = row["state"] if valid else "unverified"
    if not monitored(conv["peer_id"], account["wxid"]):
        state = "excluded"
    elif state == "verified" and (row["verified_at"] is None or now - row["verified_at"] >= 1200):
        state = "stale"
    return {
        "state": state,
        "attempted_at": row["attempted_at"] if valid else None,
        "verified_at": row["verified_at"] if valid else None,
        "next_at": row["next_at"] if valid else None,
        "reason": row["reason"] if valid else "",
    }


def decorate_monitor(conn, conv, now):
    from .chat_visibility import chat_visible

    health = verification(conn, conv, now)
    account = conn.execute("SELECT * FROM accounts WHERE id=%s", (conv["account_id"],)).fetchone()
    conv["chat_visible"] = chat_visible(conv["peer_id"], account["wxid"])
    if conv["removed_at"] is not None:
        status = "contact_removed"
    elif not account["enabled"] or account["archived_at"] is not None:
        status = "account_disabled"
    elif account["online"] == 0:
        status = "account_offline"
    elif not account["ai_enabled"]:
        status = "ai_disabled"
    elif not conv["allowed"]:
        status = "not_allowed"
    elif health["state"] not in ("verified", "excluded"):
        status = "relationship_unverified"
    elif conv["pause_until"] > now:
        status = "paused"
    else:
        status = "enabled"
    conv.update(verification=health, auto_reply_status=status, server_time=now)
    return conv


def change_hold(conn, aid, peer, now, previous, held):
    if previous is None or previous != held:
        conn.execute(
            "UPDATE conversations SET epoch=epoch+1,ai_since=%s WHERE account_id=%s AND peer_id=%s",
            (now, aid, peer),
        )
        conn.execute(
            "UPDATE tasks SET state='canceled',reason='relationship_unverified',updated_at=%s WHERE conversation_id IN (SELECT id FROM conversations WHERE account_id=%s AND peer_id=%s) AND kind='auto' AND state IN ('queued','generating','ready','preparing')",
            (now, aid, peer),
        )


def monitor_result(conn, job, now, state, reason, evidence_id, *, completed_at=None):
    old = conn.execute(
        "SELECT * FROM contact_monitor WHERE account_id=%s AND peer_id=%s",
        (job["account_id"], job["peer_id"]),
    ).fetchone()
    current = snapshot(conn, job["account_id"])
    version = conn.execute(
        "SELECT version FROM contact_removals WHERE account_id=%s AND peer_id=%s",
        (job["account_id"], job["peer_id"]),
    ).fetchone()
    held = int(state != "verified")
    if state != "removed" and not (old is None and state == "verified" and completed_at is not None):
        change_hold(conn, job["account_id"], job["peer_id"], now, old["held"] if old else None, held)
    same = old and all(old[k] == current[k] for k in RELATIONSHIP_IDENTITY_FIELDS)
    failures = min((old["failures"] if same else 0) + 1, 4) if state in ("failed", "unverified") else 0
    delay = min(3600, 600 * 2 ** max(0, failures - 1))
    verified_at = (
        (now if completed_at is None else completed_at)
        if state == "verified"
        else old["verified_at"]
        if same
        else None
    )
    conn.execute(
        "INSERT INTO contact_monitor VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,account_id,peer_id) DO UPDATE SET app_id=excluded.app_id,wxid=excluded.wxid,epoch=excluded.epoch,operation_id=excluded.operation_id,relationship_version=excluded.relationship_version,state=excluded.state,attempted_at=excluded.attempted_at,verified_at=excluded.verified_at,next_at=excluded.next_at,failures=excluded.failures,reason=excluded.reason,evidence_id=excluded.evidence_id,held=excluded.held",
        (
            job["account_id"],
            job["peer_id"],
            current["app_id"],
            current["wxid"],
            current["epoch"],
            current["operation_id"],
            version[0] if version else 0,
            state,
            job["attempted_at"],
            verified_at,
            (now if completed_at is None else completed_at) + delay,
            failures,
            reason,
            evidence_id,
            held,
        ),
    )


def system_relationship(event, wxid):
    data = event.get("Data")
    if not isinstance(data, dict) or str(data.get("MsgType")) not in ("10000", "10002"):
        return None
    if any(data.get(k) for k in ("IsHistory", "isHistory", "is_history", "History", "IsSync", "IsTest")):
        return None
    if (
        not (string(data.get("FromUserName")) or "").endswith("@chatroom")
        or string(data.get("ToUserName")) != wxid
    ):
        return None
    content = string(data.get("Content")) or ""
    if ":\n" in content:
        content = content.split(":\n", 1)[1]
    if str(data.get("MsgType")) == "10000" and re.fullmatch(
        r'你被(?:"[^"\r\n]{1,100}"|“[^”\r\n]{1,100}”)移出(?:了)?群聊[。！]?', content.strip()
    ):
        return "group_kicked"
    root = None
    if str(data.get("MsgType")) == "10002":
        root = safe_xml(content)
        if root is None or root.tag != "sysmsg" or root.get("type") != "sysmsgtemplate":
            return None
        templates = root.findall(".//content_template/template")
        if len(templates) != 1 or not templates[0].text:
            return None
        content = templates[0].text
    # 只接受完整系统句式，不扫描任意正文中的关键词。
    text = content.strip().rstrip("。！").replace('"', "").replace("“", "").replace("”", "")
    actor = r"(?:群主|管理员|群管理员|\$[A-Za-z_][A-Za-z_0-9]*\$)"
    if re.fullmatch(r"你被" + actor + r"移出(?:了)?群聊", text) or re.fullmatch(
        actor + r"将你移出(?:了)?群聊", text
    ):
        return "group_kicked"
    match = re.fullmatch(actor + r"将\$([A-Za-z_][A-Za-z_0-9]*)\$移出(?:了)?群聊", text)
    if root is not None and match:
        links = [link for link in root.findall(".//link_list/link") if link.get("name") == match[1]]
        if len(links) == 1 and wxid in [node.text for node in links[0].findall(".//member/username")]:
            return "group_kicked"
    if text in ("该群聊已被群主解散", "该群已被群主解散", "群主已解散该群聊", "该群聊已解散"):
        return "group_dissolved"
    if re.fullmatch(actor + r"已解散(?:该)?群聊", text):
        return "group_dissolved"
    return None


RELATIONSHIP_COLUMNS = (
    "CASE WHEN r.restored_at IS NULL THEN r.removed_at END AS removed_at,"
    "CASE WHEN r.removed_at IS NULL OR r.restored_at IS NOT NULL THEN 'active' "
    "WHEN c.kind='group' THEN 'group_left' ELSE 'friend_removed' END AS relationship_status,"
    "CASE WHEN r.removed_at IS NOT NULL AND r.restored_at IS NULL THEN coalesce(nullif(r.reason,''),"
    "CASE WHEN c.kind='group' THEN 'group_left' ELSE 'friend_removed' END) ELSE '' END AS relationship_reason"
)


def trusted_peer(conn, account, row, peer, *, require_wxid=False):
    terminal = conn.execute(
        "SELECT verified FROM terminals WHERE app_id=%s AND account_id=%s",
        (row["app_id"], account["id"]),
    ).fetchone()
    if (
        row["origin"] != "live"
        or not account["wxid"]
        or (row["wxid"] != account["wxid"] if require_wxid else row["wxid"] not in (None, account["wxid"]))
        or row["app_id"] != account["current_app_id"]
        or account["archived_at"] is not None
        or not terminal
        or not terminal[0]
        or not peer
        or len(peer) > 200
        or any(char.isspace() or ord(char) < 32 for char in peer)
        or peer == account["wxid"]
        or peer in SYSTEM_CONTACTS
        or peer.startswith("gh_")
    ):
        return None
    return peer


def remove_contact(conn, account, row, event, now):
    data = event.get("Data")
    peer = string(data.get("UserName")) if isinstance(data, dict) else None
    if not trusted_peer(conn, account, row, peer, require_wxid=True):
        return
    if isinstance(data, dict) and any(
        data.get(k) for k in ("IsHistory", "isHistory", "History", "IsSync", "IsTest")
    ):
        return
    mark_removed(
        conn, account, peer, now, "group_left" if peer.endswith("@chatroom") else "friend_removed", row=row
    )


def apply_system_relationship(conn, account, row, event, msg, now):
    reason = system_relationship(event, account["wxid"])
    if not reason or not trusted_peer(conn, account, row, msg["peer"], require_wxid=True):
        return reason
    previous = conn.execute(
        "SELECT restored_at FROM contact_removals WHERE account_id=%s AND peer_id=%s",
        (account["id"], msg["peer"]),
    ).fetchone()
    if (
        msg["created_at"]
        and 0 <= now - msg["created_at"] <= 300
        and (not previous or previous[0] is None or msg["created_at"] > previous[0])
    ):
        mark_removed(conn, account, msg["peer"], now, reason, row=row)
    return reason


def record_evidence(conn, account, peer, source, started_at, completed_at, result, event_id=None):
    identity = {k: account[k] for k in SNAPSHOT_FIELDS if k in account.keys()}
    if "account_id" not in identity:
        identity["account_id"] = account["id"]
        identity["app_id"] = account["current_app_id"]
        terminal = conn.execute(
            "SELECT operation_id FROM terminals WHERE app_id=%s", (account["current_app_id"],)
        ).fetchone()
        identity["operation_id"] = terminal[0] if terminal else None
    aid = account["account_id"] if "account_id" in account.keys() else account["id"]
    stable = {
        key: value
        for key, value in result.items()
        if key not in ("query_duration_ms", "processing_ms", "completed_at", "started_at")
    }
    digest = None
    if event_id is None and result.get("valid") and not result.get("error"):
        digest = hashlib.sha256(
            json.dumps([identity, stable], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
    return conn.execute(
        "INSERT INTO contact_evidence(account_id,peer_id,source,event_id,identity_json,started_at,completed_at,result_json,result_hash,first_observed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,account_id,peer_id,source,result_hash) WHERE result_hash IS NOT NULL DO UPDATE SET completed_at=excluded.completed_at,observations=contact_evidence.observations+1 RETURNING id",
        (
            aid,
            peer,
            source,
            event_id,
            json.dumps(identity),
            started_at,
            completed_at,
            json.dumps(stable, ensure_ascii=False),
            digest,
            completed_at,
        ),
    ).fetchone()[0]


def fresh_friends(result, wxid):
    friends = set(contact_lists(result, wxid)[0])
    if not result["data"]["friends"] or any(
        result["data"].get(k)
        for k in ("hasMore", "has_more", "nextPage", "next_page", "continueFlag", "isCache", "is_cache")
    ):
        raise ProviderError("incomplete_contacts_evidence")
    return friends


def remember_friends(conn, account, friends, started_at, completed_at):
    # 同一份完整全量结果只保存一次；好友基线引用该查询快照。
    evidence_id = record_evidence(
        conn, account, "", "fresh_contacts_snapshot", started_at, completed_at, {"friends": sorted(friends)}
    )
    conn.cursor().executemany(
        "INSERT INTO contact_baselines VALUES(%s,%s,%s,%s,%s) ON CONFLICT(user_id,account_id,peer_id) DO UPDATE SET wxid=excluded.wxid,verified_at=excluded.verified_at,evidence_id=excluded.evidence_id",
        [(account["account_id"], peer, account["wxid"], completed_at, evidence_id) for peer in friends],
    )


def mark_removed(conn, account, peer, now, reason, *, row=None, evidence_id=None):
    previous = conn.execute(
        "SELECT * FROM contact_removals WHERE account_id=%s AND peer_id=%s", (account["id"], peer)
    ).fetchone()
    if row and previous and previous["event_id"] is not None and previous["event_id"] >= row["id"]:
        return
    if (
        previous
        and previous["restored_at"] is None
        and reason == "group_left"
        and previous["reason"] in ("group_kicked", "group_dissolved")
    ):
        reason = previous["reason"]
    if evidence_id is None:
        data = load_event(conn, row).get("Data", {})
        created_at = data.get("CreateTime") if isinstance(data, dict) else None
        timing = {"reason": reason, "processing_ms": max(0, (now - row["received_at"]) * 1000)}
        if (
            isinstance(created_at, (int, float))
            and not isinstance(created_at, bool)
            and 0 < created_at <= row["received_at"]
        ):
            timing["platform_delay_ms"] = (row["received_at"] - created_at) * 1000
        evidence_id = record_evidence(
            conn, account, peer, "callback", row["received_at"], now, timing, row["id"]
        )
    saved = conn.execute(
        "SELECT display_name,profile_json FROM contact_entries WHERE account_id=%s AND peer_id=%s",
        (account["id"], peer),
    ).fetchone()
    conn.execute(
        "INSERT INTO contact_removals(account_id,peer_id,event_id,removed_at,display_name,profile_json,reason,evidence_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,account_id,peer_id) DO UPDATE SET event_id=coalesce(excluded.event_id,contact_removals.event_id),removed_at=excluded.removed_at,restored_at=NULL,reason=excluded.reason,evidence_id=excluded.evidence_id,version=contact_removals.version+1,display_name=CASE WHEN excluded.display_name!='' THEN excluded.display_name ELSE contact_removals.display_name END,profile_json=CASE WHEN excluded.profile_json!='{}' THEN excluded.profile_json ELSE contact_removals.profile_json END",
        (
            account["id"],
            peer,
            row["id"] if row else None,
            row["received_at"] if row else now,
            saved[0] if saved else "",
            saved[1] if saved else "{}",
            reason,
            evidence_id,
        ),
    )
    conn.execute(
        "UPDATE contact_verifications SET state='superseded',owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s",
        (account["id"], peer),
    )
    conn.execute("DELETE FROM contact_entries WHERE account_id=%s AND peer_id=%s", (account["id"], peer))
    conn.execute(
        "UPDATE account_contacts SET friend_count=(SELECT count(*) FROM contact_entries WHERE account_id=%s AND kind='friend'),chatroom_count=(SELECT count(*) FROM contact_entries WHERE account_id=%s AND kind='group' AND saved_to_contacts=1) WHERE account_id=%s AND wxid=%s AND list_updated_at IS NOT NULL",
        (account["id"], account["id"], account["id"], account["wxid"]),
    )
    conv = conn.execute(
        "SELECT id FROM conversations WHERE account_id=%s AND peer_id=%s", (account["id"], peer)
    ).fetchone()
    if conv and (not previous or previous["restored_at"] is not None):
        conn.execute("UPDATE conversations SET allowed=0,epoch=epoch+1 WHERE id=%s", (conv[0],))
        conn.execute(
            "UPDATE tasks SET state='canceled',reason='contact_removed',updated_at=%s WHERE conversation_id=%s AND state IN ('queued','generating','ready','preparing')",
            (now, conv[0]),
        )


def require_active_contact(conv):
    if conv["removed_at"] is not None:
        raise ValueError("该联系人已移除，仅可查看历史记录")


def enqueue_verification(conn, aid, peer, now):
    removed = conn.execute(
        "SELECT event_id,version FROM contact_removals WHERE account_id=%s AND peer_id=%s",
        (aid, peer),
    ).fetchone()
    current = snapshot(conn, aid)
    if not current or peer in SYSTEM_CONTACTS or peer == current["wxid"] or peer.startswith("gh_"):
        return "unchanged"
    if not conn.execute(
        "SELECT 1 FROM conversations WHERE account_id=%s AND peer_id=%s", (aid, peer)
    ).fetchone():
        return "unchanged"
    version = removed["version"] if removed else 0
    event_id = removed["event_id"] if removed else None
    old = conn.execute(
        "SELECT * FROM contact_verifications WHERE account_id=%s AND peer_id=%s", (aid, peer)
    ).fetchone()
    same = (
        old
        and old["relationship_version"] == version
        and all(old[k] == current[k] for k in RELATIONSHIP_IDENTITY_FIELDS)
    )
    if same and old["state"] in ("queued", "running"):
        return "queued"
    identity_same = old and all(old[k] == current[k] for k in RELATIONSHIP_IDENTITY_FIELDS)
    due = max(now, (old["attempted_at"] or 0) + 60) if identity_same and peer.endswith("@chatroom") else now
    monitor = conn.execute(
        "SELECT * FROM contact_monitor WHERE account_id=%s AND peer_id=%s", (aid, peer)
    ).fetchone()
    if (
        monitor
        and monitor["state"] in ("failed", "unverified")
        and monitor["relationship_version"] == version
        and all(monitor[k] == current[k] for k in RELATIONSHIP_IDENTITY_FIELDS)
    ):
        due = max(due, monitor["next_at"])
    watermark = conn.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
    record_evidence(
        conn,
        current,
        peer,
        "verification_queued",
        now,
        now,
        {"eligible_at": due, "cooldown_ms": max(0, due - now) * 1000},
    )
    conn.execute(
        "INSERT INTO contact_verifications(account_id,peer_id,app_id,wxid,epoch,operation_id,event_id,next_at,relationship_version,event_watermark) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,account_id,peer_id) DO UPDATE SET app_id=excluded.app_id,wxid=excluded.wxid,epoch=excluded.epoch,operation_id=excluded.operation_id,event_id=excluded.event_id,relationship_version=excluded.relationship_version,event_watermark=excluded.event_watermark,state='queued',next_at=excluded.next_at,owner=NULL,lease_until=NULL,reason=''",
        (
            aid,
            peer,
            current["app_id"],
            current["wxid"],
            current["epoch"],
            current["operation_id"],
            event_id,
            due,
            version,
            watermark,
        ),
    )
    return "queued"


class ContactRecovery:
    def __init__(self, manager, gateway, owner, clock):
        self.manager, self.store, self.gateway = manager, manager.store, gateway
        self.owner, self.clock = owner, clock
        self.lock = asyncio.Semaphore(2)

    def inherit_evidence(self, conn, conv, current, old, proofs):
        """承接成功查询的原始时间；不从通讯录缓存推断关系。"""
        group = conv["kind"] == "group"
        key = (conv["account_id"], conv["peer_id"] if group else "")
        if key not in proofs:
            proof = conn.execute(
                "SELECT * FROM contact_evidence WHERE account_id=%s AND source=%s AND peer_id=%s ORDER BY completed_at DESC,id DESC LIMIT 1",
                (conv["account_id"], "group_membership" if group else "fresh_contacts_snapshot", key[1]),
            ).fetchone()
            attempt = conn.execute(
                "SELECT attempted_at FROM contact_verifications WHERE account_id=%s AND peer_id=%s"
                if group
                else "SELECT full_attempted_at FROM account_contacts WHERE account_id=%s",
                key if group else (conv["account_id"],),
            ).fetchone()
            proofs[key] = (proof, attempt[0] if attempt else None)
        proof, attempted_at = proofs[key]
        if not proof or not (proof["started_at"] <= proof["completed_at"] <= self.clock()):
            return False
        if self.clock() - proof["completed_at"] >= 1200:
            return False
        if attempted_at is not None and attempted_at > proof["completed_at"]:
            return False
        if (
            old
            and all(old[k] == current[k] for k in SNAPSHOT_FIELDS)
            and old["attempted_at"] is not None
            and old["attempted_at"] >= proof["started_at"]
        ):
            return False
        identity, result = json.loads(proof["identity_json"]), json.loads(proof["result_json"])
        if any(identity.get(k) != current[k] for k in RELATIONSHIP_IDENTITY_FIELDS):
            return False
        if group:
            if not result.get("valid") or result.get("error") or not isinstance(result.get("present"), bool):
                return False
            positive = result["present"]
        else:
            positive = conv["peer_id"] in result["friends"]
        removal = conn.execute(
            "SELECT * FROM contact_removals WHERE account_id=%s AND peer_id=%s",
            (conv["account_id"], conv["peer_id"]),
        ).fetchone()
        if removal and proof["started_at"] < (removal["restored_at"] or removal["removed_at"]):
            return False
        job = {
            **current,
            "peer_id": conv["peer_id"],
            "attempted_at": proof["started_at"],
            "event_id": None,
            "event_watermark": 0,
            "relationship_version": removal["version"] if removal else 0,
        }
        if self.pending_delete(conn, job):
            return False
        marked = removal and removal["restored_at"] is None
        state = "verified" if positive else "unverified"
        if positive and marked:
            job["event_id"] = removal["event_id"]
            self.restore(conn, job, group)
        elif not positive:
            baseline = conn.execute(
                "SELECT 1 FROM contact_baselines WHERE account_id=%s AND peer_id=%s AND wxid=%s AND verified_at<=%s",
                (conv["account_id"], conv["peer_id"], current["wxid"], proof["started_at"]),
            ).fetchone()
            if marked or group or baseline:
                state = "removed"
                if not marked:
                    account = conn.execute(
                        "SELECT * FROM accounts WHERE id=%s", (conv["account_id"],)
                    ).fetchone()
                    mark_removed(
                        conn,
                        account,
                        conv["peer_id"],
                        self.clock(),
                        "group_left" if group else "friend_removed",
                        evidence_id=proof["id"],
                    )
        monitor_result(
            conn,
            job,
            self.clock(),
            state,
            "insufficient_evidence" if state == "unverified" else "",
            proof["id"],
            completed_at=proof["completed_at"],
        )
        # 同一完整结果已覆盖的排队请求直接合并；在途任务仍由自身租约与版本检查收尾。
        conn.execute(
            "UPDATE contact_verifications SET state='done',attempted_at=%s,owner=NULL,lease_until=NULL,reason='' WHERE account_id=%s AND peer_id=%s AND state='queued' AND app_id=%s AND wxid=%s AND operation_id=%s",
            (
                proof["started_at"],
                conv["account_id"],
                conv["peer_id"],
                current["app_id"],
                current["wxid"],
                current["operation_id"],
            ),
        )
        return True

    def inherit_account(self, conn, aid):
        """全量查询成功时统一应用到会话，须在通讯录条目替换前保留最后资料。"""
        current = snapshot(conn, aid)
        if not current or not self.leased(conn):
            return
        proofs = {}
        for conv in conn.execute(
            "SELECT * FROM conversations WHERE account_id=%s AND kind!='group'", (aid,)
        ).fetchall():
            if not monitored(conv["peer_id"], current["wxid"]):
                continue
            old = conn.execute(
                "SELECT * FROM contact_monitor WHERE account_id=%s AND peer_id=%s", (aid, conv["peer_id"])
            ).fetchone()
            self.inherit_evidence(conn, conv, current, old, proofs)

    def schedule(self):
        """仅扫描本站状态；网页关闭时仍由 Worker 调度全部会话。"""
        with self.store.transaction() as conn:
            if not self.leased(conn):
                return 0
            rows = conn.execute(
                "SELECT c.*,m.next_at FROM conversations c JOIN accounts a ON a.id=c.account_id "
                "LEFT JOIN contact_monitor m ON m.account_id=c.account_id AND m.peer_id=c.peer_id "
                "WHERE a.archived_at IS NULL ORDER BY c.allowed DESC,coalesce(m.next_at,0),c.id"
            ).fetchall()
            count = 0
            proofs = {}
            for row in rows:
                current = snapshot(conn, row["account_id"])
                if not current or not monitored(row["peer_id"], current["wxid"]):
                    continue
                old = conn.execute(
                    "SELECT * FROM contact_monitor WHERE account_id=%s AND peer_id=%s",
                    (row["account_id"], row["peer_id"]),
                ).fetchone()
                if self.inherit_evidence(conn, row, current, old, proofs):
                    old = conn.execute(
                        "SELECT * FROM contact_monitor WHERE account_id=%s AND peer_id=%s",
                        (row["account_id"], row["peer_id"]),
                    ).fetchone()
                health = verification(conn, row, self.clock())
                if health["state"] in ("unverified", "stale") and (not old or not old["held"]):
                    change_hold(
                        conn, row["account_id"], row["peer_id"], self.clock(), old["held"] if old else None, 1
                    )
                    if old:
                        conn.execute(
                            "UPDATE contact_monitor SET held=1 WHERE account_id=%s AND peer_id=%s",
                            (row["account_id"], row["peer_id"]),
                        )
                identity_same = old and all(old[k] == current[k] for k in RELATIONSHIP_IDENTITY_FIELDS)
                if identity_same and old["next_at"] > self.clock():
                    continue
                conn.execute(
                    "INSERT INTO contact_monitor(account_id,peer_id,app_id,wxid,epoch,operation_id) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        current["account_id"],
                        row["peer_id"],
                        current["app_id"],
                        current["wxid"],
                        current["epoch"],
                        current["operation_id"],
                    ),
                )
                enqueue_verification(conn, row["account_id"], row["peer_id"], self.clock())
                conn.execute(
                    "UPDATE contact_monitor SET next_at=%s WHERE account_id=%s AND peer_id=%s",
                    (self.clock() + 600, row["account_id"], row["peer_id"]),
                )
                count += 1
            return count

    def current(self, conn, job):
        account = snapshot(conn, job["account_id"])
        removal = conn.execute(
            "SELECT version FROM contact_removals WHERE account_id=%s AND peer_id=%s",
            (job["account_id"], job["peer_id"]),
        ).fetchone()
        return bool(
            account
            and (removal[0] if removal else 0) == job["relationship_version"]
            and all(account[k] == job[k] for k in RELATIONSHIP_IDENTITY_FIELDS)
        )

    def leased(self, conn):
        return conn.execute(
            "SELECT 1 FROM worker_lock WHERE owner=%s AND expires_at>%s", (self.owner, self.clock())
        ).fetchone()

    def pending_delete(self, conn, job):
        for row in conn.execute(
            "SELECT * FROM events WHERE id>%s AND app_id=%s AND wxid=%s AND event_type IN ('DelContacts','AddMsg','ModContacts') AND origin='live' AND parse_error IS NULL AND processed=0",
            (min(job["event_watermark"], job["event_id"] or 0), job["app_id"], job["wxid"]),
        ):
            event = load_event(conn, row)
            data = event.get("Data")
            if (
                row["event_type"] in ("DelContacts", "ModContacts")
                and isinstance(data, dict)
                and string(data.get("UserName")) == job["peer_id"]
            ):
                return True
            if system_relationship(event, job["wxid"]) and job["peer_id"] in (
                row["sender"],
                row["recipient"],
            ):
                return True
        return False

    async def once(self):
        async with self.lock:

            def _database_once_640():
                with self.store.transaction() as conn:
                    if not self.leased(conn):
                        return ("return", False, locals())
                    row = conn.execute(
                        "SELECT * FROM contact_verifications WHERE (state='queued' AND next_at<=%s) OR (state='running' AND (owner!=%s OR lease_until<=%s)) ORDER BY CASE WHEN (SELECT allowed FROM conversations c WHERE c.account_id=contact_verifications.account_id AND c.peer_id=contact_verifications.peer_id)=1 THEN 0 ELSE 1 END,next_at,account_id,peer_id LIMIT 1",
                        (self.clock(), self.owner, self.clock()),
                    ).fetchone()
                    if not row:
                        return ("return", False, locals())
                    first = dict(row)
                    group = first["peer_id"].endswith("@chatroom")
                    rows = (
                        [row]
                        if group
                        else conn.execute(
                            "SELECT * FROM contact_verifications WHERE account_id=%s AND peer_id NOT LIKE '%%@chatroom' AND ((state='queued' AND next_at<=%s) OR (state='running' AND (owner!=%s OR lease_until<=%s)))",
                            (first["account_id"], self.clock(), self.owner, self.clock()),
                        ).fetchall()
                    )
                    jobs = []
                    for row in rows:
                        job = dict(row)
                        if not self.current(conn, job):
                            conn.execute(
                                "UPDATE contact_verifications SET state='superseded',owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                                (job["account_id"], job["peer_id"]),
                            )
                        else:
                            jobs.append(job)
                    if not jobs:
                        return ("return", True, locals())
                    first = jobs[0]
                    started_at = self.clock()
                    if not group:
                        cached = conn.execute(
                            "SELECT full_attempted_at FROM account_contacts WHERE account_id=%s",
                            (first["account_id"],),
                        ).fetchone()
                        if cached and cached[0] is not None and (self.clock() < cached[0] + 600):
                            for job in jobs:
                                record_evidence(
                                    conn,
                                    job,
                                    job["peer_id"],
                                    "verification_cooldown",
                                    self.clock(),
                                    self.clock(),
                                    {
                                        "eligible_at": cached[0] + 600,
                                        "cooldown_ms": (cached[0] + 600 - self.clock()) * 1000,
                                    },
                                )
                                conn.execute(
                                    "UPDATE contact_verifications SET state='queued',next_at=%s,owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                                    (cached[0] + 600, job["account_id"], job["peer_id"]),
                                )
                            return ("return", True, locals())
                        conn.execute(
                            "INSERT INTO account_contacts(account_id,app_id,wxid,epoch,operation_id,full_attempted_at,state) VALUES(%s,%s,%s,%s,%s,%s,'idle') ON CONFLICT(user_id,account_id) DO UPDATE SET full_attempted_at=excluded.full_attempted_at",
                            (*[first[k] for k in SNAPSHOT_FIELDS], self.clock()),
                        )
                    for job in jobs:
                        job["attempted_at"] = started_at
                        conn.execute(
                            "UPDATE contact_verifications SET state='running',attempted_at=%s,owner=%s,lease_until=%s WHERE account_id=%s AND peer_id=%s",
                            (started_at, self.owner, started_at + 45, job["account_id"], job["peer_id"]),
                        )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_640)
            if "first" in _database_result[2]:
                first = _database_result[2]["first"]
            if "group" in _database_result[2]:
                group = _database_result[2]["group"]
            if "jobs" in _database_result[2]:
                jobs = _database_result[2]["jobs"]
            if "started_at" in _database_result[2]:
                started_at = _database_result[2]["started_at"]
            if _database_result[0] == "return":
                return _database_result[1]
            restored, reason, proof = set(), "", {}
            try:
                if group:
                    result = await self.gateway.call(
                        "/group/getChatroomMemberList",
                        {"appId": first["app_id"], "chatroomId": first["peer_id"]},
                        timeout=30,
                    )
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
                            for k in (
                                "hasMore",
                                "has_more",
                                "nextPage",
                                "next_page",
                                "continueFlag",
                                "isCache",
                                "is_cache",
                            )
                        )
                    ):
                        raise ProviderError("invalid_members_response")
                    if any(isinstance(m, dict) and m.get("wxid") == first["wxid"] for m in members):
                        restored.add(first["peer_id"])
                    identifiers = sorted(m["wxid"] for m in members)
                    proof = {
                        "list_count": len(identifiers),
                        "list_sha256": hashlib.sha256(json.dumps(identifiers).encode()).hexdigest(),
                    }
                else:
                    result = await self.gateway.call(
                        "/contacts/fetchContactsList", {"appId": first["app_id"]}, timeout=30
                    )
                    restored = fresh_friends(result, first["wxid"])
                    proof = {
                        "list_count": len(restored),
                        "list_sha256": hashlib.sha256(json.dumps(sorted(restored)).encode()).hexdigest(),
                    }
            except ProviderError as error:
                reason = error.reason
            except Exception:
                reason = "contact_verification_failed"

            def _database_once_763():
                with self.store.transaction() as conn:
                    if not self.leased(conn):
                        return ("return", True, locals())
                    remembered = False
                    for job in jobs:
                        owned = conn.execute(
                            "SELECT 1 FROM contact_verifications WHERE account_id=%s AND peer_id=%s AND state='running' AND owner=%s AND relationship_version=%s",
                            (job["account_id"], job["peer_id"], self.owner, job["relationship_version"]),
                        ).fetchone()
                        if not owned:
                            continue
                        removal = conn.execute(
                            "SELECT * FROM contact_removals WHERE account_id=%s AND peer_id=%s",
                            (job["account_id"], job["peer_id"]),
                        ).fetchone()
                        valid = (
                            self.current(conn, job)
                            and (not removal or removal["removed_at"] <= started_at)
                            and (not self.pending_delete(conn, job))
                        )
                        evidence_id = record_evidence(
                            conn,
                            job,
                            job["peer_id"],
                            "group_membership" if group else "fresh_contacts_list",
                            started_at,
                            self.clock(),
                            {
                                **proof,
                                "present": job["peer_id"] in restored,
                                "error": reason,
                                "valid": valid,
                                "query_duration_ms": max(0, self.clock() - started_at) * 1000,
                            },
                        )
                        marked = removal and removal["restored_at"] is None
                        monitor_state = "failed" if reason else "unverified"
                        if valid and (not reason):
                            if not group and (not remembered):
                                remember_friends(conn, job, restored, started_at, self.clock())
                                remembered = True
                            positive = job["peer_id"] in restored
                            if positive:
                                monitor_state = "verified"
                                if marked:
                                    self.restore(conn, job, group)
                            elif not marked:
                                baseline = conn.execute(
                                    "SELECT 1 FROM contact_baselines WHERE account_id=%s AND peer_id=%s AND wxid=%s AND verified_at<=%s",
                                    (job["account_id"], job["peer_id"], job["wxid"], started_at),
                                ).fetchone()
                                if group or baseline:
                                    monitor_state = "removed"
                                    account = conn.execute(
                                        "SELECT * FROM accounts WHERE id=%s", (job["account_id"],)
                                    ).fetchone()
                                    mark_removed(
                                        conn,
                                        account,
                                        job["peer_id"],
                                        self.clock(),
                                        "group_left" if group else "friend_removed",
                                        evidence_id=evidence_id,
                                    )
                            elif marked:
                                monitor_state = "removed"
                        if valid:
                            monitor_result(
                                conn,
                                job,
                                self.clock(),
                                monitor_state,
                                reason or ("insufficient_evidence" if monitor_state == "unverified" else ""),
                                evidence_id,
                            )
                        conn.execute(
                            "UPDATE contact_verifications SET state=%s,reason=%s,owner=NULL,lease_until=NULL WHERE account_id=%s AND peer_id=%s",
                            (
                                "done" if valid and (not reason) else "failed",
                                reason or ("" if valid else "contacts_superseded"),
                                job["account_id"],
                                job["peer_id"],
                            ),
                        )
                return ("normal", None, locals())

            _database_result = await database_call(_database_once_763)
            if _database_result[0] == "return":
                return _database_result[1]
            return True

    def restore(self, conn, job, group):
        now = self.clock()
        conn.execute(
            "UPDATE contact_removals SET restored_at=%s,version=version+1 WHERE account_id=%s AND peer_id=%s AND version=%s",
            (now, job["account_id"], job["peer_id"], job["relationship_version"]),
        )
        conn.execute(
            "UPDATE conversations SET allowed=0,ai_since=%s,epoch=epoch+1 WHERE account_id=%s AND peer_id=%s",
            (now, job["account_id"], job["peer_id"]),
        )
        if not group:
            removed = conn.execute(
                "SELECT display_name,profile_json FROM contact_removals WHERE account_id=%s AND peer_id=%s",
                (job["account_id"], job["peer_id"]),
            ).fetchone()
            conn.execute(
                "INSERT INTO contact_entries(account_id,peer_id,kind,display_name,profile_json,sort_key,owner_wxid,confirmed_at) VALUES(%s,%s,'friend',%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    job["account_id"],
                    job["peer_id"],
                    removed[0] or job["peer_id"],
                    removed[1],
                    (removed[0] or job["peer_id"]).casefold(),
                    job["wxid"],
                    now,
                ),
            )
            conn.execute(
                "UPDATE account_contacts SET friend_count=(SELECT count(*) FROM contact_entries WHERE account_id=%s AND kind='friend') WHERE account_id=%s AND list_updated_at IS NOT NULL",
                (job["account_id"], job["account_id"]),
            )
        from .contact_discovery import enqueue as discover_contact

        discover_contact(conn, job["account_id"], job["peer_id"], now, job["event_id"], "restored")
        from .automation_defaults import confirmed

        confirmed(self.manager, conn, job["account_id"], job["peer_id"])
        from .callbacks import refresh_greetings

        refresh_greetings(conn, job["account_id"], job["peer_id"])
        self.store.audit(
            "contact_relationship_restored",
            job["account_id"],
            {
                "peer_id": job["peer_id"],
                "removal_event_id": job["event_id"],
                "query_started_at": job["attempted_at"],
                "verification": "group_membership" if group else "fresh_contacts_list",
                "app_id": job["app_id"],
                "wxid": job["wxid"],
            },
            conn=conn,
        )
