"""只读聊天弹框：真实入库链路、全类型投影、分页及用户隔离。仅服务器隔离库。"""

import json

import pytest
from contact_fixtures import setup_directory
from qingda.media_references import record_outbound
from qingda.tenancy import user_scope


def incoming(store, manager, platform, msg_type="1", content="正文", peer="alice", sender=None, now=None):
    data = {
        "NewMsgId": platform,
        "FromUserName": sender or peer,
        "ToUserName": "me",
        "MsgType": int(msg_type),
        "Content": content,
        "CreateTime": now or manager.clock(),
    }
    store.receive(
        json.dumps(
            {"Appid": "wx_current_terminal", "Wxid": "me", "TypeName": "AddMsg", "Data": data}
        ).encode()
    )
    manager.process_events()
    return store.query("SELECT * FROM messages WHERE platform_id=%s", (str(platform),))[0]


def endpoint(aid, cid):
    return f"/wechat/api/v1/accounts/{aid}/conversations/{cid}/history"


def test_all_chat_types_full_body_transcript_and_revoke_without_side_effects(tmp_path):
    http, store, manager, aid, _ = setup_directory(tmp_path)
    cases = [
        ("1", "  原文100%_!\n" + "长正文" * 3000 + "  ", "文本"),
        ("3", '<msg><img aeskey="secret-download-key"/></msg>', "图片"),
        ("34", '<msg><voicemsg voicelength="2300"/></msg>', "语音"),
        ("43", '<msg><videomsg playlength="7"/></msg>', "视频"),
        ("62", '<msg><videomsg playlength="2"/></msg>', "短视频"),
        ("47", '<msg><emoji cdnurl="https://example.invalid/private"/></msg>', "表情"),
        ("42", '<msg nickname="联系人名片"/>', "名片"),
        ("48", '<msg><location label="详细地址" poiname="位置名称"/></msg>', "位置"),
        ("49", "<msg><appmsg><type>6</type><title>完整文件名.xlsx</title></appmsg></msg>", "文件"),
        (
            "49",
            "<msg><appmsg><type>5</type><title>"
            + "完整链接标题" * 200
            + "</title><des>链接描述</des></appmsg></msg>",
            "链接",
        ),
        ("49", "<msg><appmsg><type>33</type><title>小程序标题</title></appmsg></msg>", "小程序"),
        ("49", "<msg><appmsg><type>2000</type><title>转账备注</title></appmsg></msg>", "转账"),
        ("49", "<msg><appmsg><type>2001</type><title>红包祝福</title></appmsg></msg>", "红包"),
        (
            "49",
            "<msg><appmsg><type>57</type><title>引用后的正文</title><refermsg><svrid>2000</svrid></refermsg></appmsg></msg>",
            "引用消息",
        ),
        ("999", "<future-message>未来消息的可见正文</future-message>", "其他消息"),
        ("998", "未知类型纯文字", "其他消息"),
        (
            "997",
            '<!DOCTYPE msg [<!ENTITY x SYSTEM "file:///etc/passwd">]><msg><text>&x;</text></msg>',
            "其他消息",
        ),
        ("37", '<msg opcode="2" fromusername="alice" content="可见的打招呼正文"/>', "打招呼"),
    ]
    messages = [incoming(store, manager, 2000 + i, kind, body) for i, (kind, body, _) in enumerate(cases)]
    cid = messages[0]["conversation_id"]
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO incoming_media(message_id,state,kind,transcript,updated_at) VALUES(%s,'ready','voice','已经保存的语音转写',%s)",
            (messages[2]["id"], manager.clock()),
        )
    for i, kind in enumerate(("37", "51", "10000", "10002")):
        incoming(store, manager, 4000 + i, kind, "不应该显示的系统消息")
    incoming(
        store,
        manager,
        5000,
        "10002",
        '<sysmsg type="revokemsg"><revokemsg><session>alice</session><newmsgid>2000</newmsgid></revokemsg></sysmsg>',
    )
    path = endpoint(aid, cid)
    before = {
        name: store.query(f"SELECT count(*) AS n FROM {name}")[0]["n"]
        for name in ("message_records", "events", "tasks", "incoming_media", "contact_entries")
    }
    calls = list(http.app.state.gateway.calls)
    result = http.get(path).json()
    assert len(result["items"]) == len(cases) and not result["has_more"]
    items = {value["id"]: value for value in result["items"]}
    for row, (_, _, label) in zip(messages, cases):
        assert items[str(row["id"])]["type_label"] == label
    assert items[str(messages[0]["id"])]["content"] == cases[0][1]
    assert items[str(messages[0]["id"])]["revoked"] is True
    assert "已经保存的语音转写" in items[str(messages[2]["id"])]["content"]
    assert "3秒" in items[str(messages[2]["id"])]["content"]
    assert items[str(messages[9]["id"])]["content"].startswith("完整链接标题" * 200)
    assert items[str(messages[14]["id"])]["content"] == "未来消息的可见正文"
    assert items[str(messages[15]["id"])]["content"] == "未知类型纯文字"
    assert not items[str(messages[16]["id"])]["content"]
    assert all(
        "secret-download-key" not in item["content"] and "<msg>" not in item["content"]
        for item in items.values()
    )
    for query in ("100%_!", "已经保存的语音转写", "完整文件名.xlsx", "链接描述", "转账备注", "未来消息"):
        response = http.get(path, params={"q": query}).json()
        assert response["items"] and all(
            query.casefold() in (item["content"] + item["type_label"]).casefold()
            for item in response["items"]
        )
    assert http.get(path, params={"q": "secret-download-key"}).json()["items"] == []
    assert http.app.state.gateway.calls == calls
    assert before == {name: store.query(f"SELECT count(*) AS n FROM {name}")[0]["n"] for name in before}


