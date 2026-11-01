"""隔离关系同步用例；系统通知样例不是手机实测报文。"""

import json
import uuid

import pytest
from business_fixtures import queue_contact_check
from contact_fixtures import setup_directory
from test_contact_removal import delete_event
from wechat_manager.accounts import AccountService
from wechat_manager.worker import Worker


def system_event(store, manager, content, msg_type=10000, *, process=True, **changes):
    event = {
        "TypeName": "AddMsg",
        "Appid": "wx_current_terminal",
        "Wxid": "me",
        "Data": {
            "NewMsgId": str(uuid.uuid4().int)[:18],
            "MsgType": msg_type,
            "FromUserName": {"string": "two-person@chatroom"},
            "ToUserName": {"string": "me"},
            "Content": {"string": content},
            "CreateTime": manager.clock(),
        },
    }
    event.update(changes)
    store.receive(json.dumps(event).encode(), now=manager.clock())
    if process:
        manager.process_events()


@pytest.mark.parametrize(
    "content,msg_type,changes",
    [
        ("你被群主移出了群聊", 1, {}),
        ("转发：你被群主移出了群聊", 10000, {}),
        ("群主将张三移出了群聊", 10000, {}),
        ("该群聊已被群主解散", 10000, {"Wxid": "other-account"}),
        ("该群聊已被群主解散", 10000, {"Appid": "old-terminal"}),
        ('<sysmsg type="unknown"><template>该群聊已被群主解散</template></sysmsg>', 10002, {}),
        (
            '<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template><template>$actor$将$target$移出了群聊</template>'
            '<link_list><link name="target"><memberlist><member><username>other</username></member></memberlist></link></link_list>'
            "</content_template></sysmsgtemplate></sysmsg>",
            10002,
            {},
        ),
        ("对方开启了朋友验证，你还不是他（她）朋友", 10000, {}),
    ],
)
def test_ambiguous_or_untrusted_notifications_do_not_change_membership(tmp_path, content, msg_type, changes):
    c, store, manager, aid, headers = setup_directory(tmp_path)
    manager.ensure_conversation(aid, "two-person@chatroom")
    system_event(store, manager, content, msg_type, **changes)
    row = manager.conversation(1)
    assert row["relationship_status"] == "active" and row["relationship_reason"] == ""


async def test_group_cooldown_survives_relation_changes_and_restart(tmp_path):
    c, store, manager, aid, headers = setup_directory(tmp_path)
    clock = [manager.clock()]
    manager.clock = lambda: clock[0]
    await AccountService(manager, c.app.state.gateway).online(aid)
    conv = manager.ensure_conversation(aid, "two-person@chatroom")
    calls = []

    async def members(*args, **kwargs):
        calls.append(args)
        return {"ret": 200, "data": {"memberList": [{"wxid": "me"}]}}

    c.app.state.gateway.call = members

    queue_contact_check(c, conv["id"])
    worker = Worker(manager, c.app.state.gateway, clock=manager.clock)
    assert await worker.verify_contacts_once()
    system_event(store, manager, "你被群主移出了群聊")
    delete_event(store, manager, "two-person@chatroom")
    assert manager.conversation(conv["id"])["relationship_reason"] == "group_kicked"
    for _ in range(3):
        queue_contact_check(c, conv["id"])
        assert not await worker.verify_contacts_once()
    restarted = type(manager)(type(store)(store.url), clock=manager.clock)
    worker.release()
    clock[0] += 61
    assert await Worker(restarted, c.app.state.gateway, clock=manager.clock).verify_contacts_once()
    assert manager.conversation(conv["id"])["relationship_status"] == "active"
    assert not manager.conversation(conv["id"])["allowed"]
    assert len(calls) == 2


@pytest.mark.parametrize("flag", ["IsHistory", "IsTest"])
def test_historical_system_notification_is_not_relationship_evidence(tmp_path, flag):
    c, store, manager, aid, headers = setup_directory(tmp_path)
    manager.ensure_conversation(aid, "two-person@chatroom")
    data = {
        "NewMsgId": "988776",
        "MsgType": 10000,
        "CreateTime": manager.clock(),
        flag: True,
        "FromUserName": {"string": "two-person@chatroom"},
        "ToUserName": {"string": "me"},
        "Content": {"string": "该群聊已被群主解散"},
    }
    system_event(store, manager, "ignored", Data=data)
    assert manager.conversation(1)["relationship_status"] == "active"
