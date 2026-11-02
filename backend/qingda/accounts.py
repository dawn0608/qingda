"""微信账号资料与在线刷新；保留身份核实、内部归档及过时结果保护。"""

import json
import uuid
from urllib.parse import urlparse

from .contacts import enqueue_contacts
from .database_async import database_call
from .providers import ProviderError


def public_profile(data):
    profile = {
        key: data[key][:2000]
        for key in ("wxid", "nickName", "alias", "country", "province", "city", "signature")
        if isinstance(data.get(key), str)
    }
    sex = data.get("sex")
    mobile = data.get("mobile")
    if isinstance(mobile, str) and mobile.strip():
        profile["mobile"] = mobile.strip()[:64]
    if type(sex) is int and sex in (0, 1, 2):
        profile["sex"] = sex
    elif isinstance(sex, str) and sex in ("0", "1", "2"):
        profile["sex"] = int(sex)
    for key in ("smallHeadImgUrl", "bigHeadImgUrl"):
        value = data.get(key)
        if isinstance(value, str):
            parsed = urlparse(value)
            host = parsed.hostname or ""
            if (
                parsed.scheme in ("http", "https")
                and any(host.endswith("." + domain) or host == domain for domain in ("qlogo.cn", "qpic.cn"))
                and not parsed.username
            ):
                profile[key] = parsed._replace(scheme="https").geturl()
    return profile


class AccountService:
    def __init__(self, manager, gateway):
        self.manager, self.store, self.gateway = manager, manager.store, gateway
        self.clock = manager.clock

    def terminal(self, aid, conn=None):
        if conn is None:
            with self.store.connect() as c:
                return self.terminal(aid, c)
        account = self.manager.account(aid, conn)
        row = conn.execute(
            "SELECT * FROM terminals WHERE app_id=%s AND account_id=%s", (account["current_app_id"], aid)
        ).fetchone()
        if not row:
            raise ValueError("账号设备不存在或历史已合并，请从原账号重新扫码")
        return account, dict(row)

    def usable(self, aid):
        account, terminal = self.terminal(aid)
        if account["archived_at"] is not None:
            raise ValueError("账号已移除，请重新扫码登录后恢复")
        if not account["wxid"] or not terminal["verified"]:
            raise ValueError("请先完成扫码登录，无效记录可直接移除")
        return account, terminal

    def current(self, c, aid, app_id, operation_id, epoch):
        account = self.manager.account(aid, c)
        row = c.execute("SELECT operation_id FROM terminals WHERE app_id=%s", (app_id,)).fetchone()
        return bool(
            row
            and row[0] == operation_id
            and account["epoch"] == epoch
            and account["current_app_id"] == app_id
        )

    def snapshot_current(self, c, snapshot):
        account, terminal = snapshot
        return self.current(c, account["id"], terminal["app_id"], terminal["operation_id"], account["epoch"])

    async def read_profile(self, app_id):
        result = await self.gateway.call("/personal/getProfile", {"appId": app_id})
        data = result.get("data")
        if (
            result.get("ret") != 200
            or not isinstance(data, dict)
            or not isinstance(data.get("wxid"), str)
            or not data["wxid"].strip()
        ):
            raise ProviderError("profile_unavailable")
        return public_profile(data)

    async def online(self, aid):
        snapshot = self.usable(aid)
        a, t = snapshot
        try:
            online = await self.gateway.online(t["app_id"])
            profile = await self.read_profile(t["app_id"]) if online else None

            def _database_online_99():
                with self.store.transaction() as c:
                    if not self.snapshot_current(c, snapshot):
                        raise ValueError("账号状态已变化，请刷新后重试")
                    if profile:
                        if a["wxid"] and profile["wxid"] != a["wxid"]:
                            raise ProviderError("login_identity_mismatch")
                        self.manager.verify_identity(t["app_id"], profile, c)
                        c.execute(
                            "UPDATE accounts SET profile_json=%s WHERE id=%s", (json.dumps(profile), aid)
                        )
                    state = (
                        "logged_in"
                        if online
                        else "logged_out"
                        if t["login_state"] in ("logged_out", "logout_unknown", "logging_out")
                        else "offline"
                    )
                    c.execute(
                        "UPDATE accounts SET online=%s,online_checked_at=%s,last_successful_refresh_at=%s WHERE id=%s",
                        (online, self.clock(), self.clock(), aid),
                    )
                    c.execute(
                        "UPDATE terminals SET login_state=%s WHERE app_id=%s",
                        (state, t["app_id"]),
                    )
                    if online:
                        enqueue_contacts(c, aid, self.clock(), force=a["online"] != 1)
                return ("normal", None, locals())

            _database_result = await database_call(_database_online_99)
            return next(
                row for row in await database_call(self.manager.accounts, "active") if row["id"] == aid
            )
        except ProviderError as e:

            def _database_online_129(e):
                with self.store.transaction() as c:
                    if self.snapshot_current(c, snapshot):
                        c.execute(
                            "UPDATE accounts SET online=NULL,online_checked_at=%s WHERE id=%s",
                            (self.clock(), aid),
                        )
                        self.store.audit("online_check_failed", aid, {"reason": e.reason}, c)
                return ("normal", None, locals())

            _database_result = await database_call(_database_online_129, e)
            raise

    def remove(self, aid):
        with self.store.transaction() as c:
            account = self.manager.account(aid, c)
            if account["archived_at"] is None:
                self.manager.stop_account(aid, "account_removed", c)
                c.execute("UPDATE accounts SET archived_at=%s WHERE id=%s", (self.clock(), aid))
                c.execute(
                    "UPDATE terminals SET operation_id=%s,login_state='removed' WHERE account_id=%s",
                    (str(uuid.uuid4()), aid),
                )
                self.store.audit("account_removed", aid, conn=c)
        return {"ok": True}
