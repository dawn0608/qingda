"""服务器隔离回归的独立进程；没有租户/数据库参数时不能运行。"""

import json
import os
import sys

from qingda import automation_defaults, prompt_drafts
from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import user_scope

with user_scope(int(os.environ["DRAFT_ACTOR_USER"])):
    store = Store(os.environ["DRAFT_ACTOR_DATABASE"])
    manager = Manager(store)
    payload = json.loads(os.environ["DRAFT_ACTOR_PAYLOAD"])
    if sys.argv[1] == "crash-initialize":
        with store.transaction() as conn:
            assert automation_defaults.initialize_prompt(manager, conn, payload["aid"])
            print("initialized", flush=True)
            sys.stdin.readline()
            os._exit(23)
    if sys.argv[1] == "crash":
        with store.connect() as conn:
            conn.execute("SELECT id FROM accounts WHERE id=%s FOR UPDATE", (payload["aid"],))
            manager.store.set_setting(
                prompt_drafts.key(payload["aid"], payload.get("owner_wxid")), {"uncommitted": True}, conn
            )
            print("locked", flush=True)
            sys.stdin.readline()
            os._exit(23)
    sys.stdin.readline()
    try:
        if sys.argv[1] == "initialize":
            with store.transaction() as conn:
                changed = automation_defaults.initialize_prompt(manager, conn, payload["aid"])
            print(json.dumps({"status": "initialized", "changed": changed}))
        elif sys.argv[1] == "save":
            value = prompt_drafts.save(manager, **payload)
            print(json.dumps({"status": "saved", "version": value["version"]}))
        else:
            value = prompt_drafts.apply(manager, **payload)
            print(json.dumps({"status": "applied", "changed": value["changed"]}))
    except prompt_drafts.DraftConflict:
        print(json.dumps({"status": "conflict"}))
    finally:
        store.close()
