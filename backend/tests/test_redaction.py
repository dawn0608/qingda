from wechat_manager.protocol import normalize, redact
from wechat_manager.store import message_identity


def test_wrapped_identifiers_and_xml_credentials_are_redacted():
    data = {
        "app_id": "private-app-id-long",
        "Data": {
            "FromUserName": {"string": "wxid_private_sender"},
            "ToUserName": {"string": "wxid_private_me"},
            "Content": {"string": '<img aeskey="secret-key" cdnmidimgurl="private-url" />'},
        },
        "phone": "13800000000",
    }
    shown = str(redact(data))
    for secret in [
        "private-app-id-long",
        "wxid_private_sender",
        "wxid_private_me",
        "secret-key",
        "private-url",
        "13800000000",
    ]:
        assert secret not in shown


def test_unreliable_numeric_values_do_not_become_message_identity_or_time():
    assert message_identity({"Data": {"NewMsgId": "not-an-id"}})[1] is None
    assert message_identity({"Data": {"NewMsgId": True}})[1] is None
    assert normalize({"Data": {"CreateTime": float("inf")}})["created_at"] is None
    assert redact({"number": float("inf")}) == {"number": "[无效数值]"}
