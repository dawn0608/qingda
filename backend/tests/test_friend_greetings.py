import pytest
from test_inbox import event
from wechat_manager.protocol import normalize


def greeting(**values):
    return event(
        MsgType=37,
        FromUserName={"string": "fmessage"},
        Content={"string": '<msg fromusername="wxid_friend" content="我是lucy" opcode="2" />'},
        **values,
    )


@pytest.mark.parametrize(
    "body",
    [
        '<msg fromusername="someone@stranger" content="问候" opcode="2" />',
        '<msg fromusername="wxid_me" content="问候" opcode="2" />',
        '<msg fromusername="wxid_friend" content="" opcode="2" />',
        '<msg fromusername="wxid_friend" content="问候" opcode="3" />',
        '<!DOCTYPE msg [<!ENTITY x "问候">]><msg content="&x;" />',
    ],
)
def test_unproven_requests_stay_in_original_conversation(body):
    value = greeting()
    value["Data"]["Content"]["string"] = body
    parsed = normalize(value)
    assert parsed["peer"] == "fmessage" and parsed["text"] == ""
    assert not parsed["metadata"].get("friend_greeting")


def test_wrong_sender_or_recipient_cannot_reassign_a_conversation():
    for field in ("FromUserName", "ToUserName"):
        value = greeting()
        value["Data"][field] = {"string": "unrelated"}
        assert not normalize(value)["metadata"].get("friend_greeting")
