"""服务器候选构建验收：单表单及状态高度；接口替身不证明真实收码。"""

import argparse
import http.server
import json
import pathlib
import threading

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            target = (args.dist / path.split("?")[0].removeprefix("/wechat/")).resolve()
            return str(
                target
                if target.is_relative_to(args.dist.resolve()) and target.is_file()
                else args.dist / "index.html"
            )

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        for width, height in [(1440, 900), (1125, 505), (959, 800), (390, 844), (320, 740), (390, 500)]:
            context = browser.new_context(viewport={"width": width, "height": height})
            page = context.new_page()
            page.set_default_timeout(7000)
            writes, pending, errors = [], [], []
            options = dict(sms_available=True, email_available=True, registration_enabled=True)

            def route(request):
                path = request.request.url.split("/api/v1")[-1]
                if path == "/auth/session":
                    request.fulfill(status=401, json={"detail": "请登录管理台"})
                elif path == "/auth/options":
                    request.fulfill(json=options)
                elif path in ("/auth/code/send", "/auth/login"):
                    writes.append((path, request.request.post_data_json))
                    pending.append(request)
                else:
                    request.fulfill(json={})

            def geometry():
                return page.locator(".login-layout").evaluate(
                    "e=>{const r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height]}"
                )

            def same(before):
                after = geometry()
                assert max(abs(a - b) for a, b in zip(before, after)) <= 1, (before, after)
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")

            page.route("**/wechat/api/v1/**", route)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/wechat/")
            try:
                expect(page.get_by_role("heading", name="登录青答", exact=True)).to_be_visible()
            except AssertionError:
                page.screenshot(path=str(args.output / "load-failure.png"), full_page=True)
                print(json.dumps({"errors": errors, "body": page.locator("body").inner_text()}))
                raise
            expect(page.get_by_text("未注册账号验证后自动注册", exact=True)).to_be_visible()
            assert page.locator("form").count() == 1 and page.locator(".auth-tabs").count() == 0
            assert page.get_by_label("用户名", exact=True).count() == 0
            before = geometry()
            if width >= 960:
                assert abs(before[3] - 452) <= 1
            page.get_by_label("邮箱或手机号", exact=True).fill("fixture@example.invalid")
            page.get_by_role("button", name="获取验证码", exact=True).click()
            expect(page.get_by_role("button", name="发送中", exact=True)).to_be_visible()
            expect(page.locator("form button.primary")).to_have_text("登录")
            same(before)
            assert writes[-1] == (
                "/auth/code/send",
                {"channel": "email", "identifier": "fixture@example.invalid", "purpose": "login"},
            )
            pending.pop().fulfill(
                json={"challenge_id": "fixture-challenge", "retry_after": 60, "expires_in": 300}
            )
            expect(page.locator(".toast")).to_contain_text("验证码已发送")
            same(before)
            page.get_by_label("验证码", exact=True).fill("123456")
            page.locator("form button.primary").click()
            expect(page.locator("form button.primary")).to_have_text("登录中")
            same(before)
            assert writes[-1][0] == "/auth/login"
            assert set(writes[-1][1]) == {"channel", "identifier", "method", "challenge_id", "code"}
            pending.pop().fulfill(status=400, json={"detail": "验证码不正确或已失效，请检查后重试"})
            expect(page.locator(".toast")).to_have_attribute("role", "alert")
            expect(page.locator("form button.primary")).to_have_text("登录")
            same(before)
            options["registration_enabled"] = False
            page.reload()
            expect(page.get_by_text("当前仅支持已有账号登录", exact=True)).to_be_visible()
            same(before)
            options["email_available"] = False
            page.reload()
            page.get_by_label("邮箱或手机号", exact=True).fill("fixture@example.invalid")
            expect(page.locator("form button.primary")).to_have_text("邮箱验证码暂不可用")
            same(before)
            assert not errors, errors
            page.screenshot(path=str(args.output / f"login-{width}x{height}.png"), full_page=True)
            results.append(
                dict(
                    width=width,
                    height=height,
                    card=before,
                    states=[
                        "idle",
                        "sending",
                        "countdown",
                        "submitting",
                        "error",
                        "registration-disabled",
                        "channel-unavailable",
                    ],
                    passed=True,
                )
            )
            context.close()
        browser.close()
    server.shutdown()
    (args.output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            dict(viewports=len(results), state_checks=sum(len(r["states"]) for r in results), passed=True)
        )
    )


if __name__ == "__main__":
    main()
