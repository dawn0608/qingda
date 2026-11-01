"""服务器隔离浏览器：双面板布局、输入保护及退役功能无请求。"""

import argparse
import http.server
import json
import threading
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

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reads, writes, checks, errors = [], [], [], []
    state = dict(model=dict(model="deepseek-flash", thinking_enabled=False, web_search_enabled=True),
                 model_revision=1, model_key_configured=True,
                 asr=dict(provider="aliyun", model="qwen-audio-3.1-asr-flash", key_configured=True))
    callback = "https://example.com/wechatapi/callback/" + "a" * 48

    def check(name, passed):
        checks.append(dict(name=name, passed=bool(passed)))
        assert passed, name

    def route(r):
        path = urlsplit(r.request.url).path.split("/api/v1")[-1]
        reads.append(path)
        if r.request.method != "GET":
            body = r.request.post_data_json or {}
            writes.append(dict(path=path, body=body))
            if path == "/settings":
                state["model"].update(body.get("model", {}))
                state["model_revision"] += 1
                r.fulfill(json=state)
                return
            r.fulfill(status=400, json=dict(error="unexpected_fixture_write"))
            return
        result = {
            "/auth/session": dict(username="隔离验收", user_id=1, csrf="fixture"),
            "/accounts": [dict(id=1, nickname="隔离微信", enabled=True)],
            "/settings": state,
            "/wechat-config": dict(token="fixture-token", state="configured", revision="fixture", callback_url=callback),
        }.get(path, {})
        r.fulfill(json=result)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/wechat/api/v1/**", route)
            page.add_init_script("sessionStorage.setItem('wechat-page','settings')")
            page.goto(f"http://127.0.0.1:{server.server_port}/wechat/")
            token = page.get_by_role("textbox", name="接入 Token", exact=True)
            expect(token).to_have_value("fixture-token")
            model = page.get_by_label("DeepSeek API Key", exact=True)
            speech = page.get_by_label("阿里云百炼 API Key", exact=True)
            for width, height in ((1864, 893), (1440, 640), (1280, 720), (1024, 768), (390, 844), (320, 568)):
                page.set_viewport_size(dict(width=width, height=height))
                page.locator(".settings-page").evaluate("e=>e.scrollTop=0")
                page.wait_for_timeout(250)
                geometry = page.locator(".settings-grid").evaluate("""e=>{
                  const panels=[...e.children], [a,b]=panels.map(x=>x.getBoundingClientRect());
                  return {count:panels.length,equal:Math.abs(a.width-b.width)<1,
                    aligned:Math.abs(a.top-b.top)<1,stacked:b.top>=a.bottom+15,
                    overflow:document.documentElement.scrollWidth>innerWidth,
                    clipped:panels.some(p=>[...p.querySelectorAll('input,button')].some(x=>{
                      const r=x.getBoundingClientRect(),b=p.getBoundingClientRect();
                      return r.width && (r.left<b.left-1||r.right>b.right+1||r.bottom>b.bottom+1);
                    }))};} """)
                check(f"{width}两面板无溢出或裁切", geometry["count"] == 2 and not geometry["overflow"] and not geometry["clipped"])
                check(f"{width}上下两面板等宽", geometry["equal"] and geometry["stacked"])
                check(f"{width}回调只读单行", page.locator(".callback-copy").evaluate("e=>e.tagName==='INPUT'&&e.readOnly&&e.getBoundingClientRect().height===36"))
                page.get_by_role("button", name="保存配置", exact=True).scroll_into_view_if_needed()
                expect(page.get_by_role("button", name="保存配置", exact=True)).to_be_in_viewport(ratio=0.98)
                check(f"{width}短窗口可滚动到天气保存按钮", True)
                page.screenshot(path=str(args.output / f"settings-{width}.png"))
            page.set_viewport_size(dict(width=1864, height=893))
            token.fill("unsaved-token")
            model.fill("unsaved-model")
            speech.fill("unsaved-speech")
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.wait_for_timeout(600)
            check("自动读取保留三组草稿", (token.input_value(), model.input_value(), speech.input_value()) == ("unsaved-token", "unsaved-model", "unsaved-speech"))
            page.get_by_role("button", name="保存 DeepSeek 密钥", exact=True).click()
            expect(model).to_have_value("")
            check("密钥独立保存", writes[-1]["body"] == dict(model_key="unsaved-model") and speech.input_value() == "unsaved-speech" and token.input_value() == "unsaved-token")
            page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(300)
            count = len(reads)
            page.wait_for_timeout(5500)
            check("隐藏页面暂停读取", len(reads) == count)
            page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(600)
            check("返回页面恢复读取", len(reads) > count)
            check("没有桥接请求", not any("bridge" in path for path in reads))
            check("仅三个导航入口", page.locator(".sidebar nav button").count() == 3)
            check("没有脚本异常", not errors)
            browser.close()
    finally:
        server.shutdown()
        result = dict(passed=sum(c["passed"] for c in checks), total=len(checks), checks=checks, errors=errors)
        (args.output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(dict(passed=result["passed"], total=result["total"], errors=errors)))


if __name__ == "__main__":
    main()
