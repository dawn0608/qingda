"""服务器候选构建的号主资料布局与双编辑区交互验证，不写生产数据。"""

import argparse
import hashlib
import http.server
import json
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            target = (args.dist / urlsplit(path).path.removeprefix("/wechat/")).resolve()
            return str(target if target.is_relative_to(args.dist.resolve()) else args.dist / "missing")

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    account = dict(
        id=999,
        wxid="wxid_fixture",
        nickname="布局隔离账号",
        prompt="原助手设定",
        owner_profile="",
        enabled=1,
        online=1,
        ai_enabled=1,
    )
    drafts = {}
    writes, errors, results = [], [], []
    controls = {"fail": False}

    def receipt(kind):
        active = account["prompt" if kind == "prompt" else "owner_profile"]
        saved = drafts.get(kind, {})
        digest = hashlib.sha256(active.encode()).hexdigest()
        return dict(
            version=saved.get("version"),
            markdown=saved.get("markdown", active),
            base_sha256=saved.get("base_sha256", digest),
            active_prompt=active,
            active_sha256=digest,
            dirty=saved.get("markdown", active) != active,
            conflict=saved.get("base_sha256", digest) != digest,
        )

    def route(r):
        path = urlsplit(r.request.url).path.split("/api/v1")[-1]
        if "-draft" in path:
            kind = "owner-profile" if "owner-profile-draft" in path else "prompt"
            row = receipt(kind)
            if r.request.method == "GET":
                r.fulfill(json=row)
                return
            body = r.request.post_data_json
            if controls["fail"] and kind == "owner-profile":
                r.fulfill(status=503, json={"error": "fixture unavailable"})
                return
            if path.endswith("/resolve"):
                drafts[kind] = dict(
                    markdown=body["markdown"], version=uuid.uuid4().hex, base_sha256=row["active_sha256"]
                )
                r.fulfill(json=receipt(kind))
                return
            if (
                body["version"] != row["version"]
                or body["base_sha256"] != row["active_sha256"]
                or row["conflict"]
            ):
                r.fulfill(status=409, json={"error": "fixture conflict"})
                return
            writes.append((kind, r.request.method, body))
            if path.endswith("/apply"):
                field = "prompt" if kind == "prompt" else "owner_profile"
                account[field] = row["markdown"]
                drafts[kind]["base_sha256"] = hashlib.sha256(account[field].encode()).hexdigest()
                r.fulfill(json=dict(draft=receipt(kind), account=dict(account), changed=row["dirty"]))
            else:
                drafts[kind] = dict(
                    markdown=body["markdown"], version=uuid.uuid4().hex, base_sha256=row["active_sha256"]
                )
                r.fulfill(json=receipt(kind))
            return
        if path.endswith("/reply-scope"):
            r.fulfill(json=dict(items=[], total=0, page=1, page_size=10, server_time=0))
        else:
            r.fulfill(
                json={
                    "/auth/session": dict(username="隔离验收", user_id=1, csrf="fixture"),
                    "/accounts": [dict(account)],
                    "/settings": {},
                }.get(path, {})
            )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/wechat/api/v1/**", route)
        page.add_init_script("sessionStorage.setItem('wechat-page','automation')")
        page.on("pageerror", lambda error: errors.append(str(error)))
        origin = f"http://127.0.0.1:{server.server_port}/wechat/"

        def load(width=1440, height=900):
            page.set_viewport_size(dict(width=width, height=height))
            page.goto(origin)
            for name in ("助手设定内容", "号主资料内容"):
                expect(page.get_by_role("textbox", name=name, exact=True)).to_have_attribute(
                    "contenteditable", "true"
                )

        long = "\n\n".join(f"第{i}段：仅用于隔离验证的长正文。" for i in range(160))
        account.update(prompt=long, owner_profile=long)
        for width, height in ((1656, 950), (1440, 900), (1366, 768), (1280, 600), (390, 844), (390, 600)):
            load(width, height)
            value = page.evaluate("""() => {
              const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height}};
              return {prompt:box('#assistant-panel-prompt'),profile:box('#assistant-panel-owner-profile'),scope:box('#assistant-panel-scope'),
                scroll:[...document.querySelectorAll('.prompt-composer')].map(e=>({height:e.clientHeight,total:e.scrollHeight,style:getComputedStyle(e).overflowY,bar:getComputedStyle(e).scrollbarWidth})),
                horizontal:document.documentElement.scrollWidth-innerWidth, footer:box('.reply-scope-footer'),viewport:innerHeight};
            }""")
            assert abs(value["prompt"]["height"] - value["profile"]["height"]) <= 1, value
            assert abs(value["profile"]["top"] - value["prompt"]["bottom"] - 20) <= 1, value
            if width > 1100:
                assert abs(value["prompt"]["top"] - value["scope"]["top"]) <= 1, value
                assert abs(value["profile"]["bottom"] - value["scope"]["bottom"]) <= 1, value
            assert value["horizontal"] <= 1 and value["footer"]["bottom"] <= height + 1, value
            assert all(
                v["total"] > v["height"] and v["style"] == "auto" and v["bar"] != "none"
                for v in value["scroll"]
            ), value
            page.screenshot(path=args.output / f"layout-{width}-{height}.png")
            results.append(dict(name=f"layout-{width}-{height}", passed=True, geometry=value))
            for label in ("助手设定", "号主资料"):
                page.get_by_role("textbox", name=label + "内容", exact=True).press("Control+/")
                source = page.get_by_role("textbox", name=label + " Markdown 源码", exact=True)
                expect(source).to_be_visible()
                assert source.evaluate("e=>e.scrollHeight>e.clientHeight"), label
            results.append(dict(name=f"source-scroll-{width}-{height}", passed=True))

        account.update(prompt="原助手设定", owner_profile="")
        drafts.clear()
        page.evaluate("localStorage.clear()")
        load()
        editor = page.get_by_role("textbox", name="号主资料内容", exact=True)
        editor.fill("姓名：林一，公司：青叶科技")
        page.wait_for_timeout(1900)
        assert receipt("owner-profile")["markdown"].strip() == "姓名：林一，公司：青叶科技"
        assert account["owner_profile"] == "" and account["prompt"] == "原助手设定"
        page.get_by_role("heading", name="回复范围", exact=True).click()
        expect(page.locator("#assistant-panel-owner-profile .prompt-draft-status")).to_have_text("")
        assert account["owner_profile"].strip() == "姓名：林一，公司：青叶科技"
        assert account["prompt"] == "原助手设定"
        results.append(dict(name="independent-draft-and-apply", passed=True))

        controls["fail"] = True
        editor.fill("失败时保留这份资料")
        page.get_by_role("heading", name="回复范围", exact=True).click()
        retry = page.locator("#assistant-panel-owner-profile").get_by_role("button", name="草稿待重试")
        expect(retry).to_be_visible()
        expect(editor).to_contain_text("失败时保留这份资料")
        controls["fail"] = False
        retry.click()
        expect(page.locator("#assistant-panel-owner-profile .prompt-draft-status")).to_have_text("")
        assert account["owner_profile"].strip() == "失败时保留这份资料"
        results.append(dict(name="save-failure-and-retry", passed=True))
        editor.fill("")
        editor.press("Control+s")
        expect(page.locator("#assistant-panel-owner-profile .prompt-draft-status")).to_have_text("")
        assert account["owner_profile"].strip() == "" and account["ai_enabled"]
        results.append(dict(name="clear-keeps-assistant-enabled", passed=True))
        assert not errors, errors
        (args.output / "results.json").write_text(
            json.dumps(dict(results=results, errors=errors, writes=len(writes)), ensure_ascii=False, indent=2)
        )
        print(json.dumps(dict(passed=len(results), errors=errors)))
        browser.close()
    server.shutdown()


if __name__ == "__main__":
    main()
