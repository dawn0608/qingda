import json

import pytest
from contact_fixtures import setup_directory
from qingda.worker import Worker


def delete_event(store, manager, peer, **changes):
    event = {
        "TypeName": "DelContacts",
        "Appid": "wx_current_terminal",
        "Wxid": "me",
        "Data": {"UserName": {"string": peer}, "DeleteContactScen": 0},
    }
    origin = changes.pop("origin", "live")
    event.update(changes)
    store.receive(json.dumps(event).encode(), origin=origin, now=manager.clock())
    manager.process_events()


@pytest.mark.parametrize(
    "changes",
    [
        {"Wxid": "other"},
        {"Wxid": None},
        {"Appid": "another-terminal"},
        {"origin": "migration"},
        {"Data": {}},
        {"Data": {"UserName": {"string": " alice"}}},
    ],
)
def test_untrusted_or_historical_deletion_never_hides_chat(tmp_path, changes):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    conv = manager.ensure_conversation(aid, "alice")
    delete_event(store, manager, "alice", **changes)
    assert conv["id"] in [v["id"] for v in c.get(f"/wechat/api/v1/conversations?account_id={aid}").json()]
    assert c.get("/wechat/api/v1/conversations").json()[0]["relationship_status"] == "active"


def test_removed_contact_cancels_unsent_tasks_but_keeps_uncertain_and_accepted(tmp_path):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    conv = manager.ensure_conversation(aid, "alice")
    for kind, state in [
        ("manual", "queued"),
        ("auto", "generating"),
        ("manual", "ready"),
        ("manual", "sending"),
        ("manual", "unknown"),
        ("auto", "accepted"),
    ]:
        with store.connect() as conn:
            manager.create_task(conn, kind, manager.account(aid), conv, state=state, content=state)
    delete_event(store, manager, "alice")
    tasks = {
        t["content"]: t
        for t in store.query("SELECT * FROM tasks ORDER BY created_at DESC,ordinal DESC LIMIT 100")
    }
    for state in ("queued", "generating", "ready"):
        assert tasks[state]["state"] == "canceled" and tasks[state]["reason"] == "contact_removed"
    for state in ("sending", "unknown", "accepted"):
        assert tasks[state]["state"] == state
    with store.connect() as conn:
        assert Worker(manager, c.app.state.gateway).check(tasks["sending"], conn) == "contact_removed"


@pytest.mark.parametrize("later_change", [False, True])
def test_restart_processes_live_deletions_without_assuming_later_profile_restores_relation(
    tmp_path, later_change
):
    c, store, manager, aid, _ = setup_directory(tmp_path)
    conv = manager.ensure_conversation(aid, "alice")
    raw = {
        "TypeName": "DelContacts",
        "Appid": "wx_current_terminal",
        "Wxid": "me",
        "Data": {"UserName": {"string": "alice"}},
    }
    store.receive(json.dumps(raw).encode(), now=manager.clock())
    if later_change:
        store.receive(json.dumps({**raw, "TypeName": "ModContacts"}).encode("utf-8-sig"), now=manager.clock())
    original = store.query("SELECT payload,raw_sha256,received_at FROM events")
    upgraded = type(store)(store.url)
    restored = type(manager)(upgraded)
    restored.process_events()
    assert restored.conversation(conv["id"])["removed_at"] is not None
    assert upgraded.query("SELECT payload,raw_sha256,received_at FROM events") == original
    assert upgraded.health()["database_ready"]
    type(store)(store.url)
    assert upgraded.query("SELECT processed FROM events") == [{"processed": 1}] * (2 if later_change else 1)