def test_pagination_search_scan_continuation_and_late_arrival(tmp_path):
    http, store, manager, aid, _ = setup_directory(tmp_path)
    conv = manager.ensure_conversation(aid, "alice")
    cid = conv["id"]
    with store.transaction() as conn:
        conn.execute(
            """WITH contents AS (
            INSERT INTO message_contents(text) SELECT CASE WHEN n=1 THEN '最早唯一命中' ELSE '普通正文'||n END
            FROM generate_series(1,1107) n RETURNING id,text
        ) INSERT INTO message_records(id,account_id,conversation_id,direction,source,msg_type,sender,created_at,received_at,content_id)
        SELECT 10000+id,%s,%s,'inbound','live','1','alice',1000,1000,id FROM contents""",
            (aid, cid),
        )
    path = endpoint(aid, cid)
    first = http.get(path).json()
    assert len(first["items"]) == 50 and first["has_more"]
    all_ids, page = set(), first
    while True:
        ids = {value["id"] for value in page["items"]}
        assert not ids & all_ids
        all_ids |= ids
        if not page["has_more"]:
            break
        page = http.get(path, params={"cursor": page["cursor"]}).json()
    assert len(all_ids) == 1107
    search = http.get(path, params={"q": "唯一命中"}).json()
    assert not search["items"] and search["has_more"] and search["cursor"]
    final = http.get(path, params={"q": "唯一命中", "cursor": search["cursor"]}).json()
    assert len(final["items"]) == 1 and not final["has_more"]
    with store.transaction() as conn:
        content = conn.execute(
            "INSERT INTO message_contents(text) VALUES('时间较早但刚收到') RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO message_records(id,account_id,conversation_id,direction,source,msg_type,sender,created_at,received_at,content_id) VALUES(90000,%s,%s,'inbound','live','1','alice',10,2000,%s)",
            (aid, cid, content),
        )
    new = http.get(path, params={"after_id": first["watermark"]}).json()
    assert [value["content"] for value in new["items"]] == ["时间较早但刚收到"]
    assert new["watermark"] == "90000"
    assert http.get(path, params={"q": "不相同", "cursor": first["cursor"]}).status_code == 400


