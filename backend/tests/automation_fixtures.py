"""默认自动回复及历史问候的隔离夹具；不连接微信或模型。"""

import json

from qingda import automation_defaults
from qingda.contact_changes import monitor_result, record_evidence
from qingda.contacts import snapshot
from test_contact_discovery import setup


async def active(tmp_path):
    result = await setup(tmp_path)
    store, manager, gateway, aid, clock, worker, calls = result
    with store.transaction() as conn:
        conn.execute("UPDATE account_contacts SET list_updated_at=%s WHERE account_id=%s", (clock[0], aid))
        conn.execute(
            "INSERT INTO contact_entries(account_id,peer_id,kind,display_name,sort_key,owner_wxid) VALUES(%s,'old','friend','旧好友','old','new-wxid')",
            (aid,),
        )
        store.set_setting("automation_defaults_active", True, conn)
        assert automation_defaults.initialize(manager, conn, aid)
    # 该夹具验证已启用会话，保留显式开启用于后续人工选择回归。
    old = manager.ensure_conversation(aid, "old")
    manager.set_auto_reply(old["id"], True)
    return result


def verified(store, manager, aid, peer, clock, restored=False):
    with store.transaction() as conn:
        automation_defaults.confirmed(manager, conn, aid, peer)
        current = snapshot(conn, aid)
        proof = record_evidence(
            conn, current, peer, "isolated_greeting", clock[0], clock[0], {"present": True}
        )
        monitor_result(
            conn, {**current, "peer_id": peer, "attempted_at": clock[0]}, clock[0], "verified", "", proof
        )


def event(clock, peer="new", mt=1, content="你好", outgoing=False, mid="4001"):
    return {
        "TypeName": "AddMsg",
        "Appid": "new-terminal",
        "Wxid": "new-wxid",
        "Data": {
            "NewMsgId": mid,
            "MsgType": mt,
            "FromUserName": {"string": "new-wxid" if outgoing else peer},
            "ToUserName": {"string": peer if outgoing else "new-wxid"},
            "Content": {"string": content},
            "CreateTime": int(clock[0]),
        },
    }


def receive(store, manager, value):
    store.receive(json.dumps(value).encode())
    manager.process_events()


def system(template, links=""):
    return (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template><template>'
        + template
        + "</template><link_list>"
        + links
        + "</link_list></content_template></sysmsgtemplate></sysmsg>"
    )
