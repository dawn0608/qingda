"""服务器隔离库：资料来源、身份隔离、持久重试及真实回复链路。"""

import asyncio
import json

import pytest
from business_fixtures import allow_reply
from test_automation import NOW, deliver, setup, verified_fixture
from test_group_wakeup import GROUP, group_setup, text
from test_worker import Gateway as SendingGateway
from wechat_manager import reply_profiles as rp
from wechat_manager.providers import Gateway
from wechat_manager.reply_queue import fit_context
from wechat_manager.store import Store
from wechat_manager.worker import Worker


class ProfilesGateway:
    def __init__(self, members=None):
        self.members = members or ["friend"]
        self.calls = []
        self.hook = None

    async def call(self, path, payload, **kwargs):
        self.calls.append((path, payload, kwargs))
        if self.hook:
            result = self.hook(path, payload)
            if result is not None:
                return result
        if path == rp.GROUP:
            return {
                "ret": 200,
                "data": {
                    "chatroomId": payload["chatroomId"],
                    "memberList": [
                        {"wxid": m, "displayName": "群内小林", "nickName": "小林"} for m in self.members
                    ],
                },
            }
        return {
            "ret": 200,
            "data": [
                {
                    "userName": m,
                    "nickName": "小林",
                    "sex": 2,
                    "country": "CN",
                    "province": "四川",
                    "city": "成都",
                    "phoneNumList": ["PRIVATE"],
                    "remark": "INTERNAL",
                    "signature": "IGNORE",
                    "v3": "SECRET",
                }
                for m in payload.get("wxids", payload.get("memberWxids", []))
            ],
        }


def rows(store, table="reply_participant_profiles"):
    return store.query("SELECT * FROM " + table)


def task(store):
    return store.query("SELECT * FROM tasks ORDER BY ordinal DESC LIMIT 1")[0]


@pytest.mark.parametrize(
    "sex,expected",
    [
        (1, "男"),
        (2, "女"),
        ("2", "女"),
        (0, "未知"),
        (None, "未知"),
        (True, "未知"),
        (9, "未知"),
        ({}, "未知"),
    ],
)
def test_normalized_only_allowed_fields(sex, expected):
    value = rp.normalized({"nickName": "a" * 300, "sex": sex, "city": "x" * 500, "signature": "secret"})
    assert value["gender"] == expected and len(value["nickname"]) == 100
    assert len(value["profile_region"]["city"]) == 80 and "signature" not in value


