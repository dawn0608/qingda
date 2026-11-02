import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn

from .config import Config
from .migration import backup_database, migrate_database
from .providers import Gateway
from .security import Vault, create_key
from .service import Manager
from .store import Store
from .worker import Worker


def main():
    parser = argparse.ArgumentParser(description="青答 Qingda 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=17950)
    sub.add_parser("worker")
    sub.add_parser("migrate-db")
    for name in ("backup",):
        command = sub.add_parser(name)
        command.add_argument("path")
    args = parser.parse_args()
    os.umask(0o077)
    config = Config.from_env()
    if args.command == "migrate-db":
        if not config.key_file.exists():
            create_key(config.key_file)
        migrate_database(config.database_url)
        return
    if args.command == "serve":
        from .app import create_app

        uvicorn.run(
            create_app(config),
            host=config.web_bind_host,
            port=args.port,
            access_log=False,
            server_header=False,
            forwarded_allow_ips=config.web_trusted_proxy_ips,
        )
        return
    store = Store(config.database_url)
    if args.command == "worker":
        if Path(os.getenv("OUTBOUND_PAUSE_FILE", "/etc/qingda/outbound-paused")).exists():
            store.close()
            return
        manager = Manager(store, Vault(config.key_file))
        from .media import IncomingMedia

        manager.media = IncomingMedia(manager, config)
        asyncio.run(
            Worker(
                manager,
                Gateway(manager, tool_config=config),
                config.prompt_file,
            ).run()
        )
    else:
        functions = {"backup": backup_database}
        print(json.dumps(functions[args.command](store, args.path), ensure_ascii=False))


if __name__ == "__main__":
    main()
