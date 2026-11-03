"""服务器隔离浏览器：真实构建与只读 API 夹具，覆盖聊天弹框交互。"""

import argparse
import hashlib
import http.server
import io
import json
import math
import struct
import threading
import time
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image
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
    prompt = "## 助手设定\n\n只读聊天记录验收。"
    digest = hashlib.sha256(prompt.encode()).hexdigest()
    accounts = [
        dict(
            id=i, nickname=f"隔离账号{i}", prompt=prompt, owner_profile="", enabled=1, online=1, ai_enabled=0
        )
        for i in (1, 2)
    ]
    messages = [
        dict(
            id=str(i),
            timestamp=1700000000 + i,
            sender_name="成员昵称" if i % 2 else "隔离账号1",
            direction="inbound" if i % 2 else "outbound",
            source="live" if i % 2 else "auto",
            msg_type="1",
            type_label="文本",
            content=f"保存的完整正文 {i}\n" + "消息原文可以完整复制。" * 3,
            revoked=i == 20,
        )
        for i in range(1, 136)
    ]
    messages[19]["content"] += "撤回原文唯一词"
    messages[69].update(msg_type="34", type_label="语音", content="已有语音转写 搜索目标\n3秒")
    messages[69]["voice"] = dict(
        url="/wechat/api/v1/accounts/1/conversations/10/history/70/voice",
        duration_ms=3000,
        transcript="已有语音转写 搜索目标",
    )
    for index in (129, 130):
        messages[index].update(
            msg_type="34",
            type_label="语音",
            content="4秒",
            voice=dict(
                url=f"/wechat/api/v1/accounts/1/conversations/10/history/{index + 1}/voice",
                duration_ms=4000,
                transcript="",
            ),
        )
    messages[1]["content"] += "搜索目标100%_!"
    messages[133]["content"] = "图文[微笑][Smile]/::)[未知表情]\n原文保留"
    messages[134].update(
        msg_type="3",
        type_label="图片",
        content="",
        image=dict(url="/wechat/api/v1/accounts/1/conversations/10/history/135/image", width=640, height=360),
    )
    messages[132].update(msg_type="3", type_label="图片", content="", image=None)
    messages[131].update(msg_type="47", type_label="表情", content="", image=None)
    pixels = io.BytesIO()
    Image.new("RGB", (640, 360), "#c5eadb").save(pixels, format="PNG")
    requests, writes, errors, checks, held = [], [], [], [], []
    controls = dict(fail=False, hold=False, scan=False)
    controls.update(history_revision="", operation=None)
    delete_requests = []
    voice_requests = []
    controls["voice_fail"] = False
    controls.update(
        context_fail=False, context_hold=False, context_legacy=False, context_response_missing=False
    )
    context_requests, context_held = [], []
    sound = io.BytesIO()
    with wave.open(sound, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(
            b"".join(
                struct.pack("<h", round(2000 * math.sin(i * math.tau * 440 / 16000))) for i in range(64000)
            )
        )

    def check(name, passed):
        checks.append(dict(name=name, passed=bool(passed)))
        (args.output / "progress.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2))
        assert passed, name

    def route(r):
        path = urlsplit(r.request.url).path.split("/api/v1")[-1]
        params = parse_qs(urlsplit(r.request.url).query)
        if r.request.method == "DELETE" and path.endswith("/history"):
            body = r.request.post_data_json
            delete_requests.append(dict(path=path, body=body))
            controls["history_revision"] = body["request_id"]
            controls["operation"] = dict(
                id=body["request_id"], state="waiting", error="", message_count=len(messages)
            )
            r.fulfill(json=controls["operation"])
            return
        if r.request.method != "GET":
            writes.append(path)
            r.fulfill(status=500, json=dict(error="unexpected write"))
            return
        if path.endswith("/deletion"):
            operation = controls["operation"]
            r.fulfill(
                json=dict(
                    operation=operation
                    if operation and (operation["state"] != "complete" or params.get("request_id"))
                    else None
                )
            )
            return
        if path.endswith("/context"):
            context_requests.append(path)
            if controls["context_hold"]:
                context_held.append(r)
                return
            if controls["context_fail"]:
                r.fulfill(status=503, json={"error": "服务暂时繁忙，请稍后重试"})
                return
            if controls["context_legacy"]:
                r.fulfill(
                    json=dict(
                        status="partial",
                        missing=["旧回复未记录完整上下文"],
                        input=dict(
                            system=[],
                            data=dict(
                                legacy_context=[dict(content="旧清单原文")],
                                history_coverage=dict(scope="current_conversation", selected_messages=24),
                            ),
                            images=[],
                        ),
                        calls=[],
                        cursor=None,
                        has_more=False,
                    )
                )
                return
            model_input = dict(
                system=["生成当时的系统提示词\n" + "完整提示词保留。" * 200],
                data=dict(
                    conversation_history=[
                        dict(sender="好友", content="历史预算为1000元", timestamp=1700000000)
                    ],
                    current_messages=[dict(sender="好友", content="现在改成1200元", input_scope="deferred")],
                    participant_profiles={"requester": {"nickname": "当时的成员"}},
                    history_coverage=dict(
                        scope="current_conversation",
                        has_more=True,
                        oldest_timestamp=1700000000,
                        newest_timestamp=1700000100,
                        trim_reasons={"round_limit": 2},
                    ),
                ),
                images=[],
                size=dict(tokens=1600, text_bytes=4200),
            )
            if params.get("call_id"):
                r.fulfill(
                    json=dict(
                        call_id=params["call_id"][0],
                        kind="model",
                        request_available=True,
                        response_available=not controls["context_response_missing"],
                        request=dict(
                            model="deepseek-flash",
                            messages=[
                                dict(role="system", content=model_input["system"][0]),
                                dict(
                                    role="user", content=json.dumps(model_input["data"], ensure_ascii=False)
                                ),
                            ],
                            tools=[],
                        ),
                        response=None
                        if controls["context_response_missing"]
                        else dict(status=200, response={"choices": []}),
                    )
                )
            else:
                r.fulfill(
                    json=dict(
                        status="complete",
                        missing=[],
                        input=model_input,
                        calls=[
                            dict(
                                call_id="model-1",
                                kind="model",
                                stage="reply",
                                model="deepseek-flash",
                                available=True,
                                status=200,
                            ),
                            dict(
                                call_id="tool-1",
                                kind="tool",
                                stage="reply",
                                name="finish_reply",
                                available=True,
                            ),
                        ],
                        cursor=None,
                        has_more=False,
                    )
                )
            return
        if path.endswith("/image"):
            r.fulfill(body=pixels.getvalue(), content_type="image/png", headers={"Cache-Control": "no-store"})
            return
        if path.endswith("/voice"):
            voice_requests.append(path)
            r.fulfill(
                status=404 if controls["voice_fail"] else 200,
                body=b"" if controls["voice_fail"] else sound.getvalue(),
                content_type="audio/wav",
                headers={"Cache-Control": "no-store"},
            )
            return
        if path.endswith("/history"):
            requests.append(dict(path=path, params=params))
            if controls["fail"]:
                r.fulfill(status=503, json=dict(error="服务暂时繁忙，请稍后重试"))
                return
            q = params.get("q", [""])[0]
            data = [
                m for m in messages if not q or q.casefold() in (m["content"] + m["type_label"]).casefold()
            ]
            if "/2/" in path:
                data = []
            watermark = max((int(m["id"]) for m in messages), default=0)
            limit = int(params.get("limit", [50])[0])
            before = int(params.get("cursor", [watermark + 1])[0])
            if q and controls["scan"] and "cursor" not in params:
                body = dict(items=[], cursor=str(watermark + 1), watermark=str(watermark), has_more=True)
            elif "after_id" in params:
                values = [m for m in data if int(m["id"]) > int(params["after_id"][0])]
                body = dict(
                    items=values[:limit],
                    cursor=None,
                    watermark=values[limit - 1]["id"] if len(values) > limit else str(watermark),
                    has_more=len(values) > limit,
                )
            else:
                values = [m for m in data if int(m["id"]) < before]
                items = values[-limit:]
                body = dict(
                    items=items,
                    cursor=items[0]["id"] if len(values) > limit else None,
                    watermark=str(watermark),
                    has_more=len(values) > limit,
                )
            if controls["hold"]:
                body.update(history_revision=controls["history_revision"], clearing=None)
                held.append((r, body))
                return
            body.update(history_revision=controls["history_revision"], clearing=None)
            if controls["operation"] and controls["operation"]["state"] != "complete":
                body.update(
                    items=[], cursor=None, watermark="0", has_more=False, clearing=controls["operation"]
                )
            r.fulfill(json=body)
            return
        if path.endswith("/reply-scope"):
            aid = int(path.split("/")[2])
            kind = params.get("kind", ["private"])[0]
            names = (
                ["长备注名称" * 15, "回复关闭好友", "已删除好友"] if kind == "private" else ["已退出的群聊"]
            )
            rows = [
                dict(
                    id=aid * 10 + i,
                    account_id=aid,
                    peer_id=f"fixture-{i}",
                    kind=kind,
                    display_name=name,
                    name=name,
                    allowed=i == 0,
                    pause_until=time.time() + 600 if i == 0 else 0,
                    removed_at=1 if i == 2 or kind == "group" else None,
                    profile={},
                    auto_reply_status="disabled",
                )
                for i, name in enumerate(names)
            ]
            r.fulfill(json=dict(items=rows, total=len(rows), page=1, page_size=10, server_time=time.time()))
            return
        data = {
            "/auth/session": dict(user_id=1, username="隔离验收", csrf="fixture"),
            "/accounts": accounts,
            "/settings": {},
        }
        if path.endswith("/prompt-draft"):
            data[path] = dict(
                version=None,
                markdown=prompt,
                active_prompt=prompt,
                active_sha256=digest,
                base_sha256=digest,
                dirty=False,
                conflict=False,
            )
        if path.endswith("/owner-profile-draft"):
            data[path] = dict(
                version=None,
                markdown="",
                active_prompt="",
                active_sha256=hashlib.sha256(b"").hexdigest(),
                base_sha256=hashlib.sha256(b"").hexdigest(),
                dirty=False,
                conflict=False,
            )
        r.fulfill(json=data.get(path, {}))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(viewport=dict(width=1440, height=900))
            page.add_init_script("sessionStorage.setItem('wechat-page','automation')")
            page.route("**/wechat/api/v1/**", route)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/wechat/")
            buttons = page.locator(".reply-history")
            expect(buttons).to_have_count(3, timeout=15000)
            check("暂停关闭删除均可查看", all(buttons.nth(i).is_enabled() for i in range(3)))
            buttons.first.click()
            dialog = page.locator(".chat-history-dialog")
            body = page.locator(".chat-history-list")
            entries = page.locator(".chat-history-message")
            expect(dialog).to_be_visible()
            expect(entries).to_have_count(50)
            check(
                "首次定位最近50条底部",
                body.evaluate("e=>e.scrollHeight-e.clientHeight-e.scrollTop<2")
                and entries.first.get_attribute("data-message-id") == "86",
            )
            check("标题显示联系人", "长备注名称" in page.locator("#chat-history-title").inner_text())
            trigger = page.locator('[data-message-id="134"] .chat-history-bubble')
            check("聊天列表不预加载上下文", not context_requests)
            trigger.evaluate(
                "e=>{const r=document.createRange();r.selectNodeContents(e);const s=getSelection();s.removeAllRanges();s.addRange(r)}"
            )
            trigger.dispatch_event("click")
            check("选择助手原文不误开上下文", page.locator(".reply-context-dialog").count() == 0)
            page.evaluate("getSelection().removeAllRanges()")
            trigger.press("Enter")
            context_dialog = page.locator(".reply-context-dialog")
            expect(context_dialog).to_be_visible()
            expect(context_dialog).to_contain_text("现在改成1200元")
            expect(context_dialog).to_contain_text("历史预算为1000元")
            check("实际上下文分区和删减原因可读", "轮次上限" in context_dialog.inner_text())
            for width, height in [(1440, 900), (390, 844)]:
                page.set_viewport_size(dict(width=width, height=height))
                check(
                    f"上下文弹框{width}无横向溢出",
                    context_dialog.evaluate(
                        "e=>e.scrollWidth<=e.clientWidth&&e.getBoundingClientRect().left>=15"
                    ),
                )
                page.screenshot(path=str(args.output / f"reply-context-{width}.png"))
            context_dialog.get_by_role("button", name="生成过程", exact=True).click()
            context_dialog.get_by_role("button", name="模型请求 1", exact=True).click()
            expect(context_dialog.locator("summary").filter(has_text="脱敏原始请求")).to_be_visible()
            context_dialog.locator("summary").filter(has_text="脱敏原始请求").click()
            expect(context_dialog.locator("pre").filter(has_text="deepseek-flash")).to_be_visible()
            page.context.grant_permissions(["clipboard-read", "clipboard-write"])
            context_dialog.get_by_role("button", name="复制", exact=True).click()
            page.wait_for_function(
                "async()=>{try{return JSON.parse(await navigator.clipboard.readText()).model==='deepseek-flash'}catch{return false}}"
            )
            check("脱敏实际请求可复制为完整JSON", True)
            page.keyboard.press("Escape")
            expect(context_dialog).to_have_count(0)
            expect(dialog).to_be_visible()
            expect(trigger).to_be_focused()
            check("上下文关闭只关闭顶层并恢复气泡焦点", True)
            controls["context_response_missing"] = True
            trigger.press("Enter")
            context_dialog.get_by_role("button", name="生成过程", exact=True).click()
            context_dialog.get_by_role("button", name="模型请求 1", exact=True).click()
            expect(context_dialog).to_contain_text("该次模型响应记录缺失")
            page.keyboard.press("Escape")
            controls["context_response_missing"] = False
            page.set_viewport_size(dict(width=1440, height=900))
            controls["context_fail"] = True
            trigger.press("Space")
            expect(context_dialog.get_by_role("button", name="重试", exact=True)).to_be_visible()
            controls["context_fail"] = False
            context_dialog.get_by_role("button", name="重试", exact=True).click()
            expect(context_dialog).to_contain_text("现在改成1200元")
            page.keyboard.press("Escape")
            controls["context_legacy"] = True
            trigger.press("Enter")
            expect(context_dialog).to_contain_text("旧回复未记录完整上下文")
            expect(context_dialog).to_contain_text("清单中 24 条历史")
            check("旧回复按冻结清单显示历史数量", True)
            page.keyboard.press("Escape")
            controls["context_legacy"] = False
            controls["context_hold"] = True
            trigger.press("Enter")
            page.wait_for_function(
                "()=>document.querySelector('.reply-context-body')?.getAttribute('aria-busy')==='true'"
            )
            context_dialog.get_by_role("button", name="关闭 AI 回复上下文", exact=True).click()
            controls["context_hold"] = False
            for held_context in context_held:
                try:
                    held_context.fulfill(
                        json=dict(
                            status="unavailable",
                            missing=["迟到内容"],
                            input=None,
                            calls=[],
                            cursor=None,
                            has_more=False,
                        )
                    )
                except Exception:
                    pass
            expect(context_dialog).to_have_count(0)
            check(
                "失败可重试、旧回复明确缺失、关闭拒绝迟到详情",
                page.get_by_text("迟到内容", exact=True).count() == 0,
            )
            check(
                "打开记录不请求或预加载语音",
                not voice_requests and not dialog.locator("audio").get_attribute("src"),
            )
            voice = page.locator('[data-message-id="131"] .chat-voice-button')
            expect(voice).to_have_accessible_name("播放语音，4秒")
            check(
                "横向语音气泡显示声波和秒数",
                voice.bounding_box()["width"] > voice.bounding_box()["height"] * 2
                and voice.inner_text() == "4″"
                and voice.locator("svg").count() == 1,
            )
            voice.press("Enter")
            player = dialog.locator("audio")
            page.wait_for_function(
                "()=>{const a=document.querySelector('.chat-history-dialog audio');return a&&!a.paused&&a.currentTime>0.2}"
            )
            expect(voice).to_have_class("chat-voice-button playing")
            check(
                "键盘启动真实音频解码和声波动画",
                player.evaluate("a=>a.duration===4&&!a.error") and len(voice_requests) == 1,
            )
            voice.click()
            paused = player.evaluate("a=>a.currentTime")
            page.wait_for_timeout(200)
            check(
                "再次点击暂停",
                player.evaluate("a=>a.paused") and abs(player.evaluate("a=>a.currentTime") - paused) < 0.05,
            )
            voice.click()
            page.wait_for_function(
                "t=>document.querySelector('.chat-history-dialog audio').currentTime>t+0.1", arg=paused
            )
            check("暂停后续播不重复取图或音频", len(voice_requests) == 1)
            another = page.locator('[data-message-id="130"] .chat-voice-button')
            another.click()
            expect(another).to_have_class("chat-voice-button playing")
            check(
                "切换语音仅播放一条且发出气泡声波反向",
                dialog.locator("audio").count() == 1
                and voice.get_attribute("aria-pressed") == "false"
                and player.get_attribute("src").endswith("/130/voice")
                and another.locator("svg").evaluate("e=>getComputedStyle(e).transform") != "none",
            )
            page.wait_for_function(
                "()=>!document.querySelector('.chat-history-dialog audio').getAttribute('src')", timeout=7000
            )
            check("自然播放结束恢复可播放状态", another.get_attribute("aria-pressed") == "false")
            controls["voice_fail"] = True
            voice.click()
            expect(dialog.locator(".toast")).to_contain_text("语音暂时无法播放")
            check("播放失败顶部提示且气泡可重试", voice.get_attribute("aria-pressed") == "false")
            controls["voice_fail"] = False
            voice.click()
            expect(voice).to_have_class("chat-voice-button playing")
            voice_search = page.get_by_role("searchbox", name="搜索聊天记录", exact=True)
            voice_search.fill("搜索目标")
            expect(entries).to_have_count(2)
            check(
                "搜索切换停止原音并保留转写高亮",
                not player.get_attribute("src") and page.locator('[data-message-id="70"] mark').count() == 1,
            )
            voice_search.fill("")
            expect(entries).to_have_count(50)
            photo = page.locator('[data-message-id="135"] .chat-image-button')
            expect(photo).to_be_visible()
            expect(photo.locator("img")).to_have_js_property("naturalWidth", 640)
            check(
                "缓存图片按比例缩略",
                photo.bounding_box()["width"] <= 240 and photo.bounding_box()["height"] <= 200,
            )
            photo.press("Enter")
            preview = page.get_by_role("dialog", name="图片预览", exact=True)
            expect(preview).to_be_visible()
            check(
                "大图使用同一缓存地址",
                preview.locator(".chat-image-stage img").get_attribute("src")
                == photo.locator("img").get_attribute("src"),
            )
            page.keyboard.press("Escape")
            expect(preview).to_have_count(0)
            check(
                "Esc优先关大图并恢复焦点",
                dialog.is_visible() and photo.evaluate("e=>document.activeElement===e"),
            )
            photo.click()
            page.get_by_role("button", name="关闭图片预览", exact=True).click()
            expect(preview).to_have_count(0)
            native = page.locator('[data-message-id="134"] .chat-history-text')
            expect(native.locator(".chat-emotion")).to_have_count(3)
            check(
                "内置表情别名与未知标记",
                native.locator(".chat-emotion").evaluate_all(
                    "els=>els.every(e=>e.complete&&e.naturalWidth>0)"
                )
                and "[未知表情]" in native.inner_text(),
            )
            browser.contexts[0].grant_permissions(["clipboard-read", "clipboard-write"])
            native.evaluate(
                "e=>{const r=document.createRange();r.selectNodeContents(e);const s=getSelection();s.removeAllRanges();s.addRange(r)}"
            )
            page.keyboard.press("Control+c")
            check(
                "图标复制仍保留原始别名",
                page.evaluate("navigator.clipboard.readText()") == messages[133]["content"],
            )
            page.evaluate("getSelection().removeAllRanges()")
            photo.click()
            expect(preview).to_be_visible()
            preview.locator(".chat-image-stage img").dispatch_event("error")
            expect(page.locator('[data-message-id="135"] .chat-image-placeholder')).to_be_visible()
            expect(preview).to_have_count(0)
            check(
                "大图失败保留尺寸并在原弹框提示",
                page.locator('[data-message-id="135"] .chat-image-placeholder').bounding_box()["height"]
                == 135
                and dialog.locator(".toast").inner_text() == "图片暂不可用",
            )
            search_emotion = page.get_by_role("searchbox", name="搜索聊天记录", exact=True)
            search_emotion.fill("微笑")
            expect(entries).to_have_count(1)
            check(
                "表情原文搜索并高亮图标", page.locator(".chat-history-text mark .chat-emotion").count() == 1
            )
            search_emotion.fill("")
            expect(entries).to_have_count(50)
            check(
                "未缓存图片和贴图保留占位",
                page.locator('[data-message-id="133"]').inner_text().endswith("[图片]")
                and page.locator('[data-message-id="132"]').inner_text().endswith("[表情]"),
            )
            body.evaluate("e=>{e.scrollTop=0;e.dispatchEvent(new Event('scroll'))}")
            expect(entries).to_have_count(100)
            check(
                "加载旧页保持原阅读锚点",
                abs(
                    page.locator('[data-message-id="86"]').evaluate("e=>e.getBoundingClientRect().top")
                    - body.evaluate("e=>e.getBoundingClientRect().top")
                    - 16
                )
                < 3,
            )
            body.evaluate("e=>{e.scrollTop=0;e.dispatchEvent(new Event('scroll'))}")
            expect(entries).to_have_count(135)
            check(
                "全部历史无重复",
                len(set(entries.evaluate_all("els=>els.map(e=>e.dataset.messageId)"))) == 135,
            )
            expect(page.locator('[data-message-id="20"]')).to_contain_text("已撤回")
            search = page.get_by_role("searchbox", name="搜索聊天记录", exact=True)
            before = body.evaluate("e=>e.scrollTop")
            search.fill("搜索目标")
            expect(entries).to_have_count(2)
            check(
                "搜索全历史并高亮",
                page.locator(".chat-history-list mark").count() >= 2
                and entries.first.get_attribute("data-message-id") == "2",
            )
            search.fill("撤回原文唯一词")
            expect(entries).to_have_count(1)
            check("撤回原文可搜索", entries.first.get_attribute("data-message-id") == "20")
            search.fill("搜索目标100%_!")
            expect(entries).to_have_count(1)
            expect(entries.first).to_have_attribute("data-message-id", "2")
            check("特殊字符按字面搜索", entries.first.get_attribute("data-message-id") == "2")
            controls["hold"] = True
            search.fill("撤回原文唯一词")
            page.wait_for_timeout(450)
            check("旧查询处于等待中", len(held) == 1)
            controls["hold"] = False
            search.fill("搜索目标")
            expect(entries).to_have_count(2)
            old_route, old_body = held.pop()
            try:
                old_route.fulfill(json=old_body)
            except Exception:
                pass  # 请求已取消时，浏览器可以拒绝旧回执。
            page.wait_for_timeout(100)
            check(
                "过期搜索回执不覆盖新结果",
                entries.count() == 2 and entries.first.get_attribute("data-message-id") == "2",
            )
            search.fill("不存在的内容")
            expect(page.get_by_text("没有匹配的聊天记录", exact=True)).to_be_visible()
            search.fill("")
            expect(entries).to_have_count(135)
            check("清空搜索恢复历史位置", abs(body.evaluate("e=>e.scrollTop") - before) < 3)
            controls["scan"] = True
            search.fill("搜索目标")
            expect(entries).to_have_count(2)
            check(
                "空扫描批次继续搜索",
                any("cursor" in x["params"] and x["params"].get("q") == ["搜索目标"] for x in requests),
            )
            controls["scan"] = False
            search.fill("")
            expect(entries).to_have_count(135)
            # 原生组词事件与输入事件均由浏览器派发，组词中不应提交查询。
            count = len([x for x in requests if x["params"].get("q") == ["组词中"]])
            search.dispatch_event("compositionstart")
            search.fill("组词中")
            page.wait_for_timeout(450)
            check(
                "中文组词期间不查询",
                len([x for x in requests if x["params"].get("q") == ["组词中"]]) == count,
            )
            search.dispatch_event("compositionend")
            expect(page.get_by_text("没有匹配的聊天记录", exact=True)).to_be_visible()
            search.fill("")
            expect(entries).to_have_count(135)
            # 历史阅读时同步新消息，不改变当前看到的消息位置。
            body.evaluate('e=>{e.scrollTop=200;e.dispatchEvent(new Event("scroll"))}')
            pos = page.locator('[data-message-id="4"]').evaluate("e=>e.getBoundingClientRect().top")
            messages.append({**messages[-1], "id": "136", "timestamp": 1700000136, "content": "新增消息"})
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(entries).to_have_count(136, timeout=10000)
            check(
                "新消息不打断历史阅读",
                abs(page.locator('[data-message-id="4"]').evaluate("e=>e.getBoundingClientRect().top") - pos)
                < 3,
            )
            body.evaluate('e=>{e.scrollTop=e.scrollHeight;e.dispatchEvent(new Event("scroll"))}')
            messages.append({**messages[-1], "id": "137", "timestamp": 1700000137, "content": "底部自动跟随"})
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(entries).to_have_count(137, timeout=10000)
            check("底部跟随新增消息", body.evaluate("e=>e.scrollHeight-e.clientHeight-e.scrollTop<2"))
            # 隐藏页暂停读取，恢复可见后恢复同步。
            page.evaluate(
                "Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))"
            )
            count = len(requests)
            page.wait_for_timeout(5200)
            check("隐藏页无聊天读取", len(requests) == count)
            page.evaluate(
                "Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))"
            )
            page.wait_for_timeout(150)
            check("返回可见恢复读取", len(requests) > count)
            controls["fail"] = True
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(page.locator(".chat-history-state")).to_have_text("读取失败，正在重试")
            check(
                "失败保留记录且顶部提示在弹框内",
                entries.count() == 137 and dialog.locator(".toast").count() == 1,
            )
            controls["fail"] = False
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(page.locator(".chat-history-state")).to_have_text("")
            expect(dialog.locator(".toast")).to_have_count(0, timeout=8000)
            for width, height in ((1864, 893), (1440, 900), (390, 844), (320, 568), (780, 360)):
                page.set_viewport_size(dict(width=width, height=height))
                geometry = dialog.evaluate(
                    """e=>{const r=e.getBoundingClientRect(),b=e.querySelector('.chat-history-list'),s=e.querySelector('.chat-history-tools').getBoundingClientRect();return r.left>=15&&r.right<=innerWidth-15&&r.top>=15&&r.bottom<=innerHeight-15&&b.clientHeight>80&&s.bottom<=r.bottom&&e.scrollWidth<=e.clientWidth+1&&document.documentElement.scrollWidth<=innerWidth}"""
                )
                check(f"{width}x{height}布局可读无溢出", geometry)
                if width >= 1000:
                    check(f"{width}桌面弹框使用900px宽度", abs(dialog.bounding_box()["width"] - 900) < 2)
                page.screenshot(path=str(args.output / f"history-{width}-{height}.png"))
            page.set_viewport_size(dict(width=390, height=844))
            photo.click()
            expect(preview).to_be_visible()
            check(
                "窄屏大图无横向溢出",
                preview.evaluate(
                    "e=>{const r=e.getBoundingClientRect();return r.left>=15&&r.right<=innerWidth-15&&e.scrollWidth<=e.clientWidth+1}"
                ),
            )
            page.screenshot(path=str(args.output / "image-preview-390.png"))
            page.mouse.click(2, 2)
            expect(preview).to_have_count(0)
            check("点击大图遮罩仅关闭预览", dialog.is_visible())
            page.set_viewport_size(dict(width=1440, height=900))
            search.press("Escape")
            expect(dialog).to_have_count(0)
            check("Esc关闭且焦点回原入口", buttons.first.evaluate("e=>document.activeElement===e"))
            count = len(requests)
            page.wait_for_timeout(5200)
            check("关闭弹框停止读取", len(requests) == count)
            page.get_by_role("button", name="群聊", exact=True).click()
            expect(buttons).to_have_count(1)
            check("退群后仍有记录入口", buttons.first.is_enabled())
            buttons.first.click()
            expect(entries).to_have_count(50)
            page.get_by_role("button", name="关闭聊天记录", exact=True).click()
            page.get_by_role("combobox", name="选择账号", exact=True).click()
            page.get_by_role("option", name="隔离账号2", exact=True).click()
            expect(page.get_by_role("combobox", name="选择账号", exact=True)).to_contain_text("隔离账号2")
            page.get_by_role("button", name="好友", exact=True).click()
            expect(page.locator(".reply-scope-row")).to_have_count(3)
            buttons.first.click()
            expect(page.get_by_text("暂无已保存的聊天记录", exact=True)).to_be_visible()
            check(
                "账号切换不串历史", entries.count() == 0 and requests[-1]["path"].startswith("/accounts/2/")
            )
            failed_page = browser.new_page(viewport=dict(width=390, height=844))
            failed_page.add_init_script("sessionStorage.setItem('wechat-page','automation')")
            failed_page.route("**/wechat/api/v1/**", route)
            failed_page.route("**/assets/native-emotions/*.png", lambda r: r.fulfill(status=404))
            failed_page.on("pageerror", lambda error: errors.append(str(error)))
            failed_page.goto(f"http://127.0.0.1:{server.server_port}/wechat/")
            failed_page.locator(".reply-history").first.click()
            failed_native = failed_page.locator('[data-message-id="134"] .chat-history-text')
            expect(failed_native).to_have_text(messages[133]["content"])
            check("表情资源404回退完整原文", failed_native.locator(".chat-emotion").count() == 0)
            failed_page.close()
            page.get_by_role("button", name="关闭聊天记录", exact=True).click()
            page.get_by_role("combobox", name="选择账号", exact=True).click()
            page.get_by_role("option", name="隔离账号1", exact=True).click()
            expect(page.get_by_role("combobox", name="选择账号", exact=True)).to_contain_text("隔离账号1")
            buttons.first.click()
            expect(entries).to_have_count(50)
            search = page.get_by_role("searchbox", name="搜索聊天记录", exact=True)
            search.fill("4秒")
            expect(entries).to_have_count(2)
            clear = page.get_by_role("button", name="清空聊天记录", exact=True)
            clear.click()
            confirmation = page.get_by_role("dialog", name="清空聊天记录", exact=True)
            expect(confirmation).to_be_visible()
            check(
                "清空必须二次确认且默认焦点在取消",
                not delete_requests
                and confirmation.get_by_role("button", name="取消", exact=True).evaluate(
                    "e=>document.activeElement===e"
                ),
            )
            confirmation.get_by_role("button", name="取消", exact=True).press("Escape")
            expect(confirmation).to_have_count(0)
            check(
                "Esc只取消确认并恢复焦点",
                dialog.is_visible()
                and clear.evaluate("e=>document.activeElement===e")
                and not delete_requests,
            )
            clear.click()
            page.set_viewport_size(dict(width=390, height=844))
            check(
                "窄屏确认框及长会话名称不溢出",
                confirmation.evaluate(
                    "e=>{const r=e.getBoundingClientRect();return r.left>=15&&r.right<=innerWidth-15&&r.top>=15&&r.bottom<=innerHeight-15&&e.scrollWidth<=e.clientWidth+1}"
                ),
            )
            page.screenshot(path=str(args.output / "clear-confirmation-390.png"))
            confirmation.get_by_role("button", name="取消", exact=True).click()
            page.locator('[data-message-id="131"] .chat-voice-button').click()
            expect(page.locator('[data-message-id="131"] .chat-voice-button')).to_have_class(
                "chat-voice-button playing"
            )
            controls["hold"] = True
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.wait_for_timeout(150)
            clear.click()
            confirmation.get_by_role("button", name="确认清空", exact=True).click()
            expect(confirmation).to_have_count(0)
            expect(page.get_by_text("聊天记录清空中", exact=True)).to_be_visible()
            check(
                "确认后清空全部范围并停止播放",
                len(delete_requests) == 1
                and delete_requests[0]["body"]["confirmed"] is True
                and not urlsplit(delete_requests[0]["path"]).query
                and page.locator(".chat-history-dialog audio").evaluate(
                    "a=>a.paused&&!a.getAttribute('src')"
                ),
            )
            check("清理期间禁止重复提交和搜索", clear.is_disabled() and search.is_disabled())
            controls["hold"] = False
            messages[:] = [
                dict(
                    id="138",
                    timestamp=1700000138,
                    sender_name="成员昵称",
                    direction="inbound",
                    source="live",
                    msg_type="1",
                    type_label="文本",
                    content="清空期间的新消息",
                    revoked=False,
                )
            ]
            controls["operation"]["state"] = "complete"
            expect(page.get_by_text("聊天记录已清空", exact=True)).to_be_visible(timeout=10000)
            expect(entries).to_have_count(1)
            check(
                "完成后恢复搜索并保留新消息",
                search.input_value() == ""
                and search.is_enabled()
                and entries.first.get_attribute("data-message-id") == "138",
            )
            for old_route, old_body in held:
                try:
                    old_route.fulfill(json=old_body)
                except Exception:
                    pass
            held.clear()
            page.wait_for_timeout(150)
            check(
                "删除前迟到响应不能恢复旧内容",
                entries.count() == 1 and entries.first.get_attribute("data-message-id") == "138",
            )
            controls["history_revision"] = "remote-clear"
            messages[:] = [
                {**messages[0], "id": "139", "timestamp": 1700000139, "content": "另一页面清空之后的新消息"}
            ]
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(entries.first).to_have_attribute("data-message-id", "139")
            check("其他页面清空也会淘汰本页历史缓存", entries.count() == 1)
            page.screenshot(path=str(args.output / "clear-complete-390.png"))
            check("无意外业务写入或浏览器错误", not writes and not errors)
            browser.close()
    finally:
        server.shutdown()
        (args.output / "result.json").write_text(
            json.dumps(
                dict(
                    checks=checks,
                    errors=errors,
                    writes=writes,
                    delete_requests=delete_requests,
                    request_count=len(requests),
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    print(json.dumps(dict(passed=len(checks), errors=errors, writes=writes), ensure_ascii=False))


if __name__ == "__main__":
    main()
