"""以 PostgreSQL OID 核对一次性名称切换及逆向恢复，不提供旧名运行模式。"""

import json
import os
from pathlib import Path

from psycopg import sql


def save(path, state):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RenameJournal:
    def __init__(self, conn, path, databases, roles):
        if not conn.autocommit:
            raise ValueError("rename_requires_autocommit")
        self.conn, self.path = conn, Path(path)
        self.databases, self.roles = list(databases), list(roles)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None
        if self.state and (
            self.state["database_pairs"] != [list(p) for p in self.databases]
            or self.state["role_pairs"] != [list(p) for p in self.roles]
        ):
            raise ValueError("rename_manifest_changed")

    def snapshot(self, kind, column, value):
        if kind == "database":
            fields = "oid,datname,datdba,datallowconn,datconnlimit,datacl::text,datistemplate"
            table = "pg_database"
        else:
            fields = (
                "oid,rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,"
                "rolreplication,rolbypassrls,rolconnlimit,rolvaliduntil::text,"
                "md5(coalesce(rolpassword,'')),CASE WHEN left(rolpassword,3)='md5' THEN 'md5' ELSE 'other' END"
            )
            table = "pg_authid"
        return self.conn.execute(
            sql.SQL("SELECT " + fields + " FROM " + table + " WHERE {}=%s").format(sql.Identifier(column)),
            (value,),
        ).fetchone()

    def prepare(self):
        if self.state:
            self.verify()
            return
        self.state = {
            "phase": "prepared",
            "database_pairs": self.databases,
            "role_pairs": self.roles,
            "databases": [],
            "roles": [],
        }
        for kind, pairs, key, column in [
            ("database", self.databases, "databases", "datname"),
            ("role", self.roles, "roles", "rolname"),
        ]:
            for old, new in pairs:
                if self.snapshot(kind, column, new):
                    raise ValueError("target_name_collision")
                row = self.snapshot(kind, column, old)
                if not row:
                    raise ValueError("source_identity_missing")
                if kind == "role" and row[-1] == "md5":
                    raise ValueError("md5_role_rename_forbidden")
                self.state[key].append({"oid": row[0], "old": old, "new": new, "original": list(row)})
        save(self.path, self.state)

    def verify(self):
        for kind, key, column in [("database", "databases", "datname"), ("role", "roles", "rolname")]:
            for item in self.state[key]:
                row = self.snapshot(kind, "oid", item["oid"])
                if not row or row[1] not in (item["old"], item["new"]):
                    raise ValueError("object_identity_changed")
                for name in (item["old"], item["new"]):
                    other = self.snapshot(kind, column, name)
                    if other and other[0] != item["oid"]:
                        raise ValueError("target_name_collision")
                # ACL 文本中的角色名会随 OID 改名变化；权限按 OID 另行核验。
                indices = [2, 4, 6] if kind == "database" else list(range(2, len(row)))
                if any(row[i] != item["original"][i] for i in indices):
                    raise ValueError("object_attributes_changed")

    def disconnect(self):
        self.verify()
        for item in self.state["databases"]:
            current = self.snapshot("database", "oid", item["oid"])[1]
            self.conn.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(current))
            )
            self.conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datid=%s AND pid<>pg_backend_pid()",
                (item["oid"],),
            )
        self.state["connections_blocked"] = True
        save(self.path, self.state)

    def rename(self, kind, items, field):
        for item in items:
            row = self.snapshot(kind, "oid", item["oid"])
            if not row or row[1] not in (item["old"], item["new"]):
                raise ValueError("object_identity_changed")
            if row[1] != item[field]:
                self.conn.execute(
                    sql.SQL("ALTER " + kind.upper() + " {} RENAME TO {}").format(
                        sql.Identifier(row[1]), sql.Identifier(item[field])
                    )
                )
            item["current"] = item[field]
            save(self.path, self.state)

    def forward(self):
        self.prepare()
        if self.state["phase"] == "forward_complete":
            self.verify()
            if any(
                self.snapshot(kind, "oid", item["oid"])[1] != item["new"]
                for kind, key in [("database", "databases"), ("role", "roles")]
                for item in self.state[key]
            ):
                raise ValueError("completed_identity_changed")
            return
        self.disconnect()
        self.rename("database", self.state["databases"], "new")
        self.rename("role", self.state["roles"], "new")
        self.verify()
        self.state["phase"] = "forward_complete"
        save(self.path, self.state)

    def restore_connections(self):
        for item in self.state["databases"]:
            name = self.snapshot("database", "oid", item["oid"])[1]
            self.conn.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS {}").format(
                    sql.Identifier(name), sql.Literal(item["original"][3])
                )
            )
        self.state["connections_blocked"] = False
        save(self.path, self.state)

    def backward(self):
        self.prepare()
        self.disconnect()
        self.rename("role", reversed(self.state["roles"]), "old")
        self.rename("database", reversed(self.state["databases"]), "old")
        self.restore_connections()
        self.verify()
        self.state["phase"] = "reversed"
        save(self.path, self.state)
