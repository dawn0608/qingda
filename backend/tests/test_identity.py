import json

from test_automation import NOW, deliver, setup


def test_confirmed_alias_reuses_stable_identity_and_history_without_replying_twice(tmp_path):
    s, m, a, conv = setup(tmp_path)
    other = m.add_account("second-terminal")
    historical = {
        "TypeName": "AddMsg",
        "Appid": "second-terminal",
        "Wxid": "wxid_me",
        "Data": {
            "NewMsgId": "98765",
            "MsgType": 1,
            "FromUserName": {"string": "friend"},
            "ToUserName": {"string": "wxid_me"},
            "Content": {"string": "终端历史"},
            "CreateTime": NOW - 100,
        },
    }
    s.receive(json.dumps(historical).encode(), origin="legacy", now=NOW - 90)
    m.process_events()
    canonical = m.verify_identity("second-terminal", {"wxid": "wxid_me"})
    assert canonical["id"] == a["id"]
    assert m.account(other["id"])["enabled"] == 0
    assert s.query("SELECT account_id,conversation_id FROM messages WHERE platform_id=%s", ("98765",))[0] == {
        "account_id": a["id"],
        "conversation_id": conv["id"],
    }
    m.configure_automation(a["id"], "简洁回复", True)
    deliver(s, m, 1)
    repeat = s.event(s.query("SELECT id FROM events WHERE message_id='1'")[0]["id"])
    repeat["Appid"] = "second-terminal"
    s.receive(json.dumps(repeat).encode(), now=NOW + 3)
    m.process_events(now=NOW + 3)
    assert (
        len(
            [
                t
                for t in s.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
                if t["message_id"] and t["kind"] == "auto"
            ]
        )
        == 1
    )
