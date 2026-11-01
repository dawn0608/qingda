"""用户范围只来自已验证的凭据；ContextVar 随异步任务及数据库线程传递。"""

from contextlib import contextmanager
from contextvars import ContextVar

current_user = ContextVar("wechat_user", default=None)


@contextmanager
def user_scope(user_id):
    token = current_user.set(int(user_id) if user_id is not None else None)
    try:
        yield
    finally:
        current_user.reset(token)


def initialize_user(conn, user_id):
    conn.execute("SELECT set_config('app.user_id',%s,true)", (str(user_id),))
    conn.execute(
        "INSERT INTO dialogue_policy(id,last_old_event,enabled_at) VALUES(1,0,0) ON CONFLICT DO NOTHING"
    )
    conn.execute("INSERT INTO reply_profile_clock(id,next_at) VALUES(1,0) ON CONFLICT DO NOTHING")
