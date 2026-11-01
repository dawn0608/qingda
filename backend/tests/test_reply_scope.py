import json

import pytest
from auth_fixtures import login_response
from contact_fixtures import setup_directory


def populate(tmp_path):
    http, store, manager, aid, headers = setup_directory(tmp_path)
    now = manager.clock()
    with store.transaction() as conn:
        for i in range(527):
            conn.execute(
                "INSERT INTO conversations(account_id,peer_id,kind,name,allowed,pause_until,updated_at) VALUES(%s,%s,'private',%s,%s,%s,%s)",
                (aid, f"friend-{i:04}", f"好友 {i:04}", i % 2, now + 900 if i == 521 else 0, now),
            )
        conn.execute(
            "INSERT INTO chat_profiles(account_id,peer_id,profile_json) VALUES(%s,%s,%s)",
            (aid, "friend-0521", json.dumps({"nickName": "原始昵称", "remark": "远端目标 100%_!"})),
        )
        for peer in ("weixin", "filehelper", "me", "gh_hidden"):
            manager.ensure_conversation(aid, peer, conn)
        manager.ensure_conversation(aid, "group@chatroom", conn)
    other = manager.add_account("scope-other")
    manager.verify_identity("scope-other", {"wxid": "scope-other-self"})
    manager.ensure_conversation(other["id"], "friend-0521")
    return http, store, manager, aid, headers


def test_paginated_scope_search_filter_and_account_isolation(tmp_path):
    http, store, manager, aid, _ = populate(tmp_path)
    path = f"/wechat/api/v1/accounts/{aid}/reply-scope"
    first = http.get(path).json()
    assert first["total"] == 527 and len(first["items"]) == 10
    second = http.get(path, params={"page": 2}).json()
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    assert first == http.get(path).json() or [r["id"] for r in first["items"]] == [
        r["id"] for r in http.get(path).json()["items"]
    ]
    for q in ("远端目标", "原始昵称", "100%_!"):
        result = http.get(path, params={"q": q}).json()
        assert result["total"] == 1 and result["items"][0]["peer_id"] == "friend-0521"
    enabled = http.get(path, params={"status": "enabled"}).json()
    disabled = http.get(path, params={"status": "disabled"}).json()
    paused = http.get(path, params={"status": "paused"}).json()
    assert enabled["total"] == 263 and disabled["total"] == 264
    assert paused["total"] == 1 and paused["items"][0]["allowed"] == 1
    assert http.get(path, params={"status": "enabled", "q": "原始昵称"}).json()["total"] == 1
    assert http.get(path, params={"status": "disabled", "q": "原始昵称"}).json()["total"] == 0
    assert http.get(path, params={"kind": "group"}).json()["total"] == 1
    assert all(row["account_id"] == aid for row in first["items"])
    assert http.get(path, params={"page": 999999}).json()["page"] == 53
    empty = http.get(path, params={"q": "不存在", "page": 99}).json()
    assert empty["items"] == [] and empty["total"] == 0 and empty["page"] == 1
    with store.transaction() as conn:
        conn.execute("UPDATE accounts SET ai_enabled=0,online=0 WHERE id=%s", (aid,))
    assert http.get(path, params={"status": "enabled"}).json()["total"] == enabled["total"]
    assert http.get(path, params={"status": "paused"}).json()["total"] == 1
    # 原桥接入口仍返回数组；分页读取不依赖聊天正文。
    with store.connect() as conn:
        conn.execute("DROP TABLE message_views CASCADE")
        conn.execute("DROP VIEW messages CASCADE")
    assert isinstance(http.get(f"/wechat/api/v1/conversations?account_id={aid}").json(), list)
    assert http.get(path).json()["total"] == 527


@pytest.mark.parametrize(
    "params",
    [
        {"kind": "all"},
        {"status": "bad"},
        {"page": 0},
        {"page_size": 15},
        {"page_size": 100},
        {"q": "x" * 2001},
    ],
)
def test_scope_validates_parameters(tmp_path, params):
    http, _, _, aid, _ = setup_directory(tmp_path)
    assert http.get(f"/wechat/api/v1/accounts/{aid}/reply-scope", params=params).status_code == 422


