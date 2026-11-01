"""服务器隔离 PostgreSQL；业务用户由夹具显式建立，不依赖历史迁移。"""

import os
import subprocess
import uuid
from urllib.parse import quote, urlsplit

import psycopg
import pytest
import test_database


@pytest.fixture(autouse=True)
def postgres_resources(monkeypatch, tmp_path):
    base = os.getenv("TEST_DATABASE_URL", "")
    if os.name == "nt" or "test" not in urlsplit(base).path:
        raise RuntimeError("仅允许服务器隔离 PostgreSQL 测试库")
    from wechat_manager.config import Config
    from wechat_manager.migration import migrate_database
    from wechat_manager.security import Vault, create_key, digest
    from wechat_manager.store import Store
    from wechat_manager.tenancy import initialize_user, user_scope

    schemas, stores = {}, []
    original_store, original_config = Store.__init__, Config.__post_init__
    fixture_key = tmp_path / "fixture-master.key"
    create_key(fixture_key)

    def url_for(key, config=None):
        key = str(key)
        if key not in schemas:
            schema = "test_" + uuid.uuid4().hex
            with psycopg.connect(base, autocommit=True) as conn:
                conn.execute('CREATE SCHEMA "' + schema + '"')
            url = base + ("&" if "?" in base else "?") + "options=" + quote("-c search_path=" + schema)
            migrate_database(url)
            vault = Vault(config.key_file if config and config.key_file.exists() else fixture_key)
            callback = "a" * 48
            origin = config.public_url if config else "http://testserver"
            with psycopg.connect(url) as conn:
                uid = conn.execute(
                    "INSERT INTO users(channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url,binding_state) VALUES('phone','13900000000','测试用户',0,%s,%s,%s,'configured') RETURNING id",
                    (digest(callback), vault.encrypt(callback), origin + "/wechatapi/callback/" + callback),
                ).fetchone()[0]
                initialize_user(conn, uid)
            schemas[key] = schema, url
        return schemas[key][1]

    def init(store, url):
        original_store(store, url)
        stores.append(store)

    def configure(config):
        config.database_url = url_for(config.data_dir / "manager", config)
        original_config(config)

    monkeypatch.setattr(test_database, "resolve", url_for)
    monkeypatch.setattr(Store, "__init__", init)
    monkeypatch.setattr(Config, "__post_init__", configure)
    monkeypatch.setenv("DATABASE_URL", base)
    monkeypatch.setenv("PUBLIC_URL", "http://testserver")
    with user_scope(1):
        yield
    for store in stores:
        store.close()
    with psycopg.connect(base, autocommit=True) as conn:
        for schema, _ in schemas.values():
            conn.execute('DROP SCHEMA "' + schema + '" CASCADE')


@pytest.fixture
def isolated_role(postgres_resources):
    """以服务器 peer 运维身份创建短期 RLS 测试角色，不扩大业务角色权限。"""
    base = urlsplit(os.environ["TEST_DATABASE_URL"])
    role = "wechat_manager_rls_test_" + uuid.uuid4().hex

    def execute(statement):
        subprocess.run(
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "psql",
                "-d",
                base.path.lstrip("/"),
                "-v",
                "ON_ERROR_STOP=1",
                "-q",
            ],
            input=statement,
            text=True,
            capture_output=True,
            check=True,
        )

    execute('CREATE ROLE "' + role + '" NOLOGIN; GRANT "' + role + '" TO "' + base.username + '";')
    try:
        yield role
    finally:
        execute('DROP OWNED BY "' + role + '"; DROP ROLE "' + role + '";')