def test_group_owner_removed_contact_and_outbound_dedup(tmp_path):
    http, store, manager, aid, _ = setup_directory(tmp_path)
    message = incoming(store, manager, 7001, peer="team@chatroom", content="member-id:\n群内未唤醒正文")
    cid = message["conversation_id"]
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO chat_member_profiles(account_id,peer_id,member_id,profile_json,updated_at) VALUES(%s,'team@chatroom','member-id','{\"nickName\":\"群成员昵称\"}',0)",
            (aid,),
        )
        conn.execute(
            "INSERT INTO contact_removals(account_id,peer_id,removed_at,reason) VALUES(%s,'team@chatroom',1,'group_left')",
            (aid,),
        )
        task = manager.create_task(
            conn,
            "auto",
            manager.account(aid, conn),
            manager.conversation(cid, conn),
            state="accepted",
            content="助手已发送正文",
        )
        record_outbound(store, conn, task, {"data": {"newMsgId": "7002"}}, "accepted", manager.clock())
    store.receive(
        json.dumps(
            {
                "TypeName": "AddMsg",
                "Appid": "wx_current_terminal",
                "Wxid": "me",
                "Data": {
                    "NewMsgId": 7002,
                    "FromUserName": "team@chatroom",
                    "ToUserName": "me",
                    "MsgType": 1,
                    "Content": "me:\n助手已发送正文",
                    "CreateTime": manager.clock(),
                },
            }
        ).encode()
    )
    manager.process_events()
    with store.transaction() as conn:
        uncertain = manager.create_task(
            conn,
            "auto",
            manager.account(aid, conn),
            manager.conversation(cid, conn),
            state="unknown",
            content="不能冒充已发送的内容",
        )
        record_outbound(store, conn, uncertain, {}, "unknown", manager.clock())
    items = http.get(endpoint(aid, cid)).json()["items"]
    assert len(items) == 2
    assert items[0]["sender_name"] == "群成员昵称" and items[0]["content"] == "群内未唤醒正文"
    assert items[1]["direction"] == "outbound" and items[1]["content"] == "助手已发送正文"
    other = manager.add_account("other-history-account")
    manager.verify_identity("other-history-account", {"wxid": "other-self"})
    assert http.get(endpoint(other["id"], cid)).status_code == 400


def test_auth_user_isolation_and_empty_history(tmp_path):
    http, store, manager, aid, _ = setup_directory(tmp_path)
    conv = manager.ensure_conversation(aid, "alice")
    path = endpoint(aid, conv["id"])
    assert http.get(path).json()["items"] == []
    for peer in ("filehelper", "weixin", "gh_public", "me"):
        hidden = manager.ensure_conversation(aid, peer)
        assert http.get(endpoint(aid, hidden["id"])).status_code == 400
    from test_authentication import code_login

    http.cookies.clear()
    response = code_login(http, target="13900000001")
    assert response.status_code == 200
    uid = response.json()["user_id"]
    with user_scope(uid):
        assert store.query("SELECT id FROM accounts WHERE id=%s", (aid,)) == []
        from qingda.chat_records import history

        with pytest.raises(ValueError, match="会话不存在"):
            history(manager, aid, conv["id"])
    assert http.get(path).status_code == 400
    http.cookies.clear()
    assert http.get(path).status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"after_id": -1},
        {"q": "x" * 2001},
        {"cursor": "invalid"},
        {"after_id": 999},
        {"cursor": "invalid", "after_id": 0},
    ],
)
def test_invalid_history_parameters(tmp_path, params):
    http, _, manager, aid, _ = setup_directory(tmp_path)
    conv = manager.ensure_conversation(aid, "alice")
    assert http.get(endpoint(aid, conv["id"]), params=params).status_code in (400, 422)