def test_scope_requires_login_and_rejects_missing_account(tmp_path):
    from test_http import client

    http, _ = client(tmp_path)
    assert http.get("/wechat/api/v1/accounts/1/reply-scope").status_code == 401
    login_response(http)
    assert http.get("/wechat/api/v1/accounts/9999/reply-scope").status_code == 400


def test_paused_filter_includes_restored_relationship_but_not_active_removal(tmp_path):
    http, store, manager, aid, _ = setup_directory(tmp_path)
    conv = manager.ensure_conversation(aid, "restored-friend")
    now = manager.clock()
    with store.transaction() as conn:
        conn.execute("UPDATE conversations SET allowed=1,pause_until=%s WHERE id=%s", (now + 600, conv["id"]))
        conn.execute(
            "INSERT INTO contact_removals(account_id,peer_id,removed_at,restored_at) VALUES(%s,%s,%s,%s)",
            (aid, "restored-friend", now - 10, now),
        )
    path = f"/wechat/api/v1/accounts/{aid}/reply-scope?status=paused"
    result = http.get(path).json()
    assert result["total"] == 1 and result["items"][0]["removed_at"] is None
    with store.transaction() as conn:
        conn.execute(
            "UPDATE contact_removals SET restored_at=NULL WHERE account_id=%s AND peer_id='restored-friend'",
            (aid,),
        )
    assert http.get(path).json()["total"] == 0


@pytest.mark.parametrize("kind", ["private", "group"])
def test_removed_scope_filters_unrestored_relationships_with_search_and_pagination(tmp_path, kind):
    http, store, manager, aid, _ = setup_directory(tmp_path)
    now = manager.clock()
    peers = [f"removed-{i:02}" + ("@chatroom" if kind == "group" else "") for i in range(14)]
    reasons = ["group_left", "group_kicked", "group_dissolved"] if kind == "group" else ["friend_removed"]
    with store.transaction() as conn:
        for index, peer in enumerate(peers):
            manager.ensure_conversation(aid, peer, conn)
            if index == 13:
                continue
            conn.execute(
                "INSERT INTO contact_removals(account_id,peer_id,removed_at,restored_at,reason) "
                "VALUES(%s,%s,%s,%s,%s)",
                (aid, peer, now - 10, now if index == 12 else None, reasons[index % len(reasons)]),
            )
    other = manager.add_account("removed-other")
    manager.verify_identity("removed-other", {"wxid": "removed-other-self"})
    with store.transaction() as conn:
        manager.ensure_conversation(other["id"], peers[0], conn)
        conn.execute(
            "INSERT INTO contact_removals(account_id,peer_id,removed_at,reason) VALUES(%s,%s,%s,%s)",
            (other["id"], peers[0], now, reasons[0]),
        )
    path = f"/wechat/api/v1/accounts/{aid}/reply-scope"
    params = {"kind": kind, "status": "removed"}
    first = http.get(path, params=params)
    assert first.status_code == 200
    first = first.json()
    assert first["total"] == 12 and len(first["items"]) == 10
    second = http.get(path, params={**params, "page": 2}).json()
    assert second["total"] == 12 and len(second["items"]) == 2
    rows = first["items"] + second["items"]
    assert {row["peer_id"] for row in rows} == set(peers[:12])
    assert all(
        row["account_id"] == aid and row["kind"] == kind and row["removed_at"] is not None for row in rows
    )
    assert {row["relationship_reason"] for row in rows} == set(reasons)
    match = http.get(path, params={**params, "q": "removed-03"}).json()
    assert match["total"] == 1 and match["items"][0]["peer_id"] == peers[3]
    assert http.get(path, params={**params, "q": "removed-12"}).json()["total"] == 0
    assert http.get(path, params={"kind": kind}).json()["total"] == 14
    assert http.get(path, params={**params, "page": 99}).json()["page"] == 2
