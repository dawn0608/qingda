"""服务器隔离布局验收：真实前端构建、内存联系人夹具，不写生产业务数据。"""

import argparse
import hashlib
import http.server
import json
import pathlib
import threading
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--reproduce", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            target = (args.dist / urlsplit(path).path.removeprefix("/wechat/")).resolve()
            return str(target if target.is_relative_to(args.dist.resolve()) else args.dist / "missing")

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    results, errors, writes = [], [], []
    controls = {"total": 10, "paused": False, "long": False, "fail": False, "sync": "idle"}
    prompt = "## 助手设定\n\n完整编辑区高度基准。"
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    account = dict(id=999, nickname="布局隔离账号", prompt=prompt, enabled=1, online=1, ai_enabled=1)

    def route(r):
        path = urlsplit(r.request.url).path.split("/api/v1")[-1]
        if r.request.method != "GET":
            writes.append((r.request.method, path))
            r.fulfill(status=500, json={"error": "unexpected_fixture_write"})
            return
        if path.endswith("/reply-scope"):
            if controls["fail"]:
                r.fulfill(status=503, json={"error": "fixture unavailable"})
                return
            query = parse_qs(urlsplit(r.request.url).query)
            kind = query.get("kind", ["private"])[0]
            q = query.get("q", [""])[0]
            label = "好友" if kind == "private" else "群聊"
            items = []
            for i in range(1, controls["total"] + 1):
                name = f"{label} {i:02d}"
                if controls["long"]:
                    name += "很长的备注名称" * 12
                if q and q not in name:
                    continue
                items.append(dict(id=i, account_id=999, peer_id=f"fixture-{i}",
                                  display_name=name, name=name, kind=kind, allowed=True,
                                  auto_reply_status="enabled", removed_at=None, profile={},
                                  pause_until=time.time()+600 if controls["paused"] else 0))
            if query.get("status", ["all"])[0] == "disabled":
                items = []
            size = int(query.get("page_size", [10])[0])
            number = min(int(query.get("page", [1])[0]), max(1, (len(items)+size-1)//size))
            r.fulfill(json=dict(items=items[(number-1)*size:number*size], total=len(items),
                                page=number, page_size=size, server_time=time.time()))
            return
        data = {
            "/auth/session": dict(username="隔离验收", user_id=1, csrf="fixture"),
            "/accounts": [{**account, "contacts": {"state": controls["sync"], "updated_at": time.time() if controls["sync"] == "idle" else None}}], "/settings": {},
            "/accounts/999/prompt-draft": dict(version=None, markdown=prompt,
                active_prompt=prompt, active_sha256=digest, base_sha256=digest,
                dirty=False, conflict=False),
            "/accounts/999/owner-profile-draft": dict(version=None, markdown="", active_prompt="",
                active_sha256=hashlib.sha256(b"").hexdigest(), base_sha256=hashlib.sha256(b"").hexdigest(),
                dirty=False, conflict=False),
        }.get(path, {})
        r.fulfill(json=data)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/wechat/api/v1/**", route)
        page.add_init_script("sessionStorage.setItem('wechat-page','automation')")
        page.on("pageerror", lambda error: errors.append(str(error)))

        def load(width, height, total=10, paused=False, long=False):
            controls.update(total=total, paused=paused, long=long, fail=False, sync="idle")
            page.set_viewport_size(dict(width=width, height=height))
            page.goto(origin + "/wechat/")
            expect(page.get_by_role("textbox", name="助手设定内容", exact=True)).to_have_attribute("contenteditable", "true")
            expect(page.locator(".reply-scope-row")).to_have_count(min(total, 10))

        def measure():
            return page.evaluate("""() => {
              const box = s => {const r=document.querySelector(s).getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height,left:r.left,right:r.right}};
              const list=document.querySelector('.reply-scope-list');
              return {prompt:box('.assistant-editors'),reply:box('#assistant-panel-scope'),list:box('.reply-scope-list'),
                footer:box('.reply-scope-footer'),viewport:{width:innerWidth,height:innerHeight},
                rows:[...document.querySelectorAll('.reply-scope-row')].map(el=>{
                  const r=el.getBoundingClientRect(),info=el.querySelector('.reply-contact-info').getBoundingClientRect(),avatar=el.querySelector('.avatar').getBoundingClientRect();
                  return {top:r.top,bottom:r.bottom,height:r.height,infoBottom:info.bottom,avatarBottom:avatar.bottom,avatarTop:avatar.top};
                }), overflow:list.scrollHeight-list.clientHeight,
                horizontalOverflow:document.documentElement.scrollWidth-innerWidth};
            }""")

        def check(*, desktop=True, full=False):
            value = measure()
            if desktop:
                assert abs(value["prompt"]["bottom"]-value["reply"]["bottom"]) <= 1, value
                assert abs(value["prompt"]["top"]-value["reply"]["top"]) <= 1, value
            assert value["horizontalOverflow"] <= 1, value
            assert value["footer"]["bottom"] <= value["reply"]["bottom"]+1, value
            assert value["list"]["bottom"] <= value["footer"]["top"]+1, value
            for row in value["rows"]:
                assert row["height"] >= (39.5 if desktop else 43.5), value
                assert row["infoBottom"] <= row["bottom"]+0.5, value
                assert row["avatarBottom"] <= row["bottom"]+0.5 and row["avatarTop"] >= row["top"]-0.5, value
            if full:
                assert len(value["rows"]) == 10 and value["overflow"] <= 1, value
                assert abs(value["rows"][-1]["bottom"]-value["list"]["bottom"]) <= 1, value
                assert max(r["height"] for r in value["rows"])-min(r["height"] for r in value["rows"]) <= 1, value
            assert not errors and not writes, (errors, writes)
            return value

        def test(name, action):
            try:
                value = action()
                results.append(dict(name=name, passed=True, geometry=value))
            except Exception as error:
                results.append(dict(name=name, passed=False, error=str(error)))
            (args.output / "progress.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))

        def full_page(width, height, kind):
            load(width, height)
            if kind == "group":
                page.get_by_role("button", name="群聊", exact=True).click()
                expect(page.locator(".reply-scope-row strong").first).to_have_text("群聊 01")
            value = check(full=True)
            page.screenshot(path=args.output/f"{width}-{height}-{kind}.png")
            return value

        for width, height in ((1994,966), (1920,1080), (1440,900), (1366,768)):
            for kind in ("private", "group"):
                test(f"ten-{width}-{height}-{kind}", lambda w=width,h=height,k=kind: full_page(w,h,k))
        if not args.reproduce:
            for count in (0,1,2,9):
                test(f"partial-page-{count}", lambda n=count: (load(1440,900,n),check())[1])

            def pagination():
                load(1440,900,23)
                page.get_by_role("button", name="下一页", exact=True).click()
                expect(page.locator(".reply-scope-row strong").first).to_have_text("好友 11")
                check(full=True)
                page.get_by_role("button", name="下一页", exact=True).click()
                expect(page.locator(".reply-scope-row")).to_have_count(3)
                return check()
            test("pagination-full-and-last-page", pagination)

            def resize():
                load(1440,900)
                for width,height in ((1366,768),(1994,966),(1440,900)):
                    page.set_viewport_size(dict(width=width,height=height))
                    check(full=True)
                return measure()
            test("live-window-resize", resize)

            def compact(width,height,paused=False,long=False):
                load(width,height,10,paused,long)
                value = check(desktop=width>1100)
                assert value["overflow"]>0, value
                page.locator(".reply-scope-list").evaluate("el=>el.scrollTop=el.scrollHeight")
                last=measure()["rows"][-1]
                assert last["bottom"] <= value["list"]["bottom"]+1
                return value
            for width,height in ((1280,720),(1366,600),(1100,900),(390,844),(320,640)):
                test(f"compact-{width}-{height}",lambda w=width,h=height: compact(w,h))
            test("paused-long-names-remain-readable", lambda: compact(1366,768,True,True))

            def errors_and_empty_search():
                load(1440,900)
                page.get_by_role("textbox", name="搜索回复范围",exact=True).fill("不存在的联系人")
                expect(page.locator(".reply-scope-row")).to_have_count(0)
                check()
                page.get_by_role("textbox", name="搜索回复范围",exact=True).fill("")
                expect(page.locator(".reply-scope-row")).to_have_count(10)
                controls["fail"]=True
                expect(page.get_by_role("alert")).to_be_visible(timeout=7000)
                check()
                controls["fail"]=False
                expect(page.get_by_role("button", name="刷新回复范围",exact=True)).to_have_count(0)
                expect(page.locator(".reply-scope-row button").first).to_be_enabled(timeout=7000)
                # 全站提示已统一为6.5秒顶部浮层，恢复读取不立即撤销已展示通知。
                expect(page.get_by_role("alert")).to_have_count(0, timeout=8000)
                return check(full=True)
            test("empty-search-and-read-error",errors_and_empty_search)

            def directory_sync_status():
                load(1440,900)
                search = page.get_by_role("textbox", name="搜索回复范围", exact=True)
                search.fill("好友 01")
                expect(page.locator(".reply-scope-row")).to_have_count(1)
                controls["sync"] = "running"
                expect(page.locator("#assistant-panel-scope .panel-heading-actions [role='status']")).to_have_text("通讯录同步中", timeout=7000)
                expect(search).to_have_value("好友 01")
                controls["sync"] = "failed"
                expect(page.locator("#assistant-panel-scope .panel-heading-actions [role='status']")).to_have_text("通讯录更新暂不可用", timeout=7000)
                expect(page.locator(".reply-scope-row")).to_have_count(1)
                expect(search).to_have_value("好友 01")
                controls["sync"] = "idle"
                expect(page.locator("#assistant-panel-scope .panel-heading-actions [role='status']")).to_have_count(0, timeout=7000)
                return check()
            test("directory-sync-preserves-search-and-current-items", directory_sync_status)
        browser.close()
    server.shutdown()
    report=dict(passed=sum(r["passed"] for r in results), total=len(results), cases=results,
                environment="server isolated Chromium, memory API fixtures",time=time.time())
    (args.output/"results.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))
    raise SystemExit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()
