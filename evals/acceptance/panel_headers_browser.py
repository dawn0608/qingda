"""服务器隔离验收：统一标题、自动刷新及未保存输入保护，仅使用内存 API。"""
import argparse
import hashlib
import http.server
import json
import pathlib
import threading
import time
from collections import Counter
from urllib.parse import parse_qs, urlsplit
from playwright.sync_api import expect, sync_playwright

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dist', type=pathlib.Path, required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    dist = args.dist.resolve()

    class Handler(http.server.SimpleHTTPRequestHandler):

        def translate_path(self, path):
            target = (dist / urlsplit(path).path.removeprefix('/wechat/')).resolve()
            return str(target if target.is_relative_to(dist) and target.is_file() else dist / 'index.html')

        def log_message(self, *_):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results, errors = ([], [])
    markdown = '## 角色\n\n隔离助手设定，自动刷新不能改写正文。'
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    account = dict(id=1, nickname='验收微信', online=True, enabled=True, ai_enabled=True, prompt=markdown)

    def check(name, value):
        results.append(dict(name=name, passed=bool(value)))
        assert value, name

    def scene(browser, view, width=1440, height=1000):
        state = dict(token='fixture-original', revision='fixture-v1', status='configured', fail=False, contacts_fail=False, contact='原接收好友')
        counts = Counter()
        writes = []
        page = browser.new_page(viewport=dict(width=width, height=height))
        page.set_default_timeout(10000)
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(r):
            request = r.request
            path = urlsplit(request.url).path.split('/api/v1')[-1]
            counts[path] += 1
            if request.method != 'GET':
                writes.append((path, request.post_data))
            if path == '/auth/session':
                data = dict(user_id=1, username='隔离用户', csrf='fixture', phone='13800000000')
            elif path == '/auth/options':
                data = dict(sms_available=True, email_available=True)
            elif path == '/accounts':
                data = [account]
            elif path == '/settings':
                data = dict(model=dict(model='deepseek-flash', timeout=60, max_tokens=4096), model_revision=1, model_key_configured=True)
            elif path == '/wechat-config':
                if state['fail']:
                    r.fulfill(status=503, json={'detail': 'fixture unavailable'})
                    return
                data = dict(token=state['token'], revision=state['revision'], state=state['status'], callback_url='https://example.com/callback/fixture')
            elif path == '/conversations':
                if state['contacts_fail']:
                    r.fulfill(status=503, json={'detail': 'fixture unavailable'})
                    return
                data = [dict(id=55, name=state['contact']), dict(id=56, name='备用接收好友')]
            elif path.endswith('/reply-scope'):
                q = parse_qs(urlsplit(request.url).query)
                data = dict(items=[] if q.get('q') else [dict(id=55, name='隔离好友', allowed=True, removed_at=None, pause_until=0)], total=0 if q.get('q') else 1, page=1, page_size=10, server_time=time.time())
            elif path.endswith('/prompt-draft'):
                data = dict(version=None, markdown=markdown, base_sha256=digest, active_prompt=markdown, active_sha256=digest, dirty=False, conflict=False)
            elif path.endswith('/online'):
                data = account
            else:
                data = {}
            r.fulfill(status=200, json=data)
        page.route('**/wechat/api/v1/**', route)
        page.add_init_script("sessionStorage.setItem('wechat-page'," + json.dumps(view) + ')')
        page.goto(f'http://127.0.0.1:{server.server_port}/wechat/')
        expect(page.locator('.panel-heading')).to_have_count(2 if view == 'automation' else 2)
        if view == 'automation':
            expect(page.locator('.vditor-ir pre.vditor-reset')).to_contain_text('隔离助手设定')
        else:
            expect(page.get_by_role('textbox', name='接入 Token')).to_have_value('fixture-original')
        page.wait_for_timeout(300)
        return (page, state, counts, writes)

    def headers(page, view, width):
        rows = page.locator('.panel-heading').evaluate_all("els => els.map(e => {\n          const h=e.querySelector('h2'),s=getComputedStyle(h),r=e.getBoundingClientRect(),hs=getComputedStyle(e),a=e.querySelector('.panel-heading-actions');\n          return {title:h.textContent,font:s.fontSize,height:r.height,gradient:hs.backgroundImage,padding:hs.paddingLeft,paragraphs:e.querySelectorAll('p').length,overflow:a? a.getBoundingClientRect().right > r.right: false};\n        })")
        size = page.locator('.topbar h1').evaluate('e => parseFloat(getComputedStyle(e).fontSize)')
        (args.output / f'{view}-{width}-geometry.json').write_text(json.dumps(rows, ensure_ascii=False))
        check(f'{view}:{width}:标题14px且小于顶栏', all((r['font'] == '14px' for r in rows)) and size > 14)
        check(f'{view}:{width}:统一标题带', all((r['height'] == (40 if width <= 780 else 24) and 'gradient' in r['gradient'] for r in rows)))
        check(f'{view}:{width}:无标题说明及操作溢出', all((not r['paragraphs'] and (not r['overflow']) for r in rows)))
        check(f'{view}:{width}:无手动刷新', page.get_by_role('button', name='刷新', exact=False).count() == 0)
        check(f'{view}:{width}:无横向溢出', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        page.screenshot(path=str(args.output / f'{view}-{width}.png'), full_page=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            for width, height in ((1440, 1000), (1366, 640), (390, 844), (320, 568)):
                for view in ('automation', 'settings'):
                    page, _, _, _ = scene(browser, view, width, height)
                    headers(page, view, width)
                    page.close()
            page, state, counts, writes = scene(browser, 'settings')
            token = page.get_by_role('textbox', name='接入 Token')
            before = counts.copy()
            state.update(token='fixture-remote', revision='fixture-v2', contact='自动更新好友')
            page.wait_for_timeout(5500)
            expect(token).to_have_value('fixture-remote')
            check('设置自动更新', all((counts[k] > before[k] for k in ('/wechat-config', '/settings'))))
            token.fill('fixture-unsaved')
            state.update(token='fixture-other', revision='fixture-v3')
            page.get_by_role('option', name='备用接收好友').click()
            page.wait_for_timeout(5500)
            expect(token).to_have_value('fixture-unsaved')
            check('轮询保留未保存Token', True)
            state.update(fail=True, contacts_fail=True)
            page.wait_for_timeout(5500)
            expect(page.locator('.wechat-settings .badge')).to_have_text('读取失败')
            state.update(fail=False, contacts_fail=False)
            page.wait_for_timeout(5500)
            expect(page.locator('.wechat-settings .badge')).to_have_text('已绑定')
            expect(token).to_have_value('fixture-unsaved')
            check('失败后自动恢复且保留输入', True)
            page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(500)
            before = counts.copy()
            page.wait_for_timeout(5500)
            check('隐藏页面不读取', all((counts[k] == before[k] for k in ('/wechat-config', '/settings'))))
            page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'));for(let i=0;i<10;i++)window.dispatchEvent(new Event('focus'))")
            page.wait_for_timeout(500)
            check('返回页面立即刷新且焦点事件不重叠', all((counts[k] == before[k] + 1 for k in ('/wechat-config', '/settings'))))
            check('刷新无业务写入', not writes)
            page.close()
            browser.close()
            check('无浏览器脚本错误', not errors)
    finally:
        server.shutdown()
        (args.output / 'results.json').write_text(json.dumps(dict(passed=bool(results) and all((r['passed'] for r in results)) and (not errors), checks=results, errors=errors), ensure_ascii=False, indent=2))
        print(json.dumps(dict(checks=len(results), failed=[r for r in results if not r['passed']], errors=errors), ensure_ascii=False), flush=True)
if __name__ == '__main__':
    main()
