import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from auth_fixtures import login
from test_authentication import setup as auth_setup
from test_automation import NOW, setup
from wechat_manager import prompt_drafts as drafts
from wechat_manager.tenancy import user_scope


def pending(store, manager, account, conversation):
    with store.transaction() as conn:
        return manager.create_task(
            conn,
            "auto",
            manager.account(account["id"], conn),
            conversation,
            content="隔离回复",
            state="queued",
        )


def test_draft_persistence_does_not_change_active_prompt_or_tasks(tmp_path):
    store, manager, account, conversation = setup(tmp_path)
    task = pending(store, manager, account, conversation)
    before = manager.account(account["id"])
    draft = drafts.read(manager, account["id"])
    saved = drafts.save(manager, account["id"], "新的草稿\n- 条目", draft["version"], draft["base_sha256"])
    after = manager.account(account["id"])
    assert saved["dirty"] and saved["version"]
    assert (after["prompt"], after["epoch"], after["ai_since"]) == (
        before["prompt"],
        before["epoch"],
        before["ai_since"],
    )
    assert store.query("SELECT state FROM tasks WHERE id=%s", (task["id"],))[0]["state"] == "queued"
    from wechat_manager.service import Manager
    from wechat_manager.store import Store

    restarted = Store(store.url)
    try:
        assert drafts.read(Manager(restarted), account["id"])["markdown"] == saved["markdown"]
    finally:
        restarted.close()


def test_apply_is_atomic_idempotent_and_changes_only_current_account(tmp_path):
    store, manager, account, conversation = setup(tmp_path)
    task = pending(store, manager, account, conversation)
    before = manager.account(account["id"])
    draft = drafts.read(manager, account["id"])
    saved = drafts.save(manager, account["id"], "最终设定", None, draft["base_sha256"])
    first = drafts.apply(manager, account["id"], saved["version"], saved["base_sha256"])
    second = drafts.apply(manager, account["id"], saved["version"], saved["base_sha256"])
    assert first["changed"] and not second["changed"]
    assert second["account"]["prompt"] == "最终设定"
    assert second["account"]["epoch"] == before["epoch"] + 1
    assert store.query("SELECT state FROM tasks WHERE id=%s", (task["id"],))[0]["state"] == "canceled"
    assert not second["draft"]["dirty"]


def test_stale_save_apply_and_external_active_change_require_explicit_resolution(tmp_path):
    store, manager, account, conversation = setup(tmp_path)
    initial = drafts.read(manager, account["id"])
    first = drafts.save(manager, account["id"], "第一稿", None, initial["base_sha256"])
    second = drafts.save(manager, account["id"], "第二稿", first["version"], first["base_sha256"])
    with pytest.raises(drafts.DraftConflict):
        drafts.save(manager, account["id"], "旧页面", first["version"], first["base_sha256"])
    with pytest.raises(drafts.DraftConflict):
        drafts.apply(manager, account["id"], first["version"], first["base_sha256"])
    manager.configure_automation(account["id"], prompt="另一处生效设定")
    with pytest.raises(drafts.DraftConflict):
        drafts.apply(manager, account["id"], second["version"], second["base_sha256"])
    current = drafts.read(manager, account["id"])
    assert current["conflict"] and current["markdown"] == "第二稿"
    resolved = drafts.resolve(
        manager, account["id"], "核对后的稿件", current["version"], current["active_sha256"]
    )
    assert not resolved["conflict"]
    assert (
        drafts.apply(manager, account["id"], resolved["version"], resolved["base_sha256"])["account"][
            "prompt"
        ]
        == "核对后的稿件"
    )


def test_same_content_and_empty_drafts_do_not_repeatedly_invalidate_tasks(tmp_path):
    store, manager, account, conversation = setup(tmp_path)
    before = manager.account(account["id"])
    initial = drafts.read(manager, account["id"])
    assert (
        drafts.save(manager, account["id"], initial["markdown"], None, initial["base_sha256"])["version"]
        is None
    )
    empty = drafts.save(manager, account["id"], "", None, initial["base_sha256"])
    with pytest.raises(ValueError, match="启用前"):
        drafts.apply(manager, account["id"], empty["version"], empty["base_sha256"])
    assert manager.account(account["id"])["epoch"] == before["epoch"]
    same = drafts.save(manager, account["id"], before["prompt"], empty["version"], empty["base_sha256"])
    result = drafts.apply(manager, account["id"], same["version"], same["base_sha256"])
    assert not result["changed"] and result["account"]["epoch"] == before["epoch"]


