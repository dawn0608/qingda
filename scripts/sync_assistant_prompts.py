"""显式预览和同步全部未归档账号；原设定与草稿保存在受保护的快照文件。"""

import argparse
import json
from pathlib import Path

import psycopg
from qingda.config import Config
from qingda.prompt_sync import apply, digest, preview
from qingda.service import Manager
from qingda.store import Store
from qingda.tenancy import user_scope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preview", "apply"])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--sha256", help="apply 必须传入 preview 输出的快照哈希")
    args = parser.parse_args()
    config = Config.from_env()
    store = Store(config.database_url)
    manager = Manager(store)
    try:
        if args.mode == "preview":
            with psycopg.connect(config.database_url, options="-c default_transaction_read_only=on") as conn:
                users = [row[0] for row in conn.execute("SELECT id FROM users ORDER BY id")]
            snapshots = []
            for uid in users:
                with user_scope(uid):
                    snapshots.append(preview(manager))
            snapshot = {"version": 1, "users": snapshots}
            args.snapshot.parent.mkdir(parents=True, exist_ok=True)
            with args.snapshot.open("x", encoding="utf-8") as file:
                file.write(json.dumps(snapshot, ensure_ascii=False, indent=2))
            args.snapshot.chmod(0o600)
            print(
                json.dumps(
                    {
                        "snapshot": str(args.snapshot),
                        "sha256": digest(snapshot),
                        "accounts": sum(len(v["accounts"]) for v in snapshots),
                    }
                )
            )
        else:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
            if not args.sha256 or digest(snapshot) != args.sha256:
                raise ValueError("必须使用预览时的快照哈希，文件不可被修改")
            results = []
            for value in snapshot["users"]:
                with user_scope(value["user_id"]):
                    results.append(apply(manager, value, digest(value)))
            print(json.dumps({"users": results}, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
