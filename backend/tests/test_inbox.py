import json
from concurrent.futures import ThreadPoolExecutor

from qingda.service import Manager
from qingda.store import Store
from test_database import database_url


def event(msg="8895067626106766584", **data):
    return {
        "TypeName": "AddMsg",
        "Appid": "device-a",
        "Wxid": "wxid_me",
        "Data": {
            "NewMsgId": int(msg),
            "MsgType": 1,
            "FromUserName": {"string": "wxid_friend"},
            "ToUserName": {"string": "wxid_me"},
            "Content": {"string": "你好"},
            "CreateTime": 1791448800,
            **data,
        },
    }


def test_concurrent_redelivery_is_one_durable_event_with_original_hash_and_single_content(tmp_path):
    store = Store(database_url(tmp_path / "test.db"))
    raw = json.dumps(event(), ensure_ascii=False).encode()
    from qingda.tenancy import user_scope

    def receive(_):
        with user_scope(1):
            return store.receive(raw)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(receive, range(16)))
    rows = store.events()
    assert len(rows) == 1
    assert rows[0]["message_id"] == "8895067626106766584"
    assert rows[0]["deliveries"] == 16
    assert rows[0]["raw_sha256"] == __import__("hashlib").sha256(raw).hexdigest()
    assert store.event(rows[0]["id"])["Data"]["Content"] == "你好"
    assert store.health()["database_ready"] is True


def test_unknown_and_invalid_events_are_preserved_without_reply(tmp_path):
    store = Store(database_url(tmp_path / "test.db"))
    for raw in (b'{"TypeName":"FutureEvent","Data":{}}', b"{invalid"):
        store.receive(raw)
    Manager(store).process_events()
    assert len(store.events()) == 2
    assert store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100") == []