def actor(store, mode, payload):
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("prompt_draft_actor.py")), mode],
        env={
            **os.environ,
            "DRAFT_ACTOR_DATABASE": store.url,
            "DRAFT_ACTOR_USER": "1",
            "DRAFT_ACTOR_PAYLOAD": json.dumps(payload),
        },
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def parallel(first, second):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(process.communicate, "\n", timeout=25) for process in (first, second)]
        results = [future.result() for future in futures]
    for process, (_, stderr) in zip((first, second), results):
        assert process.returncode == 0, stderr
    return [json.loads(stdout) for stdout, _ in results]


def test_two_processes_cas_apply_crash_recovery_and_pool_resources(tmp_path):
    store, manager, account, conversation = setup(tmp_path)
    initial = drafts.read(manager, account["id"])
    payload = {"aid": account["id"], "version": None, "base_sha256": initial["base_sha256"]}
    results = parallel(
        actor(store, "save", {**payload, "markdown": "进程一"}),
        actor(store, "save", {**payload, "markdown": "进程二"}),
    )
    assert sorted(row["status"] for row in results) == ["conflict", "saved"]
    saved = drafts.read(manager, account["id"])
    before = manager.account(account["id"])["epoch"]
    payload.update(version=saved["version"])
    results = parallel(actor(store, "apply", payload), actor(store, "apply", payload))
    assert sorted(row["changed"] for row in results) == [False, True]
    assert manager.account(account["id"])["epoch"] == before + 1
    process = actor(store, "crash", {"aid": account["id"]})
    try:
        assert process.stdout.readline().strip() == "locked"
        process.communicate("crash\n", timeout=15)
        assert process.returncode == 23
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    current = drafts.read(manager, account["id"])
    recovered = drafts.save(manager, account["id"], "崩溃后继续", current["version"], current["base_sha256"])
    assert recovered["markdown"] == "崩溃后继续"
    assert store.pool.get_stats()["pool_available"] == store.pool.get_stats()["pool_size"]


def test_draft_http_auth_csrf_limit_and_account_scope(tmp_path):
    http, store, config, sms, clock = auth_setup(tmp_path)
    manager = http.app.state.manager
    account = manager.add_account("draft-fixture")
    url = f"/wechat/api/v1/accounts/{account['id']}/prompt-draft"
    assert http.get(url).status_code == 401
    headers = login(http)
    draft = http.get(url).json()
    body = {"markdown": "通过 HTTP 保存的草稿", "version": None, "base_sha256": draft["base_sha256"]}
    assert http.put(url, json=body).status_code == 403
    result = http.put(url, json=body, headers=headers)
    assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
    assert manager.account(account["id"])["prompt"] == ""
    assert http.put(url, json={**body, "markdown": "字" * 50001}, headers=headers).status_code == 422
    with user_scope(99999):
        with pytest.raises(ValueError, match="账号不存在"):
            drafts.read(manager, account["id"])
        with pytest.raises(ValueError, match="账号不存在"):
            drafts.save(manager, account["id"], "越权", None, draft["base_sha256"])
    assert drafts.read(manager, account["id"])["markdown"] == body["markdown"]
    saved = result.json()
    application = {"version": saved["version"], "base_sha256": saved["base_sha256"]}
    first = http.post(url + "/apply", json=application, headers=headers)
    assert first.status_code == 200 and first.json()["changed"]
    assert first.json()["account"]["prompt"] == body["markdown"]
    repeat = http.post(url + "/apply", json=application, headers=headers)
    assert repeat.status_code == 200 and not repeat.json()["changed"]
    assert (
        http.post(url + "/apply", json={**application, "version": "stale"}, headers=headers).status_code
        == 409
    )


def test_two_users_and_accounts_have_independent_drafts(tmp_path):
    store, manager, account, conversation = setup(tmp_path)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO users(id,channel,identifier,username,created_at,callback_key_hash,callback_key,callback_url) VALUES(2,'phone','13900000001','用户二',%s,'isolated-user-two','isolated-user-two-key','http://testserver/wechatapi/callback/two')",
            (NOW,),
        )
    first = drafts.read(manager, account["id"])
    drafts.save(manager, account["id"], "用户一草稿", first["version"], first["base_sha256"])
    with user_scope(2):
        other = manager.add_account("draft-other-user")
        second = drafts.read(manager, other["id"])
        drafts.save(manager, other["id"], "用户二草稿", second["version"], second["base_sha256"])
        assert drafts.read(manager, other["id"])["markdown"] == "用户二草稿"
        assert store.setting(drafts.key(account["id"])) is None
        with pytest.raises(ValueError, match="账号不存在"):
            drafts.read(manager, account["id"])
    assert drafts.read(manager, account["id"])["markdown"] == "用户一草稿"
    assert store.setting(drafts.key(other["id"])) is None


