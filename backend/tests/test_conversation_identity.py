import json

import pytest
from contact_fixtures import setup_directory


def test_self_conversation_is_hidden_without_changing_account_profile(tmp_path):
    client, store, manager, aid, _ = setup_directory(tmp_path)
    profile = {
        "wxid": "me",
        "nickName": "WeMind",
        "alias": "wechat_alias",
        "smallHeadImgUrl": "https://wx.qlogo.cn/mmhead/self/132",
        "bigHeadImgUrl": "https://wx.qlogo.cn/mmhead/self/0",
    }
    with store.connect() as conn:
        conn.execute(
            "UPDATE accounts SET nickname=%s,profile_json=%s WHERE id=%s",
            ("旧昵称", json.dumps(profile), aid),
        )
    conversation = manager.ensure_conversation(aid, "me")
    path = f"/wechat/api/v1/conversations?account_id={aid}"
    rows = client.get(path).json()
    assert rows == []
    assert client.app.state.manager.conversation(conversation["id"])["chat_visible"] is False
    assert json.loads(manager.account(aid)["profile_json"])["smallHeadImgUrl"] == profile["smallHeadImgUrl"]
    for query in ("WeMind", "wechat_alias", "me"):
        assert client.get(path + "&q=" + query).json() == []


@pytest.mark.parametrize("peer,name", [("filehelper", "文件传输助手"), ("weixin", "微信团队")])
def test_system_conversations_are_hidden_without_deleting_history(tmp_path, peer, name):
    client, _, manager, aid, _ = setup_directory(tmp_path)
    conversation = manager.ensure_conversation(aid, peer)
    path = f"/wechat/api/v1/conversations?account_id={aid}"
    assert client.get(path).json() == []
    assert client.app.state.manager.conversation(conversation["id"])["chat_visible"] is False
    for query in (name, peer):
        assert client.get(path + "&q=" + query).json() == []


def test_self_identity_is_scoped_to_owner_and_falls_back_when_profile_missing(tmp_path):
    client, store, manager, aid, _ = setup_directory(tmp_path)
    second = manager.add_account("wx_second_terminal")
    manager.verify_identity("wx_second_terminal", {"wxid": "second-me", "nickName": "第二账号"})
    manager.ensure_conversation(aid, "me")
    manager.ensure_conversation(second["id"], "second-me")
    other_me = manager.ensure_conversation(second["id"], "me")
    with store.connect() as conn:
        conn.execute("UPDATE accounts SET nickname=%s WHERE id=%s", ("主账号", aid))
        conn.execute(
            "INSERT INTO contact_entries(account_id,peer_id,kind,display_name,sort_key,profile_json) VALUES(%s,%s,%s,%s,%s,%s)",
            (
                second["id"],
                "me",
                "friend",
                "我的好友",
                "friend",
                '{"smallHeadImgUrl":"https://wx.qlogo.cn/friend"}',
            ),
        )
    rows = {
        r["peer_id"]: r for r in client.get(f"/wechat/api/v1/conversations?account_id={second['id']}").json()
    }
    assert "second-me" not in rows
    assert rows["me"]["id"] == other_me["id"]
    assert rows["me"]["display_name"] == "我的好友"
    assert rows["me"]["profile"]["smallHeadImgUrl"] == "https://wx.qlogo.cn/friend"
    assert client.get(f"/wechat/api/v1/conversations?account_id={aid}").json() == []
    with store.connect() as conn:
        conn.execute("UPDATE accounts SET nickname='' WHERE id=%s", (aid,))
    assert client.get(f"/wechat/api/v1/conversations?account_id={aid}").json() == []


@pytest.mark.parametrize("peer", ["uncatalogued-user", "uncatalogued@chatroom"])
def test_uncatalogued_conversation_uses_cached_remark_and_searches_original_name(tmp_path, peer):
    client, store, manager, aid, _ = setup_directory(tmp_path)
    conv = manager.ensure_conversation(aid, peer)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO chat_profiles(account_id,peer_id,profile_json,state) VALUES(%s,%s,%s,'ready')",
            (aid, peer, json.dumps({"nickName": "原始名称", "remark": "  业务备注  "})),
        )
    path = f"/wechat/api/v1/conversations?account_id={aid}"
    assert client.get(path).json()[0]["display_name"] == "业务备注"
    for query in ("业务备注", "原始名称", peer):
        assert [row["id"] for row in client.get(path + "&q=" + query).json()] == [conv["id"]]


@pytest.mark.parametrize("peer,kind", [("alice", "friend"), ("saved@chatroom", "group")])
def test_directory_remark_changes_override_stale_chat_cache_and_are_account_scoped(tmp_path, peer, kind):
    client, store, manager, aid, _ = setup_directory(tmp_path)
    manager.ensure_conversation(aid, peer)
    other = manager.add_account("wx_second_terminal")
    manager.verify_identity("wx_second_terminal", {"wxid": "second-me"})
    manager.ensure_conversation(other["id"], peer)
    with store.connect() as conn:
        for account_id, remark in ((aid, "旧缓存备注"), (other["id"], "另一账号备注")):
            conn.execute(
                "INSERT INTO chat_profiles(account_id,peer_id,profile_json,state) VALUES(%s,%s,%s,'ready')",
                (account_id, peer, json.dumps({"nickName": "缓存原名", "remark": remark})),
            )
        conn.execute(
            "INSERT INTO contact_entries(account_id,peer_id,kind,display_name,sort_key,profile_json) VALUES(%s,%s,%s,%s,%s,%s)",
            (aid, peer, kind, "旧显示名", peer, "{}"),
        )
    path = f"/wechat/api/v1/conversations?account_id={aid}"
    for remark, expected in (
        ("业务备注", "业务备注"),
        ("新备注", "新备注"),
        ("", "原始名称"),
        ("  ", "原始名称"),
        ("\t\n\u3000", "原始名称"),
    ):
        with store.connect() as conn:
            conn.execute(
                "UPDATE contact_entries SET profile_json=%s,profile_state='ready' WHERE account_id=%s AND peer_id=%s",
                (json.dumps({"nickName": "原始名称", "remark": remark}), aid, peer),
            )
        assert client.get(path).json()[0]["display_name"] == expected
        assert client.get(path + "&q=原始名称").json()[0]["peer_id"] == peer
        assert (
            client.get(f"/wechat/api/v1/conversations?account_id={other['id']}").json()[0]["display_name"]
            == "另一账号备注"
        )
