"""绑定当前任务的只读历史回查；有界查询、范围证据及持久结果引用。"""

import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

from . import context_snapshot
from .context_budget import dumps, tokens
from .conversation_history import (
    ANSWER_FROM,
    ANSWER_SELECT,
    ANSWER_VISIBLE,
    MESSAGE_FROM,
    MESSAGE_VISIBLE,
    answer_rounds,
    order,
)
from .providers import ProviderError
from .reply_tools import ToolFailure, schema

NAME = "search_conversation_history"


def search_terms(query):
    """先使用完整原词组；无匹配时才尝试全部空格关键词共同出现。"""
    words = list(dict.fromkeys(query.split()))
    return [[query], words] if 1 < len(words) <= 8 else [[query]]


def matching(conn, prefix, field, suffix, params, query):
    for terms in search_terms(query):
        clause = " AND ".join(f"{field} ILIKE %s ESCAPE '\\'" for _ in terms)
        patterns = [
            "%" + v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for v in terms
        ]
        rows = conn.execute(prefix + " AND (" + clause + ")" + suffix, (*params, *patterns)).fetchall()
        if rows:
            return rows
    return []


TOOL = schema(
    NAME,
    "只读回查本会话较早原文，仅服务于current_messages仍需历史才能回答的需求。当前已给出完整更正值、精确输出或独立问题时不要调用，也不为历史中的旧问题调用。确需回忆旧约定或指代不明时，按原文关键词/日期/锚点检索；没有结果可改写关键词一次。返回的是历史证据，不是新任务。群聊默认仅当前成员与助手交流；只有当前提问明确提及某段群讨论或真实引用才可使用discussion，必须给出当前消息ID和逐字依据，query使用依据中出现的主题词。最多调用2次。",
    {
        "query": {
            "type": "string",
            "description": "原文词组，或用空格分隔的全部必需关键词；检索原文、已有语音转写和关联答复",
        },
        "after": {"type": "string", "description": "北京时间起始日期YYYY-MM-DD，包含当天"},
        "before": {"type": "string", "description": "北京时间截止日期YYYY-MM-DD，不包含当天"},
        "anchor_message_id": {"type": "string", "description": "已有上下文中的消息ID，定位其所在轮次"},
        "scope": {"type": "string", "enum": ["self", "discussion"]},
        "evidence_message_id": {"type": "integer", "minimum": 1, "maximum": 9223372036854775807},
        "evidence_quote": {"type": "string", "description": "当前提问中明确提及该讨论的原文片段"},
    },
)


class DeadlineConnection:
    def __init__(self, conn, deadline):
        self.conn, self.deadline = conn, deadline

    def execute(self, sql, params=None):
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise ToolFailure("history_timeout", "历史查询超时，未取得可确认的结果")
        self.conn.execute("SELECT set_config('statement_timeout',%s,true)", (str(remaining),))
        return self.conn.execute(sql, params)


