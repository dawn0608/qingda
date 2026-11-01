"""服务器隔离浏览器：固定模型开关及逐用户天气配置表单。"""

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
    state = dict(model_revision=1, model_key_configured=True, asr=dict(model="qwen-audio-3.1-asr-flash", key_configured=True), weather=dict(api_host="saved.qweatherapi.com", key_configured=True, key_preview="********"))
    writes, checks, held, errors = [], [], [], []
    flags = dict(hold=False, fail=False)

    def check(name, passed):
        checks.append(dict(name=name, passed=bool(passed)))
        assert passed, name

    def finish(route, body):
        if "weather" in body:
            state["weather"]["api_host"] = body["weather"]["api_host"].strip().lower()
        route.fulfill(json=state)

    def route(r):
        path = urlsplit(r.request.url).path.split("/api/v1")[-1]
        if r.request.method != "GET":
            body = r.request.post_data_json
            writes.append(body)
            if flags["fail"]:
                r.fulfill(status=400, json=dict(error="天气配置保存失败"))
            elif flags["hold"]:
                held.append((r, body))
            else:
                finish(r, body)
            return
        r.fulfill(json={"/auth/session":dict(username="隔离验收",user_id=1,csrf="fixture"),"/accounts":[dict(id=1,nickname="隔离微信",enabled=True)],"/settings":state,"/wechat-config":dict(token="fixture-token",state="configured",revision="fixture",callback_url="https://example.com/callback")}.get(path,{}))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/wechat/api/v1/**", route)
            page.add_init_script("sessionStorage.setItem('wechat-page','settings')")
            page.goto(f"http://127.0.0.1:{server.server_port}/wechat/")
            host = page.get_by_label("和风天气 API Host", exact=True)
            weather = page.get_by_label("和风天气 API Key", exact=True)
            model = page.get_by_label("DeepSeek API Key", exact=True)
            speech = page.get_by_label("阿里云百炼 API Key", exact=True)
            token = page.get_by_label("接入 Token", exact=True)
            expect(host).to_have_value("saved.qweatherapi.com")
            check("两个开关已移除", page.get_by_role("switch", name="思考模式", exact=True).count()==0 and page.get_by_role("switch", name="联网搜索", exact=True).count()==0)
            check("天气密钥为密码框", weather.get_attribute("type")=="password")
            for width,height in ((1864,893),(1280,720),(1024,768),(390,844),(320,568)):
                page.set_viewport_size(dict(width=width,height=height))
                page.get_by_role("button",name="保存配置",exact=True).scroll_into_view_if_needed()
                box=page.locator(".settings-grid").evaluate("""e=>{const [a,b]=[...e.children].map(x=>x.getBoundingClientRect());return {count:e.children.length,overflow:document.documentElement.scrollWidth>innerWidth,equal:Math.abs(a.width-b.width)<1,aligned:Math.abs(a.top-b.top)<1,stacked:b.top>=a.bottom+15,clipped:[...e.querySelectorAll('input,button')].some(x=>{const r=x.getBoundingClientRect(),b=x.closest('.panel').getBoundingClientRect();return r.width&&(r.left<b.left-1||r.right>b.right+1)})}}""")
                check(f"{width}布局无横向溢出",box["count"]==2 and not box["overflow"] and not box["clipped"])
                check(f"{width}上下两面板等宽",box["equal"] and box["stacked"])
                columns=page.locator('.model-provider-section').evaluate_all("els=>els.map(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,b:r.bottom}})")
                panel_width=page.locator('.model-settings').evaluate('e=>e.clientWidth')
                check(f"{width}三组设置响应式排列", all(abs(c['y']-columns[0]['y'])<1 and abs(c['w']-columns[0]['w'])<1 for c in columns) if panel_width>=980 else all(columns[i]['y']>=columns[i-1]['b'] for i in (1,2)))
                page.screenshot(path=str(args.output/f"settings-{width}.png"))
            page.set_viewport_size(dict(width=1440,height=900))
            token.fill("draft-token");model.fill("draft-model");speech.fill("draft-speech");host.fill("draft.qweatherapi.com");weather.fill("draft-weather")
            urls=["https://platform.deepseek.com/api_keys","https://bailian.console.aliyun.com/cn-beijing/model/settings/api-key","https://console.qweather.com/project"]
            links=page.locator('.model-key-link')
            check("三个官方密钥入口及安全新标签页", links.count()==3 and all(links.nth(i).get_attribute('href')==url and links.nth(i).get_attribute('target')=='_blank' and set(links.nth(i).get_attribute('rel').split())>={'noopener','noreferrer'} for i,url in enumerate(urls)))
            before=len(writes)
            page.context.route('https://platform.deepseek.com/**',lambda r:r.fulfill(body='<title>官方入口隔离验证</title>'))
            with page.expect_popup() as opened:
                links.nth(0).click()
            popup=opened.value
            popup.wait_for_load_state()
            check("获取密钥打开新页且不提交表单", popup.url==urls[0] and len(writes)==before)
            popup.close()
            check("外链操作保留全部草稿",(token.input_value(),model.input_value(),speech.input_value(),host.input_value(),weather.input_value())==("draft-token","draft-model","draft-speech","draft.qweatherapi.com","draft-weather"))
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.wait_for_timeout(300)
            check("自动读取保留全部草稿",(token.input_value(),model.input_value(),speech.input_value(),host.input_value(),weather.input_value())==("draft-token","draft-model","draft-speech","draft.qweatherapi.com","draft-weather"))
            page.get_by_role("button",name="保存 DeepSeek 密钥",exact=True).click()
            expect(model).to_have_value("")
            check("模型独立保存保留天气草稿",writes[-1]==dict(model_key="draft-model") and host.input_value()=="draft.qweatherapi.com" and weather.input_value()=="draft-weather" and speech.input_value()=="draft-speech")
            flags["hold"]=True
            before=len(writes)
            page.get_by_role("form",name="天气配置",exact=True).evaluate("e=>{e.requestSubmit();e.requestSubmit()}")
            expect(page.get_by_role("button",name="保存中",exact=True)).to_have_attribute("aria-busy","true")
            check("天气保存同步防重复",len(writes)==before+1 and len(held)==1)
            check("保存期间天气输入只读",host.get_attribute("readonly") is not None and weather.get_attribute("readonly") is not None)
            r,body=held.pop();flags["hold"]=False;finish(r,body)
            expect(weather).to_have_value("")
            check("天气仅提交自身且保留其他草稿",writes[-1]==dict(weather=dict(api_host="draft.qweatherapi.com",api_key="draft-weather")) and speech.input_value()=="draft-speech" and token.input_value()=="draft-token")
            host.fill("next.qweatherapi.com");weather.press("Enter")
            expect(host).to_have_value("next.qweatherapi.com")
            check("回车提交且空密钥保留",writes[-1]==dict(weather=dict(api_host="next.qweatherapi.com",api_key="")))
            flags["fail"]=True;host.fill("failed.qweatherapi.com");weather.fill("retain-after-failure")
            page.get_by_role("button",name="保存配置",exact=True).click()
            expect(page.get_by_text("请检查填写内容",exact=True)).to_be_visible()
            check("失败保留天气草稿",host.input_value()=="failed.qweatherapi.com" and weather.input_value()=="retain-after-failure")
            check("无浏览器脚本异常",not errors)
            browser.close()
    finally:
        server.shutdown()
        result=dict(passed=sum(c["passed"] for c in checks),total=len(checks),checks=checks,errors=errors)
        (args.output/"results.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(dict(passed=result["passed"],total=result["total"])))


if __name__ == "__main__":
    main()