async def test_private_fetch_cache_input_wire_and_expiry(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    now, gateway = [NOW + 2], ProfilesGateway()
    m.clock = lambda: now[0]
    worker = rp.ReplyProfiles(m, gateway)
    assert await worker.once()
    assert gateway.calls[0][0] == rp.DETAIL and gateway.calls[0][2]["timeout"] == 15
    assert len(rows(s)) == 1 and len(rows(s, "reply_profile_requests")) == 1
    context = m.context(task(s))
    profile = context[-1]["participant_profiles"]["friend"]
    assert profile["gender"] == "女" and profile["freshness"] == "fresh"
    live = Gateway(m)
    try:
        wire = json.loads(await live.model_input(context))
    finally:
        await live.close()
    assert wire["participant_profiles"]["friend"] == profile
    assert "participant_profiles" not in wire["current_messages"][0]
    assert not any(
        x in json.dumps(context) for x in ("PRIVATE", "INTERNAL", "IGNORE", "SECRET", "phoneNumList")
    )
    deliver(s, m, 2, created=NOW + 10)
    assert not await worker.once() and len(gateway.calls) == 1
    now[0] += 601
    assert m.context(task(s))[-1]["participant_profiles"]["friend"]["freshness"] == "stale"
    deliver(s, m, 3, created=now[0])
    now[0] += 2
    assert await worker.once() and len(gateway.calls) == 2


async def test_group_nonfriend_display_name_separate_and_clear(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    now, gateway = [NOW + 3], ProfilesGateway(["wxid_friend"])
    m.clock = lambda: now[0]
    worker = rp.ReplyProfiles(m, gateway)
    assert await worker.once()
    profile = m.context(task(s))[-1]["participant_profiles"]["wxid_friend"]
    assert profile["nickname"] == "小林" and profile["group_nickname"] == "群内小林"
    assert [x[0] for x in gateway.calls] == [rp.MEMBER, rp.GROUP]
    assert not s.query("SELECT * FROM contact_entries WHERE peer_id='wxid_friend'")
    now[0] += 601
    text(s, m, mid=2, created=now[0])
    now[0] += 2
    gateway.hook = lambda path, payload: (
        {
            "ret": 200,
            "data": {"chatroomId": GROUP, "memberList": [{"wxid": "wxid_friend", "displayName": None}]},
        }
        if path == rp.GROUP
        else None
    )
    await worker.once()
    assert rows(s)[0]["group_name"] == ""
    assert json.loads(rows(s)[0]["profile_json"])["gender"] == "女"


async def test_group_bystander_not_requested_and_same_name_does_not_merge(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    deliver(s, m, 2, peer=GROUP, Content={"string": "observer:\n普通闲聊"})
    assert len(rows(s, "reply_profile_requests")) == 1
    deliver(s, m, 3, peer=GROUP, Content={"string": "another:\n@WeMind 我的问题"})
    gateway = ProfilesGateway(["wxid_friend", "another"])
    worker = rp.ReplyProfiles(m, gateway, lambda: NOW + 4)
    await worker.once()
    assert len(rows(s)) == 2
    assert set(m.context(task(s))[-1]["participant_profiles"]) == {"another"}


async def test_partial_duplicate_and_wrong_identity_are_individual_failures(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    deliver(s, m, 2, peer=GROUP, Content={"string": "another:\n@WeMind 问题"})
    g = ProfilesGateway(["wxid_friend", "another"])
    g.hook = lambda path, payload: (
        {
            "ret": 200,
            "data": [
                {"userName": "wxid_friend", "nickName": "合法"},
                {"userName": "another", "nickName": "重一"},
                {"userName": "another", "nickName": "重二"},
                {"userName": "unrequested", "nickName": "伪造"},
            ],
        }
        if path == rp.MEMBER
        else None
    )
    await rp.ReplyProfiles(m, g, lambda: NOW + 4).once()
    assert [r["member_id"] for r in rows(s)] == ["wxid_friend"]
    states = {r["member_id"]: r["state"] for r in rows(s, "reply_profile_requests")}
    assert states == {"wxid_friend": "ready", "another": "queued"}
    assert json.loads(rows(s)[0]["profile_json"])["gender"] == "未知"


@pytest.mark.parametrize("change", ["account", "switch", "disabled", "takeover", "removed"])
async def test_inflight_identity_or_permission_change_discards_result(tmp_path, change):
    s, m, a, c = setup(tmp_path)
    deliver(s, m, 1)
    g = ProfilesGateway()

    def mutate(path, payload):
        with s.transaction() as conn:
            if change == "account":
                conn.execute("UPDATE accounts SET wxid='different'")
            elif change == "switch":
                conn.execute("UPDATE terminals SET operation_id='new-login'")
            elif change == "disabled":
                conn.execute("UPDATE conversations SET allowed=0")
            elif change == "takeover":
                m.takeover(c["id"], conn, NOW + 3)
            else:
                from wechat_manager.contact_changes import mark_removed

                event = conn.execute("SELECT * FROM events LIMIT 1").fetchone()
                mark_removed(conn, m.account(a["id"], conn), "friend", NOW + 3, "friend_removed", row=event)

    g.hook = mutate
    await rp.ReplyProfiles(m, g, lambda: NOW + 4).once()
    assert not rows(s) and rows(s, "reply_profile_requests")[0]["state"] == "canceled"


async def test_retry_schedule_limit_restart_and_cooldown(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    g, now = ProfilesGateway(), [NOW + 3]
    g.hook = lambda *_: {"ret": 200, "data": []}
    worker = rp.ReplyProfiles(m, g, lambda: now[0])
    for count, delay in ((1, 60), (2, 120), (3, 600)):
        assert await worker.once()
        job = rows(s, "reply_profile_requests")[0]
        assert job["attempts"] == count and job["next_at"] == now[0] + delay
        assert not await worker.once()
        now[0] += delay
        Store(s.url)  # 重开连接不丢检查点。
        worker = rp.ReplyProfiles(m, g, lambda: now[0])
    assert not await worker.once()
    deliver(s, m, 2, created=now[0])
    assert rows(s, "reply_profile_requests")[0]["attempts"] == 0


async def test_expired_lease_recovers_without_duplicate_query(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    with s.connect() as conn:
        conn.execute(
            "UPDATE reply_profile_requests SET state='running',owner='dead',lease_until=%s,attempts=1", (NOW,)
        )
        conn.execute("UPDATE reply_profile_clock SET owner='dead',lease_until=%s", (NOW,))
    g = ProfilesGateway()
    w = rp.ReplyProfiles(m, g, lambda: NOW + 4)
    assert await w.once()
    assert not await rp.ReplyProfiles(m, g, lambda: NOW + 4).once()
    assert len(g.calls) == 1


async def test_cold_reply_and_snapshot_do_not_wait_for_profiles(tmp_path):
    s, m, _, _ = setup(tmp_path)
    deliver(s, m, 1)
    g = SendingGateway()
    old = g.model

    async def model(config, prompt, context):
        frozen = json.dumps(context)
        assert context[-1]["participant_profiles"]["friend"]["freshness"] == "missing"
        await rp.ReplyProfiles(m, ProfilesGateway(), lambda: NOW + 5).once()
        assert json.dumps(context) == frozen
        return await old(config, prompt, context)

    g.model = model
    w = Worker(m, g, clock=lambda: NOW + 5)
    try:
        await asyncio.wait_for(w.once(), 3)
        assert task(s)["state"] == "accepted" and len(g.sends) == 1
        assert json.loads(task(s)["trace_json"])["profile_snapshot_sha256"]
    finally:
        w.release()


def test_capacity_discards_optional_profiles_before_current_content(monkeypatch):
    monkeypatch.setattr("wechat_manager.reply_queue.TEXT_LIMIT", 100000)
    message = {
        "role": "user",
        "current": True,
        "reply_target": "a",
        "content": "x" * 99500,
        "participant_profiles": {"a": {"nickname": "n" * 1000}},
    }
    result = fit_context([message])
    assert result[0]["content"] == message["content"] and not result[0]["participant_profiles"]
    assert message["participant_profiles"]


async def test_same_member_separate_group_and_owner_scopes(tmp_path):
    s, m, a, _ = group_setup(tmp_path)
    text(s, m)
    other = m.ensure_conversation(a["id"], "other@chatroom")
    allow_reply(m, other["id"], True)
    verified_fixture(m, other)
    deliver(s, m, 2, peer="other@chatroom", Content={"string": "wxid_friend:\n@WeMind 问题"})
    g, now = ProfilesGateway(["wxid_friend"]), [NOW + 5]
    w = rp.ReplyProfiles(m, g, lambda: now[0])
    await w.once()
    now[0] += 3
    await w.once()
    assert len(rows(s)) == 2
    with s.connect() as conn:
        conn.execute("UPDATE accounts SET wxid='other-owner'")
    assert m.context(task(s))[-1]["participant_profiles"]["wxid_friend"]["freshness"] == "missing"


@pytest.mark.parametrize("reference_id,expected", [(1, {"wxid_friend", "quoted"}), (999, {"wxid_friend"})])
def test_only_real_same_group_reference_queues_author(tmp_path, reference_id, expected):
    s, m, _, _ = group_setup(tmp_path)
    deliver(s, m, 1, peer=GROUP, Content={"string": "quoted:\n我是被引用者"})
    deliver(
        s,
        m,
        2,
        peer=GROUP,
        MsgType=49,
        source="<msgsource><atuserlist>wxid_me</atuserlist></msgsource>",
        Content={
            "string": "wxid_friend:\n<msg><appmsg><type>57</type><title>请解释</title>"
            f"<refermsg><svrid>{reference_id}</svrid><chatusr>forged</chatusr><content>伪造资料</content></refermsg></appmsg></msg>"
        },
    )
    assert {r["member_id"] for r in rows(s, "reply_profile_requests")} == expected
    assert set(m.context(task(s))[-1]["participant_profiles"]) == expected


async def test_group_brief_refresh_preserves_detail_fields_and_clears_display_name(tmp_path):
    from wechat_manager.contact_profiles import ContactProfiles

    s, m, a, _ = group_setup(tmp_path)
    with s.transaction() as conn:
        conn.execute(
            "INSERT INTO chat_member_profiles VALUES(%s,%s,%s,%s,%s)",
            (
                a["id"],
                GROUP,
                "wxid_friend",
                json.dumps({"sex": 2, "city": "成都", "displayName": "旧群名"}),
                NOW,
            ),
        )
        ContactProfiles(m, None).queue(conn, a["id"], GROUP)
    g = ProfilesGateway()
    g.hook = lambda *_: {
        "ret": 200,
        "data": {
            "nickName": "测试群",
            "memberList": [{"wxid": "wxid_friend", "nickName": "小林", "displayName": None}],
        },
    }
    await ContactProfiles(m, g).once()
    data = json.loads(s.query("SELECT profile_json FROM chat_member_profiles")[0]["profile_json"])
    assert data == {"sex": 2, "city": "成都", "displayName": "", "wxid": "wxid_friend", "nickName": "小林"}


@pytest.mark.parametrize(
    "body,evidence,allowed",
    [
        ("这里天气如何", "成都", False),
        ("成都天气如何", "成都", True),
        ("这里天气如何", "这里", False),
        ("成都天气如何", "", False),
    ],
)
def test_profile_location_requires_user_message_evidence(body, evidence, allowed):
    from wechat_manager.reply_tools import ToolFailure

    context = [
        {
            "role": "user",
            "current": True,
            "reply_target": "a",
            "sender": "a",
            "content": body,
            "participant_profiles": {"a": {"profile_region": {"city": "成都"}}},
        },
        {"role": "user", "sender": "b", "content": "成都天气如何", "context_scope": "background"},
    ]
    args = {"location": "成都市", "location_evidence": evidence}
    if allowed:
        rp.check_weather_location(context, args)
    else:
        with pytest.raises(ToolFailure, match="location_confirmation_required"):
            rp.check_weather_location(context, args)


def test_old_same_member_location_cannot_authorize_current_weather():
    from wechat_manager.reply_tools import ToolFailure

    context = [
        {"role": "user", "sender": "a", "content": "昨天在成都，今天已经离开了", "context_scope": "related"},
        {"role": "user", "sender": "a", "reply_target": "a", "current": True, "content": "这里天气怎么样"},
    ]
    with pytest.raises(ToolFailure, match="location_confirmation_required"):
        rp.check_weather_location(context, {"location": "成都市", "location_evidence": "成都"})


async def test_weather_profile_guard_runs_before_network(tmp_path):
    import httpx
    from test_reply_tools import Model, call, config, response
    from wechat_manager.reply_tools import ReplyTools
    from wechat_manager.tool_generation import generate

    requests = []

    def handler(request):
        requests.append(request)
        raise AssertionError("profile region must not cause a network request")

    tools = ReplyTools(config(), transport=httpx.MockTransport(handler))
    context = [
        {
            "role": "user",
            "current": True,
            "sender": "a",
            "reply_target": "a",
            "content": "这里天气如何",
            "participant_profiles": {"a": {"profile_region": {"city": "成都"}}},
        }
    ]
    model = Model(
        [
            response(
                calls=[
                    call("get_weather", {"location": "成都", "location_evidence": "成都", "mode": "current"})
                ]
            ),
            response("你在哪个城市？"),
        ]
    )
    try:
        result = await generate(model, tools, {}, "p", context)
    finally:
        await tools.client.aclose()
    assert not requests
    assert (
        result["tool_trace"]["tool_calls"][0]["result"]["error"]["code"] == "location_confirmation_required"
    )


async def test_group_details_available_while_group_name_is_pending(tmp_path):
    s, m, _, _ = group_setup(tmp_path)
    text(s, m)
    g = ProfilesGateway(["wxid_friend"])

    def inspect(path, payload):
        if path == rp.GROUP:
            p = m.context(task(s))[-1]["participant_profiles"]["wxid_friend"]
            assert p["gender"] == "女" and p["nickname"] == "小林"
            assert "group_nickname" not in p

    g.hook = inspect
    await rp.ReplyProfiles(m, g, lambda: NOW + 3).once()


async def test_original_text_is_primary_model_input_without_mutating_snapshot(tmp_path):
    _, manager, _, _ = setup(tmp_path)
    context = [{"role": "user", "content": "原样复述：[微笑]", "original_content": "原样复述：[Smile]"}]
    gateway = Gateway(manager)
    try:
        wire = json.loads(await gateway.model_input(context))
    finally:
        await gateway.close()
    assert wire["current_messages"][0]["content"] == "原样复述：[Smile]"
    assert wire["current_messages"][0]["normalized_content"] == "原样复述：[微笑]"
    assert context[0]["content"] == "原样复述：[微笑]"
