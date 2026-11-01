from auth_fixtures import login_response
from test_http import client
from wechat_manager.providers import ProviderError


class DirectoryGateway:
    def __init__(self, manager):
        self.manager = manager
        self.calls = []
        self.fail = False

    async def online(self, app_id):
        return True

    async def call(self, path, payload, **kwargs):
        self.calls.append((path, payload))
        if self.fail:
            raise ProviderError("platform_check_rejected")
        if path.endswith("getProfile"):
            return {
                "ret": 200,
                "data": {
                    "wxid": "second-me" if payload["appId"] == "wx_second_terminal" else "me",
                    "nickName": "隔离账号",
                },
            }
        if path.endswith(("getBriefInfo", "getDetailInfo")):
            return {
                "ret": 200,
                "data": [
                    {
                        "userName": peer,
                        "nickName": "张三" if peer == "alice" else peer,
                        "remark": "阿明" if peer == "alice" else "",
                        "remarkQuanPin": "aming",
                        "quanPin": peer,
                        "alias": "alice_alias" if peer == "alice" else "",
                    }
                    for peer in payload["wxids"]
                ],
            }
        if path.endswith("getChatroomInfo"):
            return {
                "ret": 200,
                "data": {
                    "chatroomId": payload["chatroomId"],
                    "nickName": "保存的群聊",
                    "memberList": [{"wxid": "me"}],
                },
            }
        return {
            "ret": 200,
            "data": {
                "friends": ["alice", "alice", "bob", "me", "weixin", "fmessage", "gh_public"],
                "chatrooms": ["saved@chatroom", "saved@chatroom"],
            },
        }

    async def close(self):
        pass


def setup_directory(tmp_path):
    c, store = client(tmp_path, gateway_factory=DirectoryGateway)
    csrf = login_response(c).json()["csrf"]
    manager = c.app.state.manager
    account = manager.add_account("wx_current_terminal")
    manager.verify_identity("wx_current_terminal", {"wxid": "me"})
    return c, store, manager, account["id"], {"X-CSRF-Token": csrf}
