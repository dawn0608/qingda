"""隔离浏览器验收：真实 HTTP/SQLite/Gateway，仅替换外部微信平台。"""

import argparse
import json
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import uvicorn
from fastapi import HTTPException, Request
from qingda.accounts import AccountService
from qingda.app import create_app
from qingda.auth import COOKIE
from qingda.config import Config
from qingda.providers import Gateway
from qingda.security import PASSWORD_HASHER, create_key, digest
from qingda.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=17953)
    port = parser.parse_args().port
    with tempfile.TemporaryDirectory(prefix="wechat-accounts-browser-") as directory:
        root = Path(directory)
        create_key(root / "key")
        (root / "callback.key").write_text("isolated-browser-callback-" * 3)
        config = Config(
            root,
            root / "key",
            root / "callback.key",
            public_url=f"http://127.0.0.1:{port}",
            cookie_secure=False,
            auth_phone_number="13900000000",
            wechat_token="isolated-token",
        )
        store = Store(config.db_path)
        with store.transaction() as conn:
            conn.execute(
                "UPDATE auth_admin SET password_hash=? WHERE id=1",
                (PASSWORD_HASHER.hash("isolated-browser-only-passphrase"),),
            )
        online = {"ui-main": True, "ui-unknown": True}
        platform_paths = []

        async def transport(request):
            path = request.url.path
            payload = json.loads(request.content)
            app_id = payload.get("appId", "")
            platform_paths.append(path.removeprefix("/finder/v2/api"))
            if path.endswith("checkOnline"):
                return httpx.Response(200, json={"ret": 200, "data": online.get(app_id, False)})
            if path.endswith("getProfile"):
                return httpx.Response(
                    200,
                    json={
                        "ret": 200,
                        "data": {
                            "wxid": "wxid_" + app_id,
                            "nickName": {"ui-main": "本地验收账号", "ui-unknown": "第二个验收账号"}.get(
                                app_id, "隔离验收账号"
                            ),
                            "alias": "isolated_alias",
                            "country": "CN",
                            "city": "重庆",
                            "signature": "仅供隔离验收",
                            "sex": 1,
                        },
                    },
                )
            raise AssertionError("隔离验收禁止未声明的外部调用")

        app = create_app(
            config, lambda m: Gateway(m, httpx.MockTransport(transport), wechat_token=config.wechat_token)
        )
        m = app.state.manager
        for app_id, nickname in (
            ("ui-main", "本地验收账号"),
            ("ui-unknown", "第二个验收账号"),
            ("ui-archived", "归档历史账号"),
        ):
            account = m.add_account(app_id)
            m.verify_identity(app_id, {"wxid": "wxid_" + app_id, "nickName": nickname})
            conv = m.ensure_conversation(account["id"], "isolated-friend")
            if app_id == "ui-main":
                with store.connect() as conn:
                    conn.execute(
                        "UPDATE terminals SET config_json=? WHERE app_id=?",
                        (
                            json.dumps(
                                {
                                    "device_type": "ipad",
                                    "region_id": "320000",
                                    "proxy_cipher": m.vault.encrypt(
                                        "socks5://isolated:fixture@proxy.test:1080"
                                    ),
                                }
                            ),
                            app_id,
                        ),
                    )
            if app_id == "ui-archived":
                with store.transaction() as conn:
                    m.create_task(
                        conn,
                        "auto",
                        m.account(account["id"], conn),
                        conv,
                        content="这条历史发送记录必须保留",
                        request_id=str(uuid.uuid4()),
                    )
                AccountService(m, app.state.gateway).remove(account["id"])

        @app.get("/wechat/__acceptance/control")
        async def configure(request: Request):
            token = request.cookies.get(COOKIE, "")
            if not store.query(
                "SELECT 1 FROM sessions WHERE id_hash=? AND expires_at>?", (digest(token), time.time())
            ):
                raise HTTPException(401)
            return {
                "platform_paths": platform_paths,
                "historical_sends": store.query(
                    "SELECT count(*) AS n FROM tasks WHERE content='这条历史发送记录必须保留'"
                )[0]["n"],
            }

        # 仅验收夹具：放在 SPA 通配路由之前，并沿用 /wechat/ 会话 Cookie。
        app.router.routes.insert(0, app.router.routes.pop())
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
