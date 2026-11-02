"""网页工具的公开网址范围与跨进程请求时钟。"""

import asyncio
import html
import ipaddress
import re
import socket
import time
from urllib.parse import urlsplit, urlunsplit

from psycopg_pool import ConnectionPool

from .database_async import database_call
from .reply_tools import ToolFailure


def public_url(value):
    """规范化用于授权比对；DNS 在实际抓取前另行检查。"""
    try:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 8192
            or re.search(r"[\s\\\x00-\x1f\x7f]", value)
        ):
            raise ValueError()
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".").encode("idna").decode().lower()
        if (
            parts.scheme not in ("http", "https")
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.port == 0
            or host == "localhost"
            or "." not in host
            and ":" not in host
            or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".invalid", ".test"))
        ):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        if address is not None and address.version == 6:
            host = "[" + host + "]"
        authority = host + (":" + str(parts.port) if parts.port is not None else "")
        return urlunsplit((parts.scheme, authority, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError) as error:
        raise ToolFailure(
            "invalid_web_url", "网址须为不含登录凭据的公开HTTP/HTTPS地址，不能访问本机或内网"
        ) from error


async def resolve_public_url(value):
    parts = urlsplit(public_url(value))
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                parts.hostname,
                parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            timeout=3,
        )
    except (OSError, asyncio.TimeoutError) as error:
        raise ToolFailure("web_url_unavailable", "网页域名暂时无法解析") from error
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ToolFailure("invalid_web_url", "网页域名指向非公开地址，不能读取")


def check_fetch_scope(context, trace, value):
    target_url = public_url(value)
    target = (
        next(
            (
                v.get("reply_target", v.get("sender"))
                for v in context
                if isinstance(v, dict) and v.get("current")
            ),
            None,
        )
        if isinstance(context, list)
        else None
    )
    allowed = set()
    for item in context if isinstance(context, list) else []:
        if not (
            isinstance(item, dict)
            and item.get("role") == "user"
            and item.get("current")
            and item.get("sender", target) == target
            and item.get("speaker_type", "member") == "member"
            and item.get("context_scope") != "background"
            and item.get("message_kind") != "pat"
        ):
            continue
        text = html.unescape(item.get("content", ""))
        for match in re.findall(r'https?://[^\s<>"\u201c\u201d，。；！？）】」』]+', text):
            match = match.rstrip(".,;!?，。；！？）】」』")
            while match and match[-1] in ")]}":
                closing = match[-1]
                opening = {")": "(", "]": "[", "}": "{"}[closing]
                if match.count(closing) <= match.count(opening):
                    break
                match = match[:-1]
            try:
                allowed.add(public_url(match))
            except ToolFailure:
                pass
    for item in trace:
        if item.get("name") == "web_search" and item.get("result", {}).get("ok"):
            for row in item["result"].get("data", {}).get("results", []):
                try:
                    allowed.add(public_url(row.get("url")))
                except ToolFailure:
                    pass
    if target_url not in allowed:
        raise ToolFailure(
            "web_url_not_authorized",
            "只能读取本轮提问者提供或本任务成功搜索得到的网址；请先搜索取得来源，不要猜测网址",
        )


class WebRateLimiter:
    """短事务领取下一次启动时间；进程崩溃最多保留250ms水位，无需租约接管。"""

    def __init__(self, database_url):
        self.pool = ConnectionPool(
            database_url,
            min_size=0,
            max_size=2,
            max_waiting=16,
            timeout=3,
            open=True,
            kwargs={"autocommit": True, "connect_timeout": 3},
        )

    def acquire(self, resource):
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SET LOCAL lock_timeout='1s'")
            conn.execute("SET LOCAL statement_timeout='2s'")
            row = conn.execute(
                "SELECT next_allowed_at FROM web_tool_rate_limits WHERE resource=%s FOR UPDATE", (resource,)
            ).fetchone()
            if row is None:
                raise ToolFailure("missing_rate_config", "网页工具请求时钟未初始化，请执行数据库升级")
            delay = conn.execute(
                "SELECT greatest(0,extract(epoch FROM (%s::timestamptz-clock_timestamp())))", (row[0],)
            ).fetchone()[0]
            if delay <= 0:
                conn.execute(
                    "UPDATE web_tool_rate_limits SET next_allowed_at=clock_timestamp()+interval '250 milliseconds' WHERE resource=%s",
                    (resource,),
                )
            return float(delay)

    async def wait(self, resource):
        deadline = time.monotonic() + 5
        while True:
            try:
                delay = await database_call(self.acquire, resource)
            except ToolFailure:
                raise
            except Exception as error:
                raise ToolFailure("web_tool_busy", "网页查询容量暂时不可用，请稍后重试") from error
            if delay <= 0:
                return
            if time.monotonic() + delay >= deadline:
                raise ToolFailure("web_tool_busy", "网页查询繁忙，本次等待已达上限")
            await asyncio.sleep(min(delay, 0.25))

    def close(self):
        self.pool.close()
