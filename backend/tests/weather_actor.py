"""服务器隔离天气配置的独立进程，不调用真实供应商。"""

import json
import os
import sys

from wechat_manager.security import Vault
from wechat_manager.service import Manager
from wechat_manager.store import Store
from wechat_manager.tenancy import user_scope
from wechat_manager.weather_settings import credentials, save

if __name__ == "__main__":
    store = Store(os.environ["WEATHER_ACTOR_DATABASE"])
    manager = Manager(store, Vault(os.environ["WEATHER_ACTOR_KEY_FILE"]))
    uid = int(sys.argv[1])
    try:
        with user_scope(uid):
            print("ready", flush=True)
            sys.stdin.readline()
            for index in range(35):
                save(manager, f"u{uid}-{index}.qweatherapi.com", f"key-user-{uid}-{index}")
                host, key = credentials(manager)
                assert (host, key) == (f"u{uid}-{index}.qweatherapi.com", f"key-user-{uid}-{index}")
            print(json.dumps({"user_id": uid, "writes": 35, "isolated": True}), flush=True)
    finally:
        store.close()