def test_cached_image_read_only_integrity_and_visibility(tmp_path):
    import hashlib
    import io

    from PIL import Image
    from test_authentication import code_login

    http, store, manager, aid, _ = setup_directory(tmp_path)
    row = incoming(store, manager, 9901, "3", "<msg><img/></msg>")
    mid, cid = row["id"], row["conversation_id"]
    path = endpoint(aid, cid)
    assert http.get(path).json()["items"][0]["image"] is None
    assert http.get(f"{path}/{mid}/image").status_code == 404
    output = io.BytesIO()
    Image.new("RGB", (32, 16), "green").save(output, format="PNG")
    body = output.getvalue()
    frame = dict(message_id=mid, sha256=hashlib.sha256(body).hexdigest(), width=32, height=16)
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO incoming_media(message_id,state,kind,frames_json,trace_json,updated_at) VALUES(%s,'ready','image',%s,%s,0)",
            (mid, json.dumps([frame]), json.dumps(dict(frame_count=1, animated=False))),
        )
    target = manager.media.path(frame)
    target.write_bytes(body)
    image = http.get(path).json()["items"][0]["image"]
    assert image == dict(url=f"{path}/{mid}/image", width=32, height=16)
    before = {
        table: store.query(f"SELECT * FROM {table}")
        for table in ("tasks", "incoming_media", "media_resources", "message_records")
    }
    calls = list(http.app.state.gateway.calls)
    response = http.get(image["url"])
    assert response.status_code == 200 and response.content == body
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    other = manager.ensure_conversation(aid, "bob")
    assert http.get(f"{endpoint(aid, other['id'])}/{mid}/image").status_code == 404
    account = manager.add_account("another-image-account")
    assert http.get(f"{endpoint(account['id'], cid)}/{mid}/image").status_code == 404
    target.write_bytes(b"corrupted")
    assert http.get(image["url"]).status_code == 404
    target.unlink()
    assert http.get(image["url"]).status_code == 404
    target.symlink_to(tmp_path / "outside.png")
    (tmp_path / "outside.png").write_bytes(body)
    assert http.get(image["url"]).status_code == 404
    target.unlink()
    target.write_bytes(body)
    assert http.app.state.gateway.calls == calls
    assert before == {table: store.query(f"SELECT * FROM {table}") for table in before}
    http.cookies.clear()
    assert http.get(image["url"]).status_code == 401
    assert code_login(http, target="13900000001").status_code == 200
    assert http.get(image["url"]).status_code == 404


@pytest.mark.parametrize(
    "change", ["sticker", "video", "animated", "failed", "foreign-frame", "invalid-size", "unverified"]
)
def test_image_does_not_expose_disallowed_or_unverified_cache(tmp_path, change):
    import hashlib
    import io

    from PIL import Image

    http, store, manager, aid, _ = setup_directory(tmp_path)
    row = incoming(
        store, manager, 9902, "47" if change == "sticker" else "43" if change == "video" else "3", "<msg/>"
    )
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    body = output.getvalue()
    frame = dict(message_id=row["id"], sha256=hashlib.sha256(body).hexdigest(), width=2, height=2)
    if change == "foreign-frame":
        frame["message_id"] += 1
    if change == "invalid-size":
        frame["width"] = 0
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO incoming_media(message_id,state,kind,frames_json,trace_json,updated_at) VALUES(%s,%s,'image',%s,%s,0)",
            (
                row["id"],
                "failed" if change == "failed" else "ready",
                json.dumps([frame]),
                json.dumps(
                    {} if change == "unverified" else dict(frame_count=2 if change == "animated" else 1)
                ),
            ),
        )
    manager.media.path(frame).write_bytes(body)
    path = endpoint(aid, row["conversation_id"])
    assert http.get(path).json()["items"][0]["image"] is None
    assert http.get(f"{path}/{row['id']}/image").status_code == 404