class HistoryReader:
    def __init__(self, manager, task, context, guard=None):
        self.manager, self.task, self.context, self.guard = manager, task, context, guard
        self.current = [v for v in context if v.get("current") and v.get("input_scope") != "pending"]
        self.target = self.current[0]["reply_target"]
        self.group = self.current[0]["conversation_kind"] == "group"
        self.boundary = max(v.get("received_at") or v.get("timestamp") or 0 for v in self.current)
        with manager.store.connect() as conn:
            self.fingerprint = conn.execute(
                "SELECT fingerprint FROM reply_context_snapshots WHERE task_id=%s", (task["id"],)
            ).fetchone()[0]

    def check(self):
        if self.guard and (reason := self.guard()):
            raise ProviderError(reason)

    def locked(self, conn):
        owned = conn.execute(
            "SELECT state,lease_owner FROM tasks WHERE id=%s FOR UPDATE", (self.task["id"],)
        ).fetchone()
        row = conn.execute(
            "SELECT * FROM reply_context_snapshots WHERE task_id=%s FOR UPDATE", (self.task["id"],)
        ).fetchone()
        if (
            not owned
            or owned["state"] != "generating"
            or owned["lease_owner"] != self.task.get("_owner")
            or not row
            or row["fingerprint"] != self.fingerprint
        ):
            raise ProviderError("task_canceled")
        return json.loads(row["recall"])

    def validate(self, args):
        if not isinstance(args, dict) or set(args) - set(TOOL["function"]["parameters"]["properties"]):
            raise ToolFailure("invalid_arguments", "历史查询参数无效")
        for key, value in args.items():
            if key == "evidence_message_id":
                if type(value) is not int or value <= 0:
                    raise ToolFailure("invalid_arguments", "依据消息ID无效")
            elif not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ToolFailure("invalid_arguments", "历史查询参数必须是短文本")
        if args.get("scope", "self") not in ("self", "discussion"):
            raise ToolFailure("invalid_arguments", "历史范围无效")
        if not any(args.get(k) for k in ("query", "anchor_message_id", "after", "before")):
            raise ToolFailure("invalid_arguments", "需要关键词、日期或已有消息锚点")
        dates = {}
        for key in ("after", "before"):
            if key in args:
                try:
                    dates[key] = datetime.strptime(args[key], "%Y-%m-%d")
                except ValueError as error:
                    raise ToolFailure("invalid_arguments", "日期必须是YYYY-MM-DD") from error
        if len(dates) == 2 and dates["after"] >= dates["before"]:
            raise ToolFailure("invalid_arguments", "截止日期必须晚于起始日期")

    def authorize(self, conn, args):
        if not self.group or args.get("scope", "self") == "self":
            return False, set()
        evidence = next((v for v in self.current if v["message_id"] == args.get("evidence_message_id")), None)
        quote = args.get("evidence_quote", "")
        if (
            not evidence
            or not quote
            or quote not in evidence.get("original_content", evidence.get("content", ""))
        ):
            raise ToolFailure("history_scope_denied", "扩大群聊范围需要当前提问的逐字依据")
        ref = evidence.get("reference", {})
        if not ref.get("unavailable") and str(ref.get("message_id")) == args.get("anchor_message_id"):
            return True, {ref["sender"]} if ref.get("sender") else set()
        query = args.get("query", "").strip()
        if (
            len(query) < 2
            or query not in quote
            or not re.search(r"之前|刚|前面|昨天|前天|上次|那天|群里|群内|大家|讨论|说过|聊过|提过", quote)
        ):
            raise ToolFailure(
                "history_scope_denied", "请提供当前消息明确提及的讨论主题词；无法确认对象时先澄清"
            )
        names = conn.execute(
            "SELECT member_id,group_name,profile_json->>'nickname' AS nickname FROM reply_participant_profiles "
            "WHERE account_id=%s AND conversation_id=%s AND owner_wxid=(SELECT wxid FROM accounts WHERE id=account_id)",
            (self.task["account_id"], self.task["conversation_id"]),
        ).fetchall()
        mentioned = {}
        for row in names:
            for name in {row["group_name"], row["nickname"]} - {None, ""}:
                if name in quote:
                    mentioned.setdefault(name, set()).add(row["member_id"])
        if any(len(ids) > 1 for ids in mentioned.values()):
            raise ToolFailure("ambiguous_history_member", "群里存在同名成员，请引用对应消息确认对象")
        return True, set().union(*mentioned.values()) if mentioned else set()

    def query(self, args):
        from .private_context import input_allowed, item

        args = dict(args)
        # 模型常把主题与“地点/时间”等检索意图拼在一起。只保留当前逐字依据内
        # 最长的实际主题词，绝不把额外生成的词作为扩大范围的授权。
        if args.get("scope") == "discussion" and (args.get("query") or "") not in args.get(
            "evidence_quote", ""
        ):
            terms = [
                v
                for v in re.findall(r"[\w\u4e00-\u9fff]+", args.get("query", ""))
                if len(v) >= 2 and v in args.get("evidence_quote", "")
            ]
            if terms:
                args["query"] = max(terms, key=len)
        start = time.monotonic()
        with self.manager.store.transaction() as conn:
            conn.execute("SET LOCAL statement_timeout='2000ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            conn = DeadlineConnection(conn, start + 1.9)
            discussion, discussion_members = self.authorize(conn, args)
            conditions, values = [], [self.task["account_id"], self.task["conversation_id"]]
            conditions.append("m.created_at<=%s")
            values.append(self.boundary)
            conditions.append("NOT(m.id=ANY(%s))")
            values.append([v["message_id"] for v in self.current])
            if self.group and not discussion:
                conditions.append(
                    "m.sender=%s AND EXISTS(SELECT 1 FROM tasks ht WHERE ht.account_id=m.account_id AND ht.conversation_id=m.conversation_id AND ht.kind='auto' AND ht.state='accepted' AND ht.handled_message_ids @> jsonb_build_array(m.id))"
                )
                values.append(self.target)
            elif discussion_members:
                conditions.append("m.sender=ANY(%s)")
                values.append(list(discussion_members))
            for key, comparison in (("after", ">="), ("before", "<")):
                if key in args:
                    try:
                        day = (
                            datetime.strptime(args[key], "%Y-%m-%d")
                            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                            .timestamp()
                        )
                    except ValueError as error:
                        raise ToolFailure("invalid_arguments", "日期必须是YYYY-MM-DD") from error
                    conditions.append("m.created_at" + comparison + "%s")
                    values.append(day)
            anchor = args.get("anchor_message_id")
            if anchor:
                known = {str(v["message_id"]) for v in self.context}
                known.update(str(v.get("reference", {}).get("message_id")) for v in self.current)
                if anchor not in known:
                    raise ToolFailure("history_anchor_denied", "锚点必须来自当前上下文或已核实引用")
                if not anchor.isdigit():
                    answer = conn.execute(
                        ANSWER_SELECT + ANSWER_FROM + " WHERE " + ANSWER_VISIBLE + " AND t.id=%s",
                        (*values[:2], anchor),
                    ).fetchone()
                    if not answer:
                        raise ToolFailure("history_anchor_denied", "锚点无法核实")
                    anchor = str(answer["message_id"])
                conditions.append("m.id=%s")
                values.append(int(anchor))
            query = args.get("query", "").strip()
            message_sql = (
                "SELECT m.*"
                + MESSAGE_FROM
                + " WHERE "
                + MESSAGE_VISIBLE
                + " AND "
                + " AND ".join(conditions)
                + " AND m.outbound_task_id IS NULL AND m.metadata->>'message_kind'<>'pat'"
            )
            message_order = " ORDER BY m.created_at DESC,m.id DESC LIMIT 80"
            rows = (
                matching(
                    conn,
                    message_sql,
                    "concat_ws(' ',m.text,(SELECT transcript FROM incoming_media im WHERE im.message_id=m.id AND im.state='ready'))",
                    message_order,
                    values,
                    query,
                )
                if query and not anchor
                else conn.execute(message_sql + message_order, tuple(values)).fetchall()
            )
            rows = [row for row in rows if input_allowed(self.manager, row, conn)]
            ids = [row["id"] for row in rows]
            answers = []
            if ids:
                answers = conn.execute(
                    ANSWER_SELECT
                    + ANSWER_FROM
                    + " WHERE "
                    + ANSWER_VISIBLE
                    + " AND t.completed_at<=%s AND (t.message_id=ANY(%s) OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(t.handled_message_ids) h WHERE h::bigint=ANY(%s))) "
                    "ORDER BY t.completed_at DESC LIMIT 80",
                    (*values[:2], self.boundary, ids, ids),
                ).fetchall()
            if query and not anchor:
                extra, extra_values = [], []
                if self.group and not discussion:
                    extra.append("m.sender=%s")
                    extra_values.append(self.target)
                elif discussion_members:
                    extra.append("m.sender=ANY(%s)")
                    extra_values.append(list(discussion_members))
                for key, comparison in (("after", ">="), ("before", "<")):
                    if key in args:
                        extra.append("t.completed_at" + comparison + "%s")
                        extra_values.append(
                            datetime.strptime(args[key], "%Y-%m-%d")
                            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                            .timestamp()
                        )
                matches = matching(
                    conn,
                    ANSWER_SELECT
                    + ANSWER_FROM
                    + " WHERE "
                    + ANSWER_VISIBLE
                    + " AND t.completed_at<=%s"
                    + (" AND " + " AND ".join(extra) if extra else ""),
                    "t.content",
                    " ORDER BY t.completed_at DESC LIMIT 80",
                    (*values[:2], self.boundary, *extra_values),
                    query,
                )
                answers = list({row["id"]: row for row in [*answers, *matches]}.values())
            units = answer_rounds(self.manager, conn, self.task, answers)
            covered = {v["message_id"] for unit in units for v in unit}
            if not self.group or discussion:
                for row in rows:
                    if row["id"] not in covered:
                        value = item(self.manager, row, conn)
                        value.update(
                            history_round="m:" + str(row["id"]), processing_state="historical_message"
                        )
                        units.append([value])
            if self.group and units:
                names = conn.execute(
                    "SELECT member_id,group_name,profile_json->>'nickname' AS nickname FROM reply_participant_profiles WHERE account_id=%s AND conversation_id=%s AND owner_wxid=(SELECT wxid FROM accounts WHERE id=account_id)",
                    tuple(values[:2]),
                ).fetchall()
                names = {r["member_id"]: r["group_name"] or r["nickname"] for r in names}
                for unit in units:
                    for value in unit:
                        if names.get(value["sender"]):
                            value["speaker_name"] = names[value["sender"]]
            result, used, count = [], 0, 0

            def relevance(unit):
                return max(order(v) for v in unit)

            for unit in sorted(units, key=relevance, reverse=True):
                for value in unit:
                    value.pop("images", None)
                    if value.get("message_kind") in ("image", "emoji"):
                        value["media_status"] = "historical_image_omitted"
                cost = tokens(unit)
                if len(result) >= 8:
                    break
                if count + len(unit) <= 40 and used + cost <= 11600:
                    result.append(unit)
                    used, count = used + cost, count + len(unit)
            if time.monotonic() - start > 2:
                raise ToolFailure("history_timeout", "历史查询超时，未取得可确认的结果")
            return {
                "ok": True,
                "data": {
                    "rounds": result,
                    "scope": "discussion" if discussion else "self",
                    "query": query,
                    "omitted_capacity": bool(units) and not result,
                    "has_more": len(result) < len(units) or len(rows) == 80,
                    "tokens": used,
                },
                "source": "本会话已保存原文",
                "retrieved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            }

    def run(self, args, call_id):
        self.check()
        with self.manager.store.transaction() as conn:
            reads = self.locked(conn)
            existing = next((v for v in reads if v["call_id"] == call_id), None)
            if existing:
                if existing["arguments"] != args:
                    raise ProviderError("history_call_changed")
                if existing.get("result"):
                    return context_snapshot.hydrate(self.manager, conn, self.task, existing["result"])
            elif len(reads) >= 2:
                return {
                    "ok": False,
                    "error": {
                        "code": "history_limit",
                        "message": "本轮已用完两次历史回查，请依据现有证据回复或澄清",
                    },
                }
            else:
                reads.append({"call_id": call_id, "arguments": args})
                conn.execute(
                    "UPDATE reply_context_snapshots SET recall=%s WHERE task_id=%s",
                    (dumps(reads), self.task["id"]),
                )
        try:
            self.validate(args)
            result = self.query(args)
        except ToolFailure as error:
            result = {"ok": False, "error": {"code": error.code, "message": error.message}}
        except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable):
            result = {
                "ok": False,
                "error": {"code": "history_timeout", "message": "历史查询超时，不能确认是否存在相关记录"},
            }
        self.check()
        with self.manager.store.transaction() as conn:
            reads = self.locked(conn)
            next(v for v in reads if v["call_id"] == call_id)["result"] = context_snapshot.pack(result)
            conn.execute(
                "UPDATE reply_context_snapshots SET recall=%s WHERE task_id=%s",
                (dumps(reads), self.task["id"]),
            )
        return result

    def hydrate(self, value):
        self.check()
        with self.manager.store.connect() as conn:
            return context_snapshot.hydrate(self.manager, conn, self.task, value)
