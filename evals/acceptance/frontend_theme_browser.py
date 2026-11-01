"""服务器隔离视觉验收：候选/基线几何比较、真实组件截图和文字对比度；仅内存API。"""
import argparse
import hashlib
import http.server
import json
import pathlib
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit
from playwright.sync_api import expect, sync_playwright

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dist', type=pathlib.Path, required=True)
    parser.add_argument('--baseline', type=pathlib.Path, required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--match', default='', help='仅运行名称包含该字符串的场景')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    servers = []

    def serve(dist):

        class Handler(http.server.SimpleHTTPRequestHandler):

            def translate_path(self, path):
                target = (dist / urlsplit(path).path.removeprefix('/wechat/')).resolve()
                return str(target if target.is_relative_to(dist.resolve()) and target.is_file() else dist / 'index.html')

            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f'http://127.0.0.1:{server.server_port}'
    origins = [serve(args.baseline), serve(args.dist)]
    results, errors = ([], [])
    markdown = '## 统一视觉验收\n\n助手设定完整保留。\n\n> 引用内容\n\n[公开链接](https://example.com)\n\n```text\n代码内容\n```\n\n| 列一 | 列二 |\n| --- | --- |\n| 内容 | 内容 |'
    account = dict(id=1, nickname='验收微信', wechat_id='fixture_account', online=True, enabled=True, ai_enabled=True, prompt=markdown, mobile='138****0000', gender=1, signature='完整资料和长文本展示', device_type='iPad', last_profile_refresh_at=time.time())
    digest = hashlib.sha256(markdown.encode()).hexdigest()

    def route(request, mode):
        path = urlsplit(request.request.url).path.split('/api/v1')[-1]
        if path == '/auth/session':
            if mode == 'loading':
                return
            if mode.startswith('login'):
                request.fulfill(status=401, json={'detail': '请登录管理台'})
                return
            data = dict(user_id=1, username='验收用户', phone='13800000000', email='', csrf='fixture', auth_channel='phone', sms_available=True, email_available=True)
        elif path == '/auth/options':
            data = dict(sms_available=True, email_available=True, registration_enabled=True)
            if mode == 'filing':
                data['filing'] = dict(icp_number='隔离备案信息', icp_url='https://beian.miit.gov.cn/', public_security_number='', public_security_url='')
        elif path == '/accounts':
            data = [account, {**account, 'id': 2, 'nickname': '离线账号', 'online': False}]
            if mode == 'blank':
                data[0] = {**account, 'prompt': ''}
        elif path.endswith('/online'):
            data = account
        elif path == '/settings':
            data = dict(model=dict(model='deepseek-flash', timeout=60, max_tokens=4096), model_key_configured=True)
        elif path == '/models':
            data = dict(models=[dict(id='deepseek-flash', name='deepseek-flash')])
        elif path == '/auth/code/send':
            if mode == 'login-busy':
                return
            if mode == 'login-error':
                request.fulfill(status=503, json={'detail': 'fixture unavailable'})
                return
            data = dict(challenge_id='fixture', retry_after=60, expires_in=300)
        elif path == '/wechat-config':
            data = dict(token='fixture-token', callback_url='https://example.com/fixture/callback', state='configured', revision='fixture')
        elif path == '/conversations':
            data = [dict(id=55, name='验收好友')]
        elif path.endswith('/reply-scope'):
            query = parse_qs(urlsplit(request.request.url).query)
            items = [dict(id=55 + i, name='验收好友' if i == 0 else f'联系人 {i + 1}', allowed=True, pause_until=time.time() + 900 if i == 1 else 0, removed_at=None) for i in range(10)]
            if query.get('q'):
                items = []
            data = dict(items=items, total=len(items), page=1, page_size=10, server_time=time.time())
        elif '/prompt-draft' in path:
            data = dict(version=None, markdown=markdown, base_sha256=digest, active_prompt=markdown, active_sha256=digest, dirty=False, conflict=False)
            if mode == 'blank':
                empty = hashlib.sha256(b'').hexdigest()
                data.update(markdown='', active_prompt='', active_sha256=empty, base_sha256=empty)
        elif path == '/profile':
            if mode == 'error':
                request.fulfill(status=503, json={'detail': 'fixture unavailable'})
                return
            data = dict(user_id=1, username='新昵称', phone='13800000000', csrf='fixture')
        else:
            data = {}
        request.fulfill(status=200, json=data)
    contrast_js = "() => {\n      const rgb=s=>(s.match(/[\\d.]+/g)||[]).map(Number);\n      const luminance=c=>c.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);\n      const ratio=(a,b)=>{let x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)};\n      const backgrounds=e=>{for(let p=e;p;p=p.parentElement){let s=getComputedStyle(p);if(s.backgroundImage.includes('gradient'))return [...s.backgroundImage.matchAll(/rgba?\\([^)]+\\)/g)].map(m=>rgb(m[0])).filter(c=>c.length===3||c[3]===1);let c=rgb(s.backgroundColor);if(c.length===3||c[3]===1)return [c]}return [[255,255,255]]};\n      const failures=[],samples=[]; const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);\n      for(let n;n=walker.nextNode();){if(!n.textContent.trim())continue;let e=n.parentElement;if(!e||e.closest('script,style,[hidden],[aria-hidden=true],[disabled],[aria-disabled=true]'))continue;let s=getComputedStyle(e),r=e.getBoundingClientRect();if(!r.width||!r.height||r.bottom<0||r.top>innerHeight||s.visibility==='hidden'||s.opacity==='0')continue;let f=rgb(s.color),b=backgrounds(e);if(!b.length||f[3]===0)continue;let score=Math.min(...b.map(c=>ratio(f,c)));let row={text:n.textContent.trim().slice(0,40),ratio:+score.toFixed(2),color:s.color,backgrounds:b};samples.push(row);if(score<4.5)failures.push(row)}\n      for(const e of document.querySelectorAll('input[placeholder],textarea[placeholder],.vditor-ir pre.vditor-reset:empty')){if(e.value||e.disabled)continue;let r=e.getBoundingClientRect();if(!r.width||!r.height||r.bottom<0||r.top>innerHeight)continue;let color=getComputedStyle(e,e.matches('pre')?'::before':'::placeholder').color,b=backgrounds(e);if(!b.length)continue;let score=Math.min(...b.map(c=>ratio(rgb(color),c))),row={text:'占位文字',ratio:+score.toFixed(2),color,backgrounds:b};samples.push(row);if(score<4.5)failures.push(row)}\n      for(const e of document.querySelectorAll('input,textarea')){if(!e.value||e.disabled)continue;let r=e.getBoundingClientRect();if(!r.width||!r.height||r.bottom<0||r.top>innerHeight)continue;let s=getComputedStyle(e),b=backgrounds(e);if(!b.length)continue;let score=Math.min(...b.map(c=>ratio(rgb(s.color),c))),row={text:e.readOnly?'只读字段':'输入正文',ratio:+score.toFixed(2),color:s.color,backgrounds:b};samples.push(row);if(score<4.5)failures.push(row)}\n      for(const e of document.querySelectorAll('.primary:disabled')){let r=e.getBoundingClientRect();if(!r.width||!r.height||r.bottom<0||r.top>innerHeight)continue;let s=getComputedStyle(e),b=backgrounds(e);if(!b.length)continue;let score=Math.min(...b.map(c=>ratio(rgb(s.color),c))),row={text:'禁用主按钮',ratio:+score.toFixed(2),color:s.color,backgrounds:b};samples.push(row);if(score<4.5||s.opacity!=='1')failures.push(row)}\n      return {failures,samples};\n    }"
    selectors = '.sidebar,.topbar,.panel,.assistant-toolbar,.assistant-column,.assistant-column-heading,.reply-scope-tools,.reply-scope-footer,.account-dialog,.account-dialog > header,.account-dialog-body,.login-layout,.login-form-panel,.filing-notice'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])

        def scene(origin, mode, width, height):
            page = browser.new_page(viewport=dict(width=width, height=height))
            page.set_default_timeout(7000)
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('**/wechat/api/v1/**', lambda r: route(r, mode))
            view = 'automation' if mode in ('assistant', 'select', 'empty', 'blank') else 'settings' if mode == 'settings' else 'accounts'
            page.add_init_script("sessionStorage.setItem('wechat-page'," + json.dumps(view) + ')')
            page.goto(origin + '/wechat/')
            page.wait_for_timeout(500)
            if mode in ('assistant', 'select', 'empty'):
                expect(page.locator('.prompt-live-editor .vditor')).to_be_visible()
                expect(page.locator('.prompt-live-editor .vditor')).to_contain_text('助手设定完整保留')
            elif mode == 'blank':
                expect(page.locator('.prompt-live-editor .vditor-ir pre.vditor-reset')).to_be_visible()
            if mode == 'select':
                page.get_by_role('combobox', name='回复状态筛选').click()
            elif mode == 'empty':
                page.get_by_role('textbox', name='搜索回复范围').fill('无结果')
                page.wait_for_timeout(450)
            elif mode in ('menu', 'profile', 'contact', 'success', 'error', 'info'):
                page.get_by_role('button', name='账户：验收用户').click()
                if mode != 'menu':
                    page.get_by_role('button', name='账户设置', exact=True).click()
                    if mode == 'contact':
                        page.get_by_role('button', name='绑定邮箱', exact=True).click()
                    elif mode in ('profile', 'success', 'error', 'info'):
                        page.get_by_role('button', name='修改昵称', exact=True).click()
                        if mode in ('success', 'error'):
                            page.get_by_role('textbox', name='昵称', exact=True).fill('新昵称')
                            page.get_by_role('button', name='保存昵称', exact=True).click()
                        elif mode == 'info':
                            page.get_by_role('button', name='保存昵称', exact=True).click()
            elif mode.startswith('login'):
                expect(page.get_by_role('heading', name='登录青答', exact=True)).to_be_visible()
                assert page.locator('form').count() == 1
                assert page.get_by_role('button', name='注册', exact=True).count() == 0
                if mode != 'login':
                    identifier = 'fixture@example.invalid' if mode == 'login-email' else '13800000000'
                    page.get_by_label('邮箱或手机号', exact=True).fill(identifier)
                    if mode in ('login-code', 'login-cooldown', 'login-busy', 'login-error'):
                        page.get_by_role('button', name='获取验证码', exact=True).click()
                        if mode == 'login-busy':
                            expect(page.get_by_role('button', name='发送中', exact=True)).to_be_visible()
                        elif mode == 'login-error':
                            expect(page.locator('.toast-error')).to_be_visible()
                        else:
                            expect(page.get_by_role('button', name='秒后重发', exact=False)).to_be_visible()
                            if mode == 'login-code':
                                page.get_by_label('验证码', exact=True).fill('123456')
            elif mode == 'detail':
                page.get_by_role('button', name='隔离计划', exact=False).click()
            if mode in ('success', 'error', 'info'):
                expect(page.locator('.toast')).to_be_visible()
            page.wait_for_timeout(260)
            page.wait_for_function("document.getAnimations().every(a=>a.effect.getComputedTiming().iterations===Infinity||a.playState!=='running')")
            return page
        cases = [(m, 1440, 900) for m in ('accounts', 'assistant', 'settings', 'select', 'menu', 'profile', 'contact', 'login', 'login-email', 'login-code', 'login-cooldown', 'login-busy', 'login-error', 'empty', 'success', 'error', 'info', 'blank', 'loading', 'filing')]
        cases += [(m, w, h) for w, h in ((1366, 768), (1920, 1080), (390, 844), (320, 740), (390, 500)) for m in ('accounts', 'assistant', 'settings', 'contact', 'login')]
        cases += [(m, 390, 844) for m in ('filing', 'blank', 'menu', 'profile', 'success', 'error', 'info', 'login-code', 'login-busy', 'login-error')]
        for mode, width, height in cases:
            name = f'{mode}-{width}x{height}'
            if args.match and args.match not in name:
                continue
            pages = []
            try:
                for origin in origins:
                    pages.append(scene(origin, mode, width, height))
                boxes = [p.locator(selectors).evaluate_all('es=>es.map(e=>{const r=e.getBoundingClientRect();return [e.className,r.x,r.y,r.width,r.height]})') for p in pages]
                assert len(boxes[0]) == len(boxes[1]), boxes
                for before, after in zip(*boxes):
                    assert before[0] == after[0] and max((abs(a - b) for a, b in zip(before[1:], after[1:]))) <= 1, (before, after)
                page = pages[1]
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), 'horizontal overflow'
                contrast = page.evaluate(contrast_js)
                assert not contrast['failures'], contrast['failures']
                assert not errors, errors
                if width in (1440, 390):
                    page.screenshot(path=str(args.output / (name + '.png')), full_page=True)
                results.append(dict(name=name, passed=True, geometry_unchanged=True, text_samples=len(contrast['samples']), minimum_contrast=min((x['ratio'] for x in contrast['samples']), default=None)))
            except Exception:
                if pages:
                    pages[-1].screenshot(path=str(args.output / (name + '-failed.png')), full_page=True)
                results.append(dict(name=name, passed=False, error=traceback.format_exc()[-3500:]))
            finally:
                for page in pages:
                    page.unroute_all(behavior='ignoreErrors')
                    page.close()
                (args.output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
            if args.match and args.match not in mode:
                continue
            page = scene(origins[1], mode, 1440, 900)
            for selector in controls:
                control = page.locator(selector).first
                if not control.count() or not control.is_visible():
                    continue
                name = f'{mode}-states-{len(results)}'
                try:
                    control.focus()
                    control.hover()
                    page.wait_for_timeout(200)
                    for state in ('focus-hover', 'pressed'):
                        if state == 'pressed':
                            page.mouse.down()
                            page.wait_for_timeout(200)
                        contrast = page.evaluate(contrast_js)
                        assert not contrast['failures'], contrast['failures']
                        page.screenshot(path=str(args.output / f'{name}-{state}.png'), full_page=True)
                    results.append(dict(name=name, passed=True, selector=selector))
                except Exception:
                    results.append(dict(name=name, passed=False, selector=selector, error=traceback.format_exc()[-3500:]))
                finally:
                    page.mouse.move(0, 0)
                    page.mouse.up()
            page.close()
        page = scene(origins[1], 'accounts', 1440, 900)
        page.emulate_media(reduced_motion='reduce')
        page.get_by_role('button', name='微信管理', exact=True).focus()
        focus = page.get_by_role('button', name='微信管理', exact=True).evaluate('e=>({width:getComputedStyle(e).outlineWidth,style:getComputedStyle(e).outlineStyle})')
        assert focus['style'] != 'none' and float(focus['width'].removesuffix('px')) >= 2, focus
        assert page.locator('.sidebar nav button').first.evaluate('e=>getComputedStyle(e).transitionDuration') == '0s'
        results.append(dict(name='keyboard-focus-and-reduced-motion', passed=True))
        browser.close()
    for server in servers:
        server.shutdown()
    (args.output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(dict(passed=sum((x['passed'] for x in results)), total=len(results))))
    assert all((x['passed'] for x in results))
if __name__ == '__main__':
    main()
