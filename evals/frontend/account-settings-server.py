"""账户设置服务器隔离夹具；无真实短信、邮件、微信或模型调用。"""

import argparse
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from qingda.app import create_app
from qingda.config import Config
from qingda.migration import migrate_database
from qingda.providers import Gateway
from qingda.security import PASSWORD_HASHER, create_key


class Sms:
    async def send(self, phone, cid):
        pass

    async def verify(self, phone, cid, code):
        return code == "123456"


class Email:
    async def send(self, target, code):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=17964)
    args = parser.parse_args()
    database = os.environ["DATABASE_URL"]
    if not urlsplit(database).path.startswith("/qingda_account_settings_eval_"):
        raise RuntimeError("仅允许账户设置隔离数据库")
    args.directory.mkdir(parents=True, exist_ok=True)
    if not (args.directory / "master.key").exists():
        create_key(args.directory / "master.key")
    (args.directory / "callback.key").write_text("isolated-account-settings-" * 3)
    config = Config(
        args.directory,
        args.directory / "master.key",
        args.directory / "callback.key",
        database_url=database,
        public_url=f"http://127.0.0.1:{args.port}",
        cookie_secure=False,
        auth_phone_number="13900000000",
        aliyun_access_key_id="isolated",
        aliyun_access_key_secret="isolated",
        aliyun_sms_sign_name="isolated",
        aliyun_sms_template_code="isolated",
        smtp_host="isolated.invalid",
        smtp_from="fixture@example.com",
    )
    migrate_database(database, config)

    async def reject(request):
        raise AssertionError("隔离环境禁止业务外部调用")

    app = create_app(config, lambda m: Gateway(m, httpx.MockTransport(reject)), sms_provider=Sms())
    app.state.authentication.email = Email()
    with app.state.manager.store.transaction() as c:
        c.execute(
            "UPDATE users SET username='Dawn',password_hash=%s WHERE id=1",
            (PASSWORD_HASHER.hash("isolated-account-settings-passphrase"),),
        )
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
