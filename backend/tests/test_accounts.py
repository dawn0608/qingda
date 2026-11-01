import json

from auth_fixtures import login_response
from test_database import database_url
from test_http import client
from wechat_manager.service import Manager
from wechat_manager.store import Store


class AccountGateway:
    def __init__(self, manager):
        self.calls = []
        self.is_online = True

    async def call(self, path, payload):
        self.calls.append((path, payload))
        if path == "/personal/getProfile":
            return {
                "ret": 200,
                "data": {
                    "wxid": "new-wxid",
                    "nickName": "真实昵称",
                    "alias": "my-alias",
                    "mobile": "private-mobile",
                },
            }
        raise AssertionError("账号夹具仅支持资料查询")

    async def online(self, app_id):
        self.calls.append(("/login/checkOnline", {"appId": app_id}))
        return self.is_online

    async def close(self):
        pass


def authenticated(tmp_path, gateway_factory=None):
    c, store = client(tmp_path, gateway_factory)
    csrf = login_response(c).json()["csrf"]
    c.headers["X-CSRF-Token"] = csrf
    return c, store, Manager(store)


def test_restart_preserves_terminal_device_and_unverified_account(tmp_path):
    store = Store(database_url(tmp_path / "postgres"))
    manager = Manager(store)
    aid = manager.add_account("pending")["id"]
    with store.transaction() as conn:
        conn.execute(
            "UPDATE terminals SET config_json=%s,login_state='unknown' WHERE app_id='pending'",
            (json.dumps({"device_type": "ipad"}),),
        )
    reopened = Store(store.url)
    terminal = reopened.query("SELECT * FROM terminals")[0]
    assert json.loads(terminal["config_json"]) == {"device_type": "ipad"}
    assert terminal["login_state"] == "unknown"
    assert reopened.query("SELECT archived_at FROM accounts WHERE id=%s", (aid,))[0]["archived_at"] is None
