"""服务器 PostgreSQL 隔离库：长上下文、检索范围和可恢复原文清单。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_automation import NOW, deliver, setup
from test_group_wakeup import GROUP, group_setup, text
from wechat_manager import context_budget, context_snapshot
from wechat_manager.history_tool import HistoryReader
from wechat_manager.providers import ProviderError
from wechat_manager.reply_queue import fit_context


def accept(store, mid, answer, at):
    task = store.query("SELECT * FROM tasks WHERE message_id=%s", (mid,))[0]
    with store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state='accepted',content=%s,completed_at=%s,response_json=%s WHERE id=%s",
            (answer, at, json.dumps({"data": {"newMsgId": str(900000 + mid)}}), task["id"]),
        )
        conn.execute(
            "INSERT INTO reply_decisions(task_id,input_revision,action,reason,close_dialogue,policy_version,updated_at) VALUES(%s,0,'reply','answer',0,'test',%s)",
            (task["id"], at),
        )
        conn.execute("UPDATE tasks SET handled_message_ids=%s WHERE id=%s", (json.dumps([mid]), task["id"]))
    return task["id"]


def seed(store, manager, count, group=False):
    for i in range(1, count + 1):
        kwargs = dict(created=NOW + i, received=NOW + i + 0.1)
        if group:
            text(store, manager, i, body=f"WeMind，历史预算{i}元", **kwargs)
        else:
            deliver(store, manager, i, Content={"string": f"历史预算{i}元"}, **kwargs)
        mid = store.query("SELECT id FROM messages WHERE platform_id=%s", (str(i),))[0]["id"]
        accept(store, mid, f"预算为{i}元", NOW + i + 0.2)


def reader(store, manager):
    task = dict(store.query("SELECT * FROM tasks ORDER BY ordinal DESC LIMIT 1")[0])
    task["_owner"] = "context-test"
    with store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET state='generating',lease_owner=%s WHERE id=%s", (task["_owner"], task["id"])
        )
    context = manager.context(task)
    context = context_snapshot.freeze(manager, task, context)
    task["_context_frozen"] = True
    return task, context, HistoryReader(manager, task, context)


def test_private_12_complete_rounds_and_older_query(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    seed(store, manager, 120)
    deliver(
        store,
        manager,
        121,
        created=NOW + 121,
        received=NOW + 121.1,
        Content={"string": "最早的历史预算1元是什么"},
    )
    task, context, history = reader(store, manager)
    old = [v for v in context if not v.get("current")]
    assert len({v["history_round"] for v in old}) == 12
    assert len(old) == 24
    assert next(v for v in context if v.get("current"))["history_selection"]["has_more"]
    assert not any(v["content"] == "历史预算1元" for v in old)
    result = history.run({"query": "历史预算1元"}, "read-one")
    assert result["ok"] and result["data"]["rounds"][0][0]["content"] == "历史预算1元"
    history.run({"query": "不存在的记录"}, "read-two")
    assert history.run({"query": "预算"}, "read-three")["error"]["code"] == "history_limit"
    manifest = store.query(
        "SELECT manifest,recall FROM reply_context_snapshots WHERE task_id=%s", (task["id"],)
    )[0]
    assert "历史预算1元" not in manifest["manifest"]
    # 查询词可留审计，完整返回正文只保存哈希及引用。
    assert "预算为1元" not in manifest["recall"]
    restarted = HistoryReader(manager, task, context)
    assert restarted.run({"query": "历史预算1元"}, "read-one") == result
    assert restarted.run({"query": "预算"}, "read-four")["error"]["code"] == "history_limit"


def test_group_member_history_survives_500_bystander_messages(tmp_path):
    store, manager, _, _ = group_setup(tmp_path)
    seed(store, manager, 65, True)
    for i in range(66, 568):
        deliver(
            store,
            manager,
            i,
            peer=GROUP,
            created=NOW + i,
            received=NOW + i + 0.1,
            Content={"string": f"bystander:\n闲聊{i}"},
        )
    text(store, manager, 568, body="WeMind，我之前的预算是多少", created=NOW + 568, received=NOW + 568.1)
    _, context, history = reader(store, manager)
    related = [v for v in context if v.get("context_scope") == "related"]
    assert len({v["history_round"] for v in related}) == 8
    assert all(v["sender"] in ("wxid_friend", "self") for v in related)
    assert len([v for v in context if v.get("context_scope") == "background"]) <= 6
    assert history.run({"query": "闲聊"}, "private-read")["data"]["rounds"] == []


def test_search_voice_and_keyword_intersection_prefers_latest_correction(tmp_path):
    from types import SimpleNamespace

    from wechat_manager.media import IncomingMedia

    store, manager, _, _ = setup(tmp_path)
    manager.media = IncomingMedia(manager, SimpleNamespace(data_dir=tmp_path), transcriber=object())
    deliver(
        store,
        manager,
        1,
        MsgType=34,
        Content={"string": "<msg><voicemsg/></msg>"},
        created=NOW + 1,
        received=NOW + 1.1,
    )
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO incoming_media(message_id,state,kind,transcript,updated_at) VALUES(1,'ready','voice',%s,%s)",
            ("青杉项目预算800元，票号为ZX90", NOW + 1.2),
        )
    accept(store, 1, "预算800元", NOW + 1.3)
    deliver(
        store,
        manager,
        2,
        Content={"string": "更正，青杉项目的预算是1000元"},
        created=NOW + 2,
        received=NOW + 2.1,
    )
    accept(store, 2, "预算已更正为1000元", NOW + 2.2)
    deliver(
        store, manager, 3, Content={"string": "回查之前的预算和票号"}, created=NOW + 3, received=NOW + 3.1
    )
    _, _, history = reader(store, manager)
    budget = history.run({"query": "青杉 预算"}, "budget")
    assert budget["ok"] and "1000" in budget["data"]["rounds"][0][0]["content"]
    voice = history.run({"query": "票号 ZX90"}, "voice")
    assert voice["ok"] and voice["data"]["rounds"][0][0]["message_kind"] == "voice"
    assert "ZX90" in voice["data"]["rounds"][0][0]["content"]


def test_recent_clarification_survives_unanswered_materials(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    seed(store, manager, 1)
    store.query("UPDATE reply_decisions SET reason='clarify' RETURNING task_id")
    for mid in range(2, 18):
        deliver(
            store,
            manager,
            mid,
            Content={"string": "未回答的材料" + str(mid)},
            created=NOW + mid,
            received=NOW + mid + 0.1,
        )
        store.query("UPDATE tasks SET state='failed' WHERE message_id=%s RETURNING id", (mid,))
    deliver(store, manager, 18, Content={"string": "好的"}, created=NOW + 18, received=NOW + 18.1)
    _, context, _ = reader(store, manager)
    pending = [v for v in context if v.get("unresolved_clarification")]
    assert len(pending) == 2 and any(v["role"] == "assistant" for v in pending)
    assert context_budget.tokens([v for v in context if not v.get("current")]) <= context_budget.PRIVATE[2]


def test_discussion_requires_current_evidence_and_exact_topic(tmp_path):
    store, manager, _, _ = group_setup(tmp_path)
    deliver(
        store,
        manager,
        1,
        peer=GROUP,
        created=NOW + 1,
        received=NOW + 1.1,
        Content={"string": "other:\n周五聚餐定在西湖饭店"},
    )
    text(store, manager, 2, body="WeMind，刚才群里讨论的周五聚餐在哪里", created=NOW + 2, received=NOW + 2.1)
    _, context, history = reader(store, manager)
    current = next(v for v in context if v.get("current"))
    denied = history.run(
        {
            "query": "聚餐",
            "scope": "discussion",
            "evidence_message_id": current["message_id"],
            "evidence_quote": "伪造依据",
        },
        "bad",
    )
    assert denied["error"]["code"] == "history_scope_denied"
    allowed = history.run(
        {
            "query": "周五聚餐 地点",
            "scope": "discussion",
            "evidence_message_id": current["message_id"],
            "evidence_quote": "刚才群里讨论的周五聚餐在哪里",
        },
        "good",
    )
    assert allowed["ok"] and allowed["data"]["rounds"][0][0]["sender"] == "other"
    assert allowed["data"]["query"] == "周五聚餐"


def test_snapshot_freezes_selection_and_detects_source_change(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    seed(store, manager, 3)
    deliver(store, manager, 4, created=NOW + 4, received=NOW + 4.1)
    task, context, _ = reader(store, manager)
    assert context_snapshot.freeze(manager, task, [{"content": "不应采用新的清单"}]) == context
    with store.transaction() as conn:
        conn.execute(
            "UPDATE message_contents SET text='已经改变' WHERE id=(SELECT content_id FROM messages WHERE id=1)"
        )
        assert context_snapshot.check(manager, conn, task) == "history_source_changed"


def test_whole_round_budget_preserves_current_and_drops_background_first():
    context = [
        {
            "message_id": 1,
            "role": "user",
            "content": "相关事实",
            "history_round": "r",
            "context_scope": "related",
        },
        {
            "message_id": "answer",
            "role": "assistant",
            "content": "相关回答",
            "history_round": "r",
            "context_scope": "related",
        },
        {
            "message_id": 2,
            "role": "user",
            "content": "闲聊" * 10000,
            "history_round": "b",
            "context_scope": "background",
        },
        {"message_id": 3, "role": "user", "content": "现在的问题", "current": True},
    ]
    fitted = fit_context(context, text_tokens=1000)
    assert [v["message_id"] for v in fitted] == [1, "answer", 3]
    fitted = fit_context(context, text_tokens=context_budget.tokens([context[-1]]) + 2)
    assert fitted == [context[-1]]
    with pytest.raises(ProviderError, match="current_batch_too_large"):
        fit_context([context[-1]], text_tokens=1)


def test_image_base64_not_counted_as_text_tokens():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 1000000}}
            ],
        }
    ]
    assert 1024 <= context_budget.request_tokens(messages) < 2000
    assert context_budget.request_size(messages)["text_bytes"] < 200


def test_same_name_discussion_is_ambiguous_and_identity_scoped(tmp_path):
    store, manager, _, _ = group_setup(tmp_path)
    text(store, manager, 1, body="WeMind，之前小明说过的预算是多少", created=NOW + 1, received=NOW + 1.1)
    task, context, history = reader(store, manager)
    current = next(v for v in context if v.get("current"))
    with store.transaction() as conn:
        owner = manager.account(task["account_id"], conn)["wxid"]
        for member in ("one", "two"):
            conn.execute(
                "INSERT INTO reply_participant_profiles(account_id,owner_wxid,conversation_id,member_id,profile_json,source,updated_at,group_name) VALUES(%s,%s,%s,%s,'{}','test',%s,'小明')",
                (task["account_id"], owner, task["conversation_id"], member, NOW),
            )
    args = {
        "query": "预算",
        "scope": "discussion",
        "evidence_message_id": current["message_id"],
        "evidence_quote": "之前小明说过的预算是多少",
    }
    assert history.run(args, "ambiguous")["error"]["code"] == "ambiguous_history_member"
    with store.transaction() as conn:
        conn.execute("UPDATE reply_participant_profiles SET owner_wxid='old-identity' WHERE member_id='two'")
    assert history.run(args, "valid")["ok"]


def test_history_failure_not_empty_success_and_invalid_dates(tmp_path, monkeypatch):
    import psycopg

    store, manager, _, _ = setup(tmp_path)
    deliver(store, manager, 1)
    _, _, history = reader(store, manager)
    assert (
        history.run({"after": "2026-10-16", "before": "2026-10-15"}, "date")["error"]["code"]
        == "invalid_arguments"
    )

    def timeout(args):
        raise psycopg.errors.QueryCanceled("isolated timeout")

    monkeypatch.setattr(history, "query", timeout)
    result = history.run({"query": "旧约定"}, "timeout")
    assert not result["ok"] and result["error"]["code"] == "history_timeout"


def test_two_process_recall_crash_restore_and_ownership(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    seed(store, manager, 2)
    deliver(store, manager, 3, created=NOW + 3, received=NOW + 3.1)
    task, _, _ = reader(store, manager)
    actor = Path(__file__).with_name("context_process_actor.py")
    env = {
        **os.environ,
        "ACTOR_DATABASE_URL": store.url,
        "ACTOR_TASK_ID": task["id"],
        "ACTOR_OWNER": task["_owner"],
        "PGAPPNAME": "wechat-manager-context-actor",
    }
    first = subprocess.Popen(
        [sys.executable, str(actor), "hold"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert json.loads(first.stdout.readline())["first"] == "历史预算1元"
        first.kill()
        first.communicate(timeout=5)
        with store.transaction() as conn:
            conn.execute("UPDATE tasks SET lease_owner='replacement' WHERE id=%s", (task["id"],))
        stale = subprocess.run(
            [sys.executable, str(actor), "resume"], env=env, capture_output=True, text=True, timeout=10
        )
        assert stale.returncode != 0 and "task_canceled" in stale.stderr
        resumed = subprocess.run(
            [sys.executable, str(actor), "resume"],
            env={**env, "ACTOR_OWNER": "replacement"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert resumed.returncode == 0, resumed.stderr
        assert json.loads(resumed.stdout)["ok"]
        assert (
            len(
                json.loads(
                    store.query("SELECT recall FROM reply_context_snapshots WHERE task_id=%s", (task["id"],))[
                        0
                    ]["recall"]
                )
            )
            == 1
        )
        with store.transaction() as conn:
            conn.execute("UPDATE tasks SET state='canceled' WHERE id=%s", (task["id"],))
        canceled = subprocess.run(
            [sys.executable, str(actor), "resume"],
            env={**env, "ACTOR_OWNER": "replacement"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert canceled.returncode != 0 and "task_canceled" in canceled.stderr
        assert (
            store.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name='wechat-manager-context-actor'"
            )[0]["n"]
            == 0
        )
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)


def test_two_process_snapshot_checks_with_saturated_media_pool(tmp_path):
    store, manager, _, _ = setup(tmp_path)
    seed(store, manager, 3)
    deliver(store, manager, 4, created=NOW + 4, received=NOW + 4.1)
    task, _, _ = reader(store, manager)
    actor = Path(__file__).with_name("context_process_actor.py")
    env = {
        **os.environ,
        "ACTOR_DATABASE_URL": store.url,
        "ACTOR_TASK_ID": task["id"],
        "ACTOR_OWNER": task["_owner"],
        "ACTOR_MEDIA_ROOT": str(tmp_path),
        "PGAPPNAME": "wechat-manager-context-pool-actor",
    }
    actors = [
        subprocess.Popen(
            [sys.executable, str(actor), "pool-check"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for process in actors:
            assert json.loads(process.stdout.readline())["ready"]
        for process in actors:
            process.stdin.write("run\n")
            process.stdin.flush()
        for process in actors:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            assert json.loads(stdout)["ok"]
        assert (
            store.query(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name='wechat-manager-context-pool-actor'"
            )[0]["n"]
            == 0
        )
    finally:
        for process in actors:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


async def test_history_hidden_after_generation_is_checked_at_send_boundary(tmp_path):
    from test_worker import Gateway
    from wechat_manager.callbacks import message_key, put
    from wechat_manager.worker import Worker

    store, manager, _, conv = setup(tmp_path)
    seed(store, manager, 2)
    deliver(store, manager, 3, created=NOW + 3, received=NOW + 3.1)
    gateway = Gateway()

    async def online(_):
        assert store.query("SELECT 1 FROM tasks WHERE state='ready'")
        with store.connect() as conn:
            put(conn, message_key(1), 1, conv["id"], visible=False, event_id=1)
        return True

    gateway.online = online
    worker = Worker(manager, gateway, clock=lambda: NOW + 10)
    try:
        await worker.once()
        assert not gateway.sends
        latest = store.query("SELECT state,reason FROM tasks ORDER BY ordinal DESC LIMIT 1")[0]
        assert latest["state"] == "queued" and latest["reason"] == "private_input_changed"
    finally:
        worker.release()
