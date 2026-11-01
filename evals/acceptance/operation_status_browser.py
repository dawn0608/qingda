"""服务器隔离浏览器验证操作状态；仅内存 API，不调用供应商或生产写接口。"""

import argparse
import base64
import hashlib
import http.server
import json
from pathlib import Path
import threading
import time
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
            return str(target if target.is_relative_to(args.dist.resolve()) and target.is_file() else args.dist / "index.html")

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    checks, errors = [], []

    def check(name, passed):
        checks.append(dict(name=name, passed=bool(passed)))
        (args.output / "results.json").write_text(json.dumps(dict(checks=checks, errors=errors), ensure_ascii=False, indent=2))
        assert passed, name

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            for width, height in [(1440, 900), (390, 844)]:
                context = browser.new_context(viewport=dict(width=width, height=height))
                page = context.new_page()
                page.set_default_timeout(7000)
                page.on("pageerror", lambda e: errors.append(str(e)))
                user = dict(user_id=1, username="隔离验收", csrf="fixture", phone=None, email="old@example.invalid", sms_available=True, email_available=True)
                settings = dict(model_revision=1, model_key_configured=True, model_key_preview="fixture", asr=dict(key_configured=True, model="qwen-audio-3.1-asr-flash"), weather=dict(api_host="fixture.qweatherapi.com", key_configured=True))
                config = dict(token="fixture", revision="1", state="configured", callback_url="https://example.invalid/callback")
                prompt = "## 助手设定\n\n隔离内容。"
                sha = hashlib.sha256(prompt.encode()).hexdigest()
                account = dict(id=1, nickname="隔离微信", enabled=True, online=1, ai_enabled=True, prompt=prompt)
                rows = [dict(id=i, display_name=f"隔离好友{i}", kind="private", allowed=True, removed_at=None, profile={}, auto_reply_status="enabled", pause_until=time.time()+600) for i in (1, 2)]
                held, writes = [], []
                flags = dict(draft_fail=False, draft_hold=False, conflict=False, hold_history=False)
                receipt = dict(version=None, markdown=prompt, base_sha256=sha, active_prompt=prompt, active_sha256=sha, dirty=False, conflict=False)

                def route(r):
                    path = urlsplit(r.request.url).path.split("/api/v1")[-1]
                    if r.request.method != "GET":
                        writes.append(path)
                        held.append((path, r))
                    elif path.endswith("/prompt-draft"):
                        if flags["draft_hold"]:
                            held.append((path, r))
                        elif flags["draft_fail"]:
                            r.fulfill(status=503, json=dict(error="服务暂时繁忙，请稍后重试"))
                        else:
                            r.fulfill(json={**receipt, "conflict": flags["conflict"]})
                    elif path.endswith("/reply-scope"):
                        r.fulfill(json=dict(items=rows, total=len(rows), page=1, page_size=10, server_time=time.time()))
                    elif path.endswith("/history"):
                        if flags["hold_history"]:
                            held.append((path, r))
                        else:
                            r.fulfill(json=dict(items=[], has_more=False, cursor=None, watermark="0"))
                    else:
                        r.fulfill(json={"/auth/session":user, "/accounts":[account], "/settings":settings, "/wechat-config":config}.get(path, {}))

                def release(status=400, body=None):
                    path, request = held.pop(0)
                    request.fulfill(status=status, json=body if body is not None else dict(error="请检查填写内容"))
                    return path

                def box(locator):
                    return locator.evaluate("e=>{const r=e.getBoundingClientRect();return [r.width,r.height]}")

                def stable(locator, before, name):
                    after = box(locator)
                    check(f"{width} {name}尺寸稳定", max(abs(a-b) for a,b in zip(before,after)) <= 1)

                page.route("**/wechat/api/v1/**", route)
                page.add_init_script("if(!sessionStorage.getItem('wechat-page'))sessionStorage.setItem('wechat-page','settings')")
                page.goto(f"http://127.0.0.1:{server.server_port}/wechat/")
                for provider, label in [("DeepSeek 密钥设置", "DeepSeek API Key"), ("语音密钥设置", "阿里云百炼 API Key"), ("天气配置", "和风天气 API Key")]:
                    form = page.get_by_role("form", name=provider, exact=True)
                    page.get_by_label(label, exact=True).fill("retain-fixture")
                    button = form.locator("button.primary")
                    before, count = box(button), len(writes)
                    form.evaluate("e=>{e.requestSubmit();e.requestSubmit()}")
                    expect(button).to_have_text("保存中")
                    expect(button).to_have_attribute("aria-busy", "true")
                    check(f"{width} {provider}仅提交一次", len(writes)==count+1)
                    check(f"{width} 仅当前分区显示保存中", page.locator(".model-settings button").evaluate_all("els=>els.filter(e=>e.textContent.trim()==='保存中').length")==1)
                    stable(button,before,provider)
                    release()
                    expect(button).not_to_have_text("保存中")
                    check(f"{width} {provider}失败保留草稿", page.get_by_label(label, exact=True).input_value()=="retain-fixture")
                token_button=page.locator(".wechat-config-actions button")
                for initial, pending in [("configured","绑定中"),("uncertain","确认中")]:
                    config["state"]=initial
                    if initial=="uncertain":
                        page.reload()
                        expect(token_button).to_have_text("重新确认绑定")
                    token_button.click()
                    expect(token_button).to_have_text(pending)
                    release()
                    expect(token_button).not_to_have_text(pending)
                    check(f"{width} 微信{initial}操作状态", True)

                page.locator(".account-trigger").click()
                page.get_by_role("button", name="账户设置", exact=True).click()
                dialog=page.get_by_role("dialog",name="账户设置",exact=True)
                page.get_by_role("button", name="修改昵称",exact=True).click()
                page.get_by_label("昵称",exact=True).fill("新昵称")
                profile=page.locator(".account-profile-form")
                profile.evaluate("e=>{e.requestSubmit();e.requestSubmit()}")
                expect(profile.locator("button")).to_have_text("保存中")
                expect(page.get_by_role("button",name="关闭账户设置",exact=True)).to_be_disabled()
                check(f"{width} 昵称同步防重复",len(held)==1)
                release()
                expect(profile.locator("button")).to_have_text("保存昵称")
                profile.locator("button").click()
                expect(profile.locator("button")).to_have_text("保存中")
                user["username"]="新昵称"
                release(200,user)
                expect(profile.locator("button")).to_have_text("保存昵称")
                check(f"{width} 昵称成功恢复",True)
                png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aE0QAAAAASUVORK5CYII=")
                page.locator(".profile-avatar-input").set_input_files(dict(name="fixture.png",mimeType="image/png",buffer=png))
                expect(page.get_by_role("button",name="头像更新中",exact=True)).to_have_attribute("title","头像更新中")
                release()
                expect(page.get_by_role("button",name="更换头像",exact=True)).to_be_enabled()
                check(f"{width} 头像状态失败恢复",True)

                for kind, bound in [("手机号",False),("邮箱",True)]:
                    page.get_by_role("button",name=f"{'更换' if bound else '绑定'}{kind}",exact=True).click()
                    editor=page.locator(".account-contact-edit:visible")
                    editor.get_by_label(f"{'新' if bound else ''}{kind}",exact=True).fill("new@example.invalid" if bound else "13800000000")
                    send=editor.locator(".auth-code-row > button")
                    confirm=editor.locator(".account-contact-actions button")
                    original=f"确认{'更换' if bound else '绑定'}"
                    send.click()
                    expect(send).to_have_text("发送中")
                    expect(confirm).to_have_text(original)
                    expect(confirm).to_be_disabled()
                    check(f"{width} {kind}发送不串确认状态",len(held)==1)
                    release(200,dict(challenge_id="fixture",retry_after=0))
                    expect(send).to_have_text("获取验证码")
                    editor.get_by_label("邮箱验证码" if bound else "短信验证码",exact=True).fill("123456")
                    editor.evaluate("e=>{e.requestSubmit();e.requestSubmit()}")
                    expect(confirm).to_have_text("更换中" if bound else "绑定中")
                    expect(send).to_have_text("获取验证码")
                    check(f"{width} {kind}提交不串发送且防重复",len(held)==1)
                    release()
                    expect(confirm).to_have_text(original)
                    check(f"{width} {kind}失败保留输入",editor.get_by_label("邮箱验证码" if bound else "短信验证码",exact=True).input_value()=="123456")
                    confirm.click()
                    expect(confirm).to_have_text("更换中" if bound else "绑定中")
                    user["email" if bound else "phone"]="new@example.invalid" if bound else "13800000000"
                    release(200,user)
                    expect(page.get_by_text(f"{kind}已{'更换' if bound else '绑定'}",exact=True)).to_be_visible()
                    check(f"{width} {kind}成功收起",page.locator(".account-contact-edit:visible").count()==0)
                page.get_by_role("button",name="关闭账户设置",exact=True).click()

                page.locator(".account-trigger").click()
                logout=page.get_by_role("button",name="退出登录",exact=True)
                logout.click()
                expect(page.get_by_role("button",name="退出中",exact=True)).to_be_disabled()
                check(f"{width} 退出等待原菜单可见",len(held)==1 and dialog.count()==0)
                release()
                expect(logout).to_be_enabled()
                logout.click()
                expect(page.get_by_role("button",name="退出中",exact=True)).to_be_visible()
                release(200,{})
                expect(page.get_by_role("heading",name="登录青答",exact=True)).to_be_visible()
                check(f"{width} 退出成功进入登录页",True)
                page.reload()

                page.get_by_role("button",name="智能助手",exact=True).click()
                switch=page.get_by_role("switch",name="启用当前账号助手",exact=True)
                before=box(page.locator(".assistant-enable"))
                switch.click()
                expect(page.get_by_role("switch",name="当前账号助手停用中",exact=True)).to_have_attribute("aria-busy","true")
                stable(page.locator(".assistant-enable"),before,"助手开关")
                release()
                expect(switch).to_be_enabled()
                switch.click()
                expect(page.get_by_role("switch",name="当前账号助手停用中",exact=True)).to_be_visible()
                account["ai_enabled"]=False
                release(200,account)
                expect(switch).to_have_attribute("aria-checked","false")
                switch.click()
                expect(page.get_by_role("switch",name="当前账号助手启用中",exact=True)).to_be_visible()
                account["ai_enabled"]=True
                release(200,account)
                expect(switch).to_have_attribute("aria-checked","true")
                check(f"{width} 助手启停成功恢复",True)
                resume=page.get_by_role("button",name="恢复 隔离好友1 的回复",exact=True)
                before=box(resume)
                resume.click()
                active=page.get_by_role("button",name="隔离好友1 的回复恢复中",exact=True)
                expect(active).to_have_text("恢复中")
                stable(active,before,"恢复回复")
                expect(page.get_by_role("button",name="恢复 隔离好友2 的回复",exact=True)).to_have_text("恢复回复")
                release()
                expect(resume).to_be_enabled()
                rows[0]["pause_until"]=0
                page.evaluate("window.dispatchEvent(new Event('focus'))")
                reply_switch=page.get_by_role("switch",name="允许 隔离好友1",exact=True)
                reply_switch.click()
                expect(page.get_by_role("switch",name="隔离好友1 的回复关闭中",exact=True)).to_have_attribute("aria-busy","true")
                release()
                expect(reply_switch).to_be_enabled()
                check(f"{width} 回复开关仅当前对象显示进行中",True)

                flags["hold_history"]=True
                page.get_by_role("button",name="查看 隔离好友1 的聊天记录",exact=True).click()
                expect(page.get_by_text("聊天记录读取中",exact=True)).to_be_visible()
                release(200,dict(items=[],has_more=False,cursor=None,watermark="0"))
                expect(page.get_by_text("暂无已保存的聊天记录",exact=True)).to_be_visible()
                page.get_by_label("搜索聊天记录",exact=True).fill("目标")
                expect(page.get_by_text("聊天记录搜索中",exact=True)).to_be_visible()
                release(200,dict(items=[],has_more=False,cursor=None,watermark="0"))
                page.get_by_role("button",name="关闭聊天记录",exact=True).click()

                flags["draft_fail"]=True
                page.reload()
                retry=page.get_by_role("button",name="草稿待重试",exact=True)
                expect(retry).to_be_visible()
                flags["draft_hold"]=True
                retry.click()
                expect(page.get_by_text("重试中",exact=True)).to_be_visible()
                flags["draft_hold"]=False;flags["draft_fail"]=False
                release(200,receipt)
                expect(page.get_by_text("重试中",exact=True)).to_have_count(0)
                flags["conflict"]=True
                page.reload()
                page.get_by_role("button",name="处理设定冲突",exact=True).click()
                flags["draft_hold"]=True
                page.get_by_role("menuitem",name="保留本页内容",exact=True).click()
                expect(page.get_by_text("解决中",exact=True)).to_be_visible()
                flags["draft_hold"]=False
                release()
                expect(page.get_by_role("button",name="处理设定冲突",exact=True)).to_be_visible()
                check(f"{width} 草稿重试与冲突失败恢复",True)
                check(f"{width} 无横向溢出",page.evaluate("document.documentElement.scrollWidth<=innerWidth"))
                page.screenshot(path=str(args.output/f"operations-{width}.png"),full_page=True)
                check(f"{width} 无悬空请求",not held)
                context.close()
            check("无浏览器脚本异常",not errors)
            browser.close()
    finally:
        server.shutdown()
        (args.output/"results.json").write_text(json.dumps(dict(passed=sum(x["passed"] for x in checks),total=len(checks),checks=checks,errors=errors),ensure_ascii=False,indent=2))
    print(json.dumps(dict(passed=len(checks),total=len(checks))))


if __name__ == "__main__":
    main()
