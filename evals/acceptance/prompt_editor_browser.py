"""服务器隔离浏览器回归；仅用内存 API 夹具，不访问生产业务数据。

用独立 Playwright 环境运行：python prompt_editor_browser.py --dist <frontend/dist> --output <证据目录>
"""

import argparse
import http.server
import hashlib
import uuid
import json
import pathlib
import threading
import time

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--reproduce", action="store_true")
    parser.add_argument("--match", default="")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def translate_path(self, path):
            path = path.split("?", 1)[0].removeprefix("/wechat/")
            target = (args.dist / path).resolve()
            return str(target if target.is_relative_to(args.dist.resolve()) else args.dist / "missing")

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors, writes, draft_writes, requests, held = [], [], [], [], []
        account, stored, controls = {}, {}, {}

        def fingerprint(value):
            return hashlib.sha256(value.encode()).hexdigest()

        def receipt():
            active = account.get("prompt", "")
            return {"version": stored.get("version"), "markdown": stored.get("markdown", active),
                "base_sha256": stored.get("base_sha256", fingerprint(active)),
                "active_prompt":active, "active_sha256":fingerprint(active),
                "dirty":stored.get("markdown",active)!=active,
                "conflict":stored.get("base_sha256",fingerprint(active))!=fingerprint(active)}

        def route(r):
            path=r.request.url.split("/api/v1")[-1].split("?")[0]
            if path.endswith("/owner-profile-draft") and r.request.method == "GET":
                digest = fingerprint("")
                r.fulfill(json=dict(version=None, markdown="", active_prompt="", active_sha256=digest,
                                    base_sha256=digest, dirty=False, conflict=False))
                return
            expected_method = "POST" if path.endswith(("/prompt-draft/apply", "/prompt-draft/resolve")) else "PUT" if path.endswith("/prompt-draft") and r.request.method != "GET" else None
            if expected_method and r.request.method != expected_method:
                errors.append("unexpected_fixture_method: " + path + " " + r.request.method)
                r.fulfill(status=405,json={"error":"unexpected_fixture_method"})
                return
            if path == "/accounts/999/online":
                r.fulfill(status=200,json=account)
                return
            if path == "/accounts/999/prompt-draft":
                if r.request.method=="GET":
                    r.fulfill(status=200,json=receipt())
                    return
                body=r.request.post_data_json
                requests.append({"path":path,"body":body})
                if controls.get("failSave"):
                    r.fulfill(status=503,json={"error":"fixture unavailable"})
                    return
                row=receipt()
                if body["base_sha256"]!=row["active_sha256"] or row["conflict"] or (body["version"]!=row["version"] and body["markdown"]!=row["markdown"]):
                    r.fulfill(status=409,json={"error":"fixture conflict"})
                    return
                if body["markdown"]!=row["markdown"]:
                    stored.update(markdown=body["markdown"],version=uuid.uuid4().hex,base_sha256=body["base_sha256"])
                    draft_writes.append(dict(body))
                response=receipt()
                if controls.pop("holdSave",False):
                    held.append((r,response))
                elif controls.pop("lostSaveAck",False):
                    r.fulfill(status=503,json={"error":"fixture lost ack"})
                else:
                    r.fulfill(status=200,json=response)
                return
            if path == "/accounts/999/prompt-draft/apply":
                body=r.request.post_data_json
                requests.append({"path":path,"body":body})
                row=receipt()
                if body["version"]!=row["version"]:
                    r.fulfill(status=409,json={"error":"fixture conflict"})
                    return
                if controls.get("failApply"):
                    r.fulfill(status=503,json={"error":"fixture unavailable"})
                    return
                changed=row["dirty"]
                if changed:
                    account["prompt"]=row["markdown"]
                    account["version"]+=1
                    stored["base_sha256"]=fingerprint(account["prompt"])
                    writes.append({"path":path,"body":{"prompt":account["prompt"]}})
                response={"draft":receipt(),"account":dict(account),"changed":changed}
                if controls.pop("holdApply",False):
                    held.append((r,response))
                elif controls.pop("lostApplyAck",False):
                    r.fulfill(status=503,json={"error":"fixture lost ack"})
                else:
                    r.fulfill(status=200,json=response)
                return
            if path == "/accounts/999/prompt-draft/resolve":
                body=r.request.post_data_json
                stored.update(markdown=body["markdown"],version=uuid.uuid4().hex,base_sha256=fingerprint(account["prompt"]))
                r.fulfill(status=200,json=receipt())
                return
            if r.request.method!="GET":
                errors.append("unexpected_fixture_write: "+path)
                r.fulfill(status=500,json={"error":"unexpected_fixture_write"})
                return
            data={"/auth/session":{"username":"验收用户","user_id":controls.get("userId",1),"csrf":"fixture"},
                "/accounts":[account],"/settings":{}}.get(path,{"items":[],"total":0,"page":1,"page_size":10})
            r.fulfill(status=200,json=data)

        def load(prompt="普通正文。",width=1440):
            nonlocal page
            page.close()
            account.clear()
            account.update(id=999,nickname="编辑器验收",prompt=prompt,version=1,enabled=1,online=1,ai_enabled=False,status="online")
            stored.clear()
            controls.clear()
            held.clear()
            writes.clear()
            draft_writes.clear()
            requests.clear()
            errors.clear()
            page=browser.new_page(viewport={"width":width,"height":1000})
            page.on("pageerror",lambda error: errors.append(str(error)))
            page.route("**/wechat/api/v1/**",route)
            page.add_init_script("sessionStorage.setItem('wechat-page','automation')")
            page.goto(origin+"/wechat/")
            ed=page.get_by_role("textbox",name="助手设定内容",exact=True)
            expect(ed).to_be_visible()
            expect(ed).to_have_attribute("contenteditable","true")
            ed.focus()
            ed.press("Control+End")
            return ed

        def until(condition):
            deadline=time.monotonic()+10
            while not condition() and time.monotonic()<deadline:
                page.wait_for_timeout(40)
            assert condition(), "fixture condition timed out"

        def source():
            field = page.get_by_role("textbox", name="助手设定 Markdown 源码", exact=True)
            if not field.is_visible():
                page.get_by_role("textbox", name="助手设定内容", exact=True).press("Control+/")
            expect(field).to_be_visible()
            return field

        def preview():
            field = page.get_by_role("textbox", name="助手设定 Markdown 源码", exact=True)
            if field.is_visible():
                field.press("Control+/")
            return page.get_by_role("textbox", name="助手设定内容", exact=True)

        def save():
            count=len(writes)
            page.get_by_role("heading",name="回复范围",exact=True).click()
            until(lambda: len(writes)>count)
            assert len(writes)==count+1
            return writes[-1]["body"]["prompt"]

        def test(name, run):
            if args.match and args.match not in name:
                return
            try:
                run()
                assert not errors, errors
                results.append({"name": name, "passed": True})
            except Exception as error:
                page.screenshot(path=str(args.output / (name + ".png")), full_page=True)
                results.append({"name": name, "passed": False, "error": str(error)[:1500], "fixture_writes": list(writes), "fixture_requests": list(requests)})
            (args.output / "progress.json").write_text(json.dumps({"cases": results, "errors": errors}, ensure_ascii=False, indent=2))

        def dash(marker):
            ed = load("## 真实与边界\n\n普通正文第一行。\n普通正文第二行。")
            ed.press("Enter")
            ed.press_sequentially(marker)
            expect(ed).to_contain_text(marker)
            ed.press("Enter")
            ed.press_sequentially("后续正文")
            expect(ed.locator("h2")).to_have_count(1)
            expect(ed.locator("p").first).to_contain_text("普通正文第二行。")
            if len(marker) >= 3:
                expect(ed.locator("hr")).to_have_count(1)
            elif marker != "-":
                expect(ed).to_contain_text(marker)

        for marker in ("-", "--", "---", "-----", "***", "___", "="):
            test("paragraph-marker-" + str(len(results)), lambda m=marker: dash(m))
        if not args.reproduce:
            def paragraph():
                ed = load("第一段")
                ed.press("Enter")
                ed.press_sequentially("第二段")
                expect(ed.locator("p")).to_have_count(2)
                assert source().input_value() == "第一段\n\n第二段"
            test("enter-new-paragraph", paragraph)

            def soft_break():
                ed = load("第一行")
                ed.press("Shift+Enter")
                ed.press_sequentially("第二行")
                expect(ed.locator("p")).to_have_count(1)
                value = source().input_value()
                assert "第一行" in value and "\n第二行" in value, repr(value)
                assert "第一行\n第二行" in preview().inner_text()
                saved = save()
                page.reload()
                expect(page.get_by_role("textbox", name="助手设定内容", exact=True)).to_contain_text("第二行")
                assert source().input_value() == saved
            test("shift-enter-roundtrip", soft_break)

            def split():
                ed = load("前半后半")
                ed.press("ArrowLeft")
                ed.press("ArrowLeft")
                ed.press("Enter")
                value=source().input_value()
                assert value == "前半\n\n后半", repr(value)
            test("split-paragraph-at-caret", split)

            def list_case(marker):
                ed = load("")
                ed.press_sequentially(marker + "第一项")
                ed.press("Enter")
                ed.press_sequentially("第二项")
                expect(ed.locator("li")).to_have_count(2)
                ed.press("Enter")
                ed.press("Enter")
                ed.press_sequentially("列表外正文")
                expect(ed.locator("li")).to_have_count(2)
                expect(ed.locator("p").last).to_contain_text("列表外正文")
            for marker in ("- ", "* ", "+ ", "1. ", "- [ ] "):
                test("list-continue-exit-" + str(len(results)), lambda m=marker: list_case(m))

            def nested():
                ed = load("- 父项")
                ed.press("Enter")
                ed.press("Tab")
                ed.press_sequentially("子项")
                expect(ed.locator("li li")).to_have_count(1)
                ed.press("Shift+Tab")
                expect(ed.locator("li li")).to_have_count(0)
            test("list-indent-outdent", nested)

            def heading():
                ed = load("")
                ed.press_sequentially("## 新标题")
                ed.press("Enter")
                ed.press_sequentially("标题后的正文")
                expect(ed.locator("h2")).to_have_count(1)
                expect(ed.locator("p").last).to_contain_text("标题后的正文")
            test("heading-enter-body", heading)

            def quote():
                ed = load("")
                ed.press_sequentially("> 引用")
                ed.press("Enter")
                ed.press("Enter")
                ed.press_sequentially("引用外")
                expect(ed.locator("blockquote")).to_have_count(1)
                expect(ed.locator(":scope > p").last).to_contain_text("引用外")
            test("quote-enter-exit", quote)

            def code():
                ed = load("")
                ed.press_sequentially("```python")
                ed.press("Enter")
                ed.press_sequentially("a = 1")
                ed.press("Enter")
                ed.press_sequentially("b = 2")
                expect(ed.locator('[data-type="code-block"]')).to_have_count(1)
                assert "a = 1\nb = 2" in source().input_value()
            test("fenced-code-lines", code)

            def inline():
                ed = load("")
                ed.press_sequentially("**加粗** *斜体* ~~删除~~ `code` ")
                expect(ed.locator("strong")).to_have_text("加粗")
                expect(ed.locator("em")).to_have_text("斜体")
                expect(ed.locator("s")).to_have_text("删除")
                for text in ("**加粗**", "*斜体*", "~~删除~~", "`code`"):
                    assert text in source().input_value()
            test("inline-preview-source", inline)

            fixture = ("## 标题\n\n- [ ] 待办\n- [x] 完成\n\n> 引用\n\n"
                       "| 名称 | 值 |\n| --- | --- |\n| A | B |\n\n"
                       "```text\n保留 - 与 #\n```\n\n[链接](https://example.com)\n\n"
                       "脚注[^1]\n\n[^1]: 注释\n\n---\n\n正文")
            def roundtrip():
                ed = load(fixture)
                expect(ed.locator("table")).to_have_count(1)
                first = source().input_value()
                for _ in range(4):
                    preview()
                    value=source().input_value()
                    assert value == first, repr((first, value))
                src = source()
                src.press("Control+End")
                src.press("Enter")
                src.press_sequentially("源码修改")
                expect(preview()).to_contain_text("源码修改")
                saved = save()
                page.reload()
                expect(page.get_by_role("textbox", name="助手设定内容", exact=True)).to_contain_text("源码修改")
                value=source().input_value()
                assert value == saved, repr((saved, value))
            test("gfm-modes-save-reload", roundtrip)

            def setext():
                ed = load("显式下划线标题\n---\n\n正文")
                expect(ed.locator("h2")).to_have_count(1)
                assert "显式下划线标题" in source().input_value()
            test("preserve-imported-setext", setext)

            def undo():
                ed = load("原文")
                ed.press_sequentially("新增")
                page.wait_for_timeout(250)
                ed.press("Control+z")
                expect(ed).to_have_text("原文")
                ed.press("Control+Shift+z")
                expect(ed).to_have_text("原文新增")
            test("undo-redo-preview", undo)

            def undo_source():
                load("原文")
                src=source()
                src.press("Control+End")
                src.press_sequentially("新增")
                page.wait_for_timeout(250)
                src.press("Control+z")
                expect(src).to_have_value("原文")
                src.press("Control+y")
                expect(src).to_have_value("原文新增")
            test("undo-redo-source", undo_source)

            def clean_modes():
                load("原文")
                for _ in range(3):
                    assert source().input_value() == "原文"
                    preview()
                page.get_by_role("button", name="微信管理",exact=True).click()
                expect(page.get_by_role("dialog")).to_have_count(0)
                assert not writes
            test("mode-viewing-stays-clean", clean_modes)

            def immediate_mode_undo():
                ed=load("原文")
                ed.press_sequentially("甲")
                src=source()
                src.press("Control+End")
                src.press_sequentially("乙")
                page.wait_for_timeout(250)
                src.press("Control+z")
                expect(src).to_have_value("原文甲")
                ed=preview()
                assert "乙" not in ed.inner_text()
            test("fast-mode-switch-undo", immediate_mode_undo)

            def keyboard_modes():
                ed=load("原文")
                ed.press("Control+Alt+9")
                expect(page.get_by_role("textbox", name="助手设定 Markdown 源码",exact=True)).to_have_value("原文")
                source().press("Control+Alt+8")
                expect(page.get_by_role("textbox", name="助手设定内容",exact=True)).to_be_visible()
                page.get_by_role("button", name="微信管理",exact=True).click()
                expect(page.get_by_role("dialog")).to_have_count(0)
            test("keyboard-mode-parity", keyboard_modes)

            def paste():
                ed=load("")
                ed.evaluate("""el => {
                  const data = new DataTransfer();
                  data.setData('text/plain', '## 粘贴标题\\n\\n- 条目\\n\\n正文');
                  data.setData('text/html', '<img src=x onerror=alert(1)>');
                  el.dispatchEvent(new ClipboardEvent('paste', {clipboardData:data, bubbles:true, cancelable:true}));
                }""")
                expect(ed.locator("h2")).to_have_count(1)
                expect(ed.locator("li")).to_have_count(1)
                expect(ed.locator("img")).to_have_count(0)
                assert "粘贴标题" in save()
            test("plain-markdown-paste", paste)

            def ime(source_mode=False):
                load("原文")
                ed=source() if source_mode else preview()
                ed.press("Control+End")
                ed.evaluate("el => el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}))")
                page.keyboard.insert_text("中文")
                page.wait_for_timeout(1700)
                assert not draft_writes and not writes
                ed.evaluate("el => el.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true,data:'中文'}))")
                assert save().count("中文")==1
            test("composition-preview",ime)
            test("composition-source",lambda:ime(True))

            def leave():
                ed=load("原设定")
                ed.press_sequentially("修改")
                page.get_by_role("button",name="微信管理",exact=True).click()
                expect(page.get_by_role("heading",name="微信管理",exact=True)).to_be_visible()
                assert account["prompt"]=="原设定修改" and len(writes)==1
                expect(page.get_by_role("dialog")).to_have_count(0)
            test("leave-auto-apply",leave)

            def source_newlines():
                load("原文")
                src=source()
                src.fill("保留空行\n\n\n")
                assert save() == "保留空行\n\n\n"
            test("source-trailing-newlines-save", source_newlines)

            def limit():
                load("")
                src=source()
                src.fill("字" * 50001)
                expect(page.get_by_role("alert")).to_contain_text("50000")
                assert not writes
            test("character-limit", limit)

            def literal_html():
                ed=load('<script>alert(1)</script>\n\n<iframe src="https://example.com"></iframe>\n\n[危险](javascript:alert(1))')
                expect(ed.locator("script,iframe,object,embed")).to_have_count(0)
                expect(ed).to_contain_text("<script>")
                assert "<script>alert(1)</script>" in source().input_value()
            test("html-literal-no-execution", literal_html)

            def responsive(width):
                load(fixture, width)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                page.get_by_role("textbox",name="助手设定内容",exact=True).press("Control+/")
                expect(page.get_by_role("textbox",name="助手设定 Markdown 源码",exact=True)).to_be_visible()
                dimensions=page.evaluate("""() => ({source:document.querySelector('.vditor-sv').clientHeight,card:document.querySelector('.prompt-composer').clientHeight})""")
                assert dimensions["source"] > 0 and dimensions["source"] >= dimensions["card"] * 0.65, dimensions
                assert dimensions["source"] <= dimensions["card"] + 1, dimensions
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                page.screenshot(path=str(args.output/f"editor-{width}.png"),full_page=True)
            for width in (1920, 1366, 390, 320):
                test(f"responsive-{width}", lambda w=width: responsive(w))

            def iphone():
                load("原文")
                touch=browser.new_page(viewport={"width":390,"height":844},is_mobile=True,has_touch=True,
                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1")
                touch.route("**/wechat/api/v1/**",route)
                touch.add_init_script("sessionStorage.setItem('wechat-page','automation')")
                try:
                    touch.goto(origin+"/wechat/")
                    expect(touch.get_by_role("textbox",name="助手设定内容",exact=True)).to_have_attribute("contenteditable","true")
                    touch.get_by_role("textbox",name="助手设定内容",exact=True).press("Control+/")
                    expect(touch.get_by_role("textbox",name="助手设定 Markdown 源码",exact=True)).to_have_value("原文")
                    touch.get_by_role("textbox",name="助手设定 Markdown 源码",exact=True).press("Control+/")
                    expect(touch.get_by_role("textbox",name="助手设定内容",exact=True)).to_contain_text("原文")
                finally:
                    touch.close()
            test("iphone-touch-mode-switch",iphone)

            def minimal():
                load("正文")
                expect(page.locator("#assistant-panel-prompt .vditor-toolbar")).not_to_be_visible()
                expect(page.get_by_role("button",name="保存设定",exact=True)).to_have_count(0)
                expect(page.get_by_role("button",name="放弃修改",exact=True)).to_have_count(0)
                assert not page.locator(".prompt-save-bar,.prompt-leave-dialog").count()
            test("minimal-no-toolbar-or-save-buttons",minimal)

            def autosave():
                ed=load("原文")
                ed.press_sequentially("一")
                page.wait_for_timeout(500)
                ed.press_sequentially("二")
                page.wait_for_timeout(500)
                ed.press_sequentially("三")
                until(lambda:len(draft_writes)==1)
                assert stored["markdown"]=="原文一二三" and account["prompt"]=="原文"
                assert not writes
                expect(ed).to_have_attribute("contenteditable","true")
                assert save()=="原文一二三"
            test("autosave-debounce-draft-only-until-blur",autosave)

            def restored_draft():
                ed=load("生效正文")
                ed.press_sequentially("仅草稿修改")
                until(lambda:len(draft_writes)==1)
                page.reload()
                ed=page.get_by_role("textbox",name="助手设定内容",exact=True)
                expect(ed).to_have_attribute("contenteditable","true")
                expect(ed).to_have_text("生效正文仅草稿修改")
                assert account["prompt"]=="生效正文" and not writes
                ed.focus()
                assert save()=="生效正文仅草稿修改"
            test("server-draft-refresh-does-not-activate-on-load",restored_draft)

            def slow_save():
                ed=load("原文")
                controls["holdSave"]=True
                ed.press_sequentially("第一稿")
                until(lambda:len(held)==1)
                expect(ed).to_have_attribute("contenteditable","true")
                ed.press_sequentially("继续连续输入")
                expected=ed.inner_text()
                r,response=held.pop()
                r.fulfill(status=200,json=response)
                expect(ed).to_have_text(expected)
                assert save()==expected and len(writes)==1
                assert len(draft_writes)==2
            test("slow-save-continuous-input-not-overwritten",slow_save)

            def slow_save_ime():
                ed=load("原文")
                controls["holdSave"]=True
                ed.press_sequentially("已确认")
                until(lambda:len(held)==1)
                ed.evaluate("el=>el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}))")
                page.keyboard.insert_text("中文")
                r,response=held.pop()
                r.fulfill(status=200,json=response)
                expect(ed).to_contain_text("中文")
                page.wait_for_timeout(1700)
                assert len(draft_writes)==1 and not writes
                ed.evaluate("el=>el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'中文'}))")
                assert save()=="原文已确认中文"
            test("inflight-ack-does-not-disrupt-composition",slow_save_ime)

            def blur_composing():
                ed=load("原文")
                ed.evaluate("el=>el.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}))")
                page.keyboard.insert_text("中文")
                page.get_by_role("heading",name="回复范围",exact=True).click()
                page.wait_for_timeout(300)
                assert not draft_writes and not writes
                ed.evaluate("el=>el.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'中文'}))")
                until(lambda:len(writes)==1)
                assert account["prompt"]=="原文中文"
            test("blur-waits-for-composition-commit",blur_composing)

            def slow_apply():
                ed=load("原文")
                controls["holdApply"]=True
                ed.press_sequentially("第一轮")
                page.get_by_role("heading",name="回复范围",exact=True).click()
                until(lambda:len(held)==1)
                ed.focus()
                ed.press("Control+End")
                ed.press_sequentially("第二轮")
                expect(ed).to_have_text("原文第一轮第二轮")
                r,response=held.pop()
                r.fulfill(status=200,json=response)
                expect(ed).to_have_text("原文第一轮第二轮")
                assert save()=="原文第一轮第二轮"
                assert len(writes)==2
            test("apply-inflight-new-input-retained",slow_apply)

            def offline_refresh():
                load("原文")
                controls["failSave"]=True
                src=source()
                src.fill("断网期间的完整草稿")
                expect(page.get_by_role("alert")).to_contain_text("内容已保留",timeout=7000)
                assert not writes and account["prompt"]=="原文"
                page.reload()
                ed=page.get_by_role("textbox",name="助手设定内容",exact=True)
                expect(ed).to_have_attribute("contenteditable","true")
                expect(ed).to_contain_text("断网期间的完整草稿")
                del controls["failSave"]
                ed.focus()
                assert save()=="断网期间的完整草稿"
            test("network-failure-refresh-recovers-local-tail",offline_refresh)

            def lost_ack():
                ed=load("原文")
                controls.update(lostSaveAck=True,lostApplyAck=True)
                ed.press_sequentially("完整修改")
                assert save()=="原文完整修改"
                expect(page.get_by_role("alert")).to_have_count(0,timeout=8000)
                assert len(writes)==1
            test("lost-acks-readback-no-duplicate-apply",lost_ack)

            def conflict(keep):
                ed=load("原文")
                ed.press_sequentially("本页草稿")
                stored.update(markdown="另一处草稿",version=uuid.uuid4().hex,base_sha256=fingerprint("原文"))
                expect(page.get_by_role("alert")).to_contain_text("另一处",timeout=7000)
                expect(ed).to_contain_text("本页草稿")
                assert account["prompt"]=="原文" and not writes
                page.get_by_role("button",name="处理设定冲突",exact=True).click()
                if keep:
                    page.get_by_role("menuitem",name="保留本页内容",exact=True).click()
                    until(lambda:len(writes)==1)
                    assert account["prompt"]=="原文本页草稿"
                else:
                    page.get_by_role("menuitem",name="采用生效设定",exact=True).click()
                    expect(ed).to_have_text("原文")
                    assert not writes
            test("conflict-retain-local-explicitly",lambda:conflict(True))
            test("conflict-adopt-active-explicitly",lambda:conflict(False))

            def user_scope():
                ed=load("用户一")
                controls["failSave"]=True
                ed.press_sequentially("本机备份")
                expect(page.get_by_role("alert")).to_contain_text("保留",timeout=7000)
                controls["userId"]=2
                account["prompt"]="用户二的设定"
                stored.clear()
                page.reload()
                ed=page.get_by_role("textbox",name="助手设定内容",exact=True)
                expect(ed).to_have_attribute("contenteditable","true")
                expect(ed).to_have_text("用户二的设定")
                assert not writes
            test("browser-recovery-isolated-by-user",user_scope)
        browser.close()
    server.shutdown()
    report={"passed":sum(row["passed"] for row in results), "total":len(results), "cases":results,
            "environment":"server isolated Chromium, memory API fixtures", "time":time.time()}
    (args.output/"results.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))
    raise SystemExit(0 if all(row["passed"] for row in results) else 1)


if __name__ == "__main__":
    main()
