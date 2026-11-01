"""服务器 PostgreSQL 隔离 UI 夹具；不连接真实微信或模型。"""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import Request, Response

from wechat_manager.app import create_app
from wechat_manager.config import Config
from wechat_manager.migration import migrate_database
from wechat_manager.providers import Gateway
from wechat_manager.security import PASSWORD_HASHER, create_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=17962)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    create_key(args.directory / "master.key")
    (args.directory / "callback.key").write_text("isolated-assistant-only-" * 3)
    config = Config(args.directory, args.directory / "master.key", args.directory / "callback.key",
                    public_url=f"http://127.0.0.1:{args.port}", cookie_secure=False,
                    auth_phone_number="13900000000", wechat_token="isolated-only")
    if not urlsplit(config.database_url).path.lstrip("/").startswith("wechat_manager_assistant_layout_eval_"):
        raise RuntimeError("UI夹具仅允许专用服务器隔离数据库")
    migrate_database(config.database_url)

    async def reject(request):
        raise AssertionError("隔离环境禁止外部业务调用")

    app = create_app(config, lambda m: Gateway(m, httpx.MockTransport(reject), wechat_token="isolated-only"))
    manager = app.state.manager
    with manager.store.transaction() as conn:
        conn.execute("UPDATE auth_admin SET password_hash=%s WHERE id=1", (PASSWORD_HASHER.hash("isolated-assistant-only-passphrase"),))
    manager.configure_model({}, api_key="isolated-only")
    now = time.time()
    for index, count in ((1, 527), (2, 3)):
        app_id = f"assistant-fixture-{index}"
        account = manager.add_account(app_id, "WeMind" if index == 1 else "第二账号")
        manager.verify_identity(app_id, {"wxid": f"assistant-self-{index}", "nickName": "WeMind" if index == 1 else "第二账号"})
        manager.configure_automation(account["id"], prompt="## 角色\n\n你是 WeMind，一个在微信里与人交流的智能助手。\n\n## 回复原则\n\n- 回答准确、有用，交流自然、有分寸。\n- 结合上下文，以最新补充和更正为准。\n- 直接回答核心内容，默认简洁表达。\n\n## 准确与边界\n\n如实表达已知信息，不编造事实、来源或结果。", ai_enabled=True)
        with manager.store.transaction() as conn:
            conn.execute("UPDATE accounts SET online=1 WHERE id=%s", (account["id"],))
            for i in range(count):
                name = ("林小雨", "陈远", "周宁", "许知远", "顾晨", "唐欣", "苏禾", "沈青", "陆川", "程晓")[i] if i < 10 else f"好友 {i:04}"
                name = f"{i:04} {name}" if index == 1 else f"第二账号好友 {i}"
                conn.execute("INSERT INTO conversations(account_id,peer_id,kind,name,allowed,pause_until,updated_at) VALUES(%s,%s,'private',%s,%s,%s,%s)",
                             (account["id"], f"fixture-friend-{i:04}", name, int(i % 3 != 1), now + 720 if index == 1 and i == 5 else 0, now))
                if i == 521:
                    conn.execute("INSERT INTO chat_profiles(account_id,peer_id,profile_json) VALUES(%s,%s,%s)",
                                 (account["id"], f"fixture-friend-{i:04}", json.dumps({"remark": "跨页备注目标", "nickName": "跨页原始昵称"})))
            conn.execute("INSERT INTO conversations(account_id,peer_id,kind,name,allowed,updated_at) VALUES(%s,'fixture@chatroom','group','验收群聊',1,%s)", (account["id"], now))
            for peer in ("filehelper", "weixin", f"assistant-self-{index}", "gh_excluded"):
                manager.ensure_conversation(account["id"], peer, conn)
            conn.execute("""INSERT INTO contact_monitor(account_id,peer_id,app_id,wxid,epoch,operation_id,state,verified_at,next_at,held)
                SELECT c.account_id,c.peer_id,a.current_app_id,a.wxid,a.epoch,t.operation_id,'verified',%s,%s,0
                FROM conversations c JOIN accounts a ON a.id=c.account_id JOIN terminals t ON t.app_id=a.current_app_id WHERE c.account_id=%s""", (now, now + 1200, account["id"]))

    @app.middleware("http")
    async def deterministic_failure(request: Request, call_next):
        if request.url.path.endswith("/reply-scope") and (args.directory / "fail-scope").exists():
            return Response(status_code=503)
        return await call_next(request)

    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