def test_row_lock_wait_is_bounded_and_cancellation_keeps_committed_result_recoverable(tmp_path):
    from wechat_manager.database_async import database_call

    store, manager, account, conversation = setup(tmp_path)
    initial = drafts.read(manager, account["id"])
    process = actor(store, "crash", {"aid": account["id"]})
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(psycopg.OperationalError):
            drafts.save(manager, account["id"], "有界等待", None, initial["base_sha256"])

        async def canceled_request():
            task = asyncio.create_task(
                database_call(drafts.save, manager, account["id"], "取消后核对", None, initial["base_sha256"])
            )
            await asyncio.sleep(0.1)
            task.cancel()
            await asyncio.sleep(0.1)
            assert not task.done()
            process.stdin.write("crash\n")
            process.stdin.flush()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)

        asyncio.run(canceled_request())
        process.wait(timeout=10)
        assert process.returncode == 23
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    result = drafts.read(manager, account["id"])
    assert result["markdown"] == "取消后核对"
    assert result["active_prompt"] == initial["active_prompt"]
    assert (
        drafts.save(manager, account["id"], result["markdown"], None, initial["base_sha256"])["version"]
        == result["version"]
    )
    assert store.pool.get_stats()["pool_available"] == store.pool.get_stats()["pool_size"]


@pytest.mark.skipif(not os.getenv("PROMPT_BROWSER_DIST"), reason="仅服务器带候选构建的隔离浏览器环境")
def test_built_browser_draft_and_apply_use_real_authenticated_api(tmp_path):
    from playwright.sync_api import expect, sync_playwright

    http, store, config, sms, clock = auth_setup(tmp_path)
    config.public_url = "http://127.0.0.1"
    manager = http.app.state.manager
    account = manager.add_account("integrated-browser")
    manager.verify_identity("integrated-browser", {"wxid": "wxid_isolated_browser", "nickName": "隔离账号"})
    manager.configure_automation(account["id"], prompt="", ai_enabled=False)
    login(http)
    calls = []

    def dispatch(route):
        request = route.request
        path = request.url.split("http://127.0.0.1", 1)[1]
        response = http.request(
            request.method,
            path,
            content=request.post_data_buffer,
            headers={k: v for k, v in request.headers.items() if k not in {"host", "cookie"}},
        )
        calls.append((request.method, path, response.status_code))
        if response.status_code >= 400:
            calls.append(("error", path, response.text))
        route.fulfill(
            status=response.status_code,
            body=response.content,
            headers={
                k: v for k, v in response.headers.items() if k not in {"content-length", "content-encoding"}
            },
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            browser_errors = []
            page.on("pageerror", lambda error: browser_errors.append(str(error)))
            page.route("http://127.0.0.1/**", dispatch)
            page.goto("http://127.0.0.1/wechat/")
            page.get_by_role("button", name="智能助手", exact=True).click()
            editor = page.get_by_role("textbox", name="助手设定内容", exact=True)
            try:
                expect(editor).to_have_attribute("contenteditable", "true", timeout=10000)
            except AssertionError:
                raise AssertionError(
                    {"calls": calls, "errors": browser_errors, "body": page.locator("body").inner_text()}
                ) from None
            editor.press_sequentially("真实接口草稿")
            expect(page.locator("#assistant-panel-prompt .prompt-draft-status")).to_have_text(
                "", timeout=8000
            )
            page.wait_for_timeout(1800)
            assert drafts.read(manager, account["id"])["markdown"] == "真实接口草稿", json.dumps(
                {"calls": calls, "errors": browser_errors, "body": page.locator("body").inner_text()},
                ensure_ascii=False,
            )
            assert manager.account(account["id"])["prompt"] == ""
            page.get_by_role("heading", name="回复范围", exact=True).click()
            expect(page.locator("#assistant-panel-prompt .prompt-draft-status")).to_have_text(
                "", timeout=8000
            )
            assert manager.account(account["id"])["prompt"] == "真实接口草稿"
            applications = [row for row in calls if row[1].endswith("/prompt-draft/apply")]
            assert applications == [
                ("POST", f"/wechat/api/v1/accounts/{account['id']}/prompt-draft/apply", 200)
            ]
            assert not drafts.read(manager, account["id"])["dirty"]
            owner = page.get_by_role("textbox", name="号主资料内容", exact=True)
            expect(owner).to_have_attribute("contenteditable", "true")
            owner.fill("号主公开资料：青叶科技")
            page.wait_for_timeout(1800)
            owner_draft = drafts.read(manager, account["id"], owner_wxid="wxid_isolated_browser")
            assert owner_draft["markdown"].strip() == "号主公开资料：青叶科技"
            assert manager.account(account["id"])["owner_profile"] == ""
            page.get_by_role("button", name="系统设置", exact=True).click()
            assert manager.account(account["id"])["owner_profile"].strip() == "号主公开资料：青叶科技"
            assert manager.account(account["id"])["prompt"] == "真实接口草稿"
        finally:
            browser.close()
