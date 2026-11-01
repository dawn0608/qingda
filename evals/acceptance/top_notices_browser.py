"""服务器隔离浏览器验收：真实候选构建，内存API，无生产数据及供应商调用。"""
import argparse, functools, hashlib, http.server, json, pathlib, threading, time, traceback
from playwright.sync_api import sync_playwright, expect

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dist', type=pathlib.Path, required=True)
    p.add_argument('--output', type=pathlib.Path, required=True)
    p.add_argument('--match', default='')
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    class Handler(http.server.SimpleHTTPRequestHandler):

        def translate_path(self, path):
            target = (args.dist / path.split('?')[0].removeprefix('/wechat/')).resolve()
            return str(target if target.is_relative_to(args.dist.resolve()) and target.is_file() else args.dist / 'index.html')

        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    results = []
    controls = {}
    writes = []
    errors = []
    user = {}
    account = {}
    stored = {}

    def receipt():
        active = account['prompt']
        return dict(version=stored.get('version'), markdown=stored.get('markdown', active), base_sha256=stored.get('base_sha256', hashlib.sha256(active.encode()).hexdigest()), active_prompt=active, active_sha256=hashlib.sha256(active.encode()).hexdigest(), dirty=stored.get('markdown', active) != active, conflict=False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1440, 'height': 900})
        page = context.new_page()

        def route(r):
            path = r.request.url.split('/api/v1')[-1].split('?')[0]
            method = r.request.method
            if method != 'GET':
                writes.append({'path': path, 'method': method, 'body': r.request.post_data})
            failure = controls.get((path, method))
            if failure:
                if failure == 'network':
                    r.abort('failed')
                else:
                    r.fulfill(status=failure[0], json={'detail': failure[1]})
                return
            if path == '/auth/session':
                if controls.get('loggedOut'):
                    r.fulfill(status=401, json={'detail': '请登录管理台'})
                    return
                data = user
            elif path == '/auth/options':
                data = {'sms_available': True, 'email_available': True, 'registration_enabled': True}
            elif path == '/auth/login':
                controls['loggedOut'] = False
                data = user
            elif path == '/auth/code/send':
                data = {'challenge_id': 'fixture', 'retry_after': 60}
            elif path == '/profile':
                user.update(r.request.post_data_json)
                data = user
            elif path == '/profile/avatar':
                data = {'avatar_url': '/wechat/avatar.png?v=2'}
            elif path == '/auth/contacts/code/send':
                data = {'challenge_id': 'fixture', 'retry_after': 60}
            elif path == '/auth/contacts':
                body = r.request.post_data_json
                user[body['channel']] = body['identifier']
                data = user
            elif path == '/wechat-config':
                data = dict(token='fixture-token', callback_url=origin + '/callback/fixture', state='configured', revision='fixture')
            elif path == '/settings':
                data = {'model': {'model': 'deepseek-flash', 'timeout': 60, 'max_tokens': 4096}, 'model_key_configured': True}
            elif path == '/models':
                data = {'models': [{'id': 'deepseek-flash', 'name': 'deepseek-flash'}]}
            elif path == '/accounts':
                data = [account]
            elif path.endswith('/online'):
                data = account
            elif path.endswith('/reply-scope'):
                data = {'items': [{'id': 55, 'name': '隔离好友', 'allowed': True, 'pause_until': 0, 'removed_at': None}], 'total': 1, 'page': 1, 'page_size': 10, 'server_time': time.time()}
            elif '/prompt-draft' in path:
                if method == 'PUT':
                    stored.update(r.request.post_data_json)
                    stored['version'] = 'saved'
                if path.endswith('/apply'):
                    account['prompt'] = stored['markdown']
                    stored['base_sha256'] = hashlib.sha256(account['prompt'].encode()).hexdigest()
                    data = {'draft': receipt(), 'account': account, 'changed': True}
                else:
                    data = receipt()
            elif path == '/conversations':
                data = [{'id': 55, 'name': '隔离好友'}]
            else:
                data = {}
            r.fulfill(status=200, json=data)

        def load(view='accounts', width=1440, height=900, **flags):
            nonlocal page
            page.close()
            context.clear_cookies()
            controls.clear()
            controls.update(flags)
            writes.clear()
            errors.clear()
            user.clear()
            user.update(username='验收用户', user_id=1, csrf='fixture', phone='13900000000', email='', sms_available=True, email_available=True)
            account.clear()
            account.update(id=1, nickname='隔离账号', enabled=1, online=1, prompt='原有设定', ai_enabled=False, version=1, status='online')
            stored.clear()
            page = context.new_page()
            page.set_viewport_size({'width': width, 'height': height})
            page.set_default_timeout(7000)
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.route('**/wechat/api/v1/**', route)
            page.add_init_script("sessionStorage.setItem('wechat-page'," + json.dumps(view) + "); window.noticeEvents=[];new MutationObserver(ms=>{for(const m of ms)for(const n of m.addedNodes)if(n.nodeType===1 && n.matches('.toast'))window.noticeEvents.push(n.textContent)}).observe(document,{subtree:true,childList:true});")
            page.goto(origin + '/wechat/')
            page.wait_for_timeout(500)

        def toast(text=None, role=None):
            target = page.locator('.toast')
            expect(target).to_be_visible()
            page.wait_for_function("document.getAnimations().every(a=>a.effect.getComputedTiming().iterations===Infinity||a.playState!=='running')")
            if text:
                expect(target).to_contain_text(text)
            if role:
                expect(target).to_have_attribute('role', role)
            page.wait_for_function("()=>{const e=document.querySelector('.toast');if(!e)return false;const r=e.getBoundingClientRect();return e.getAnimations().length===0 && Math.abs(r.y-24)<1 && Math.abs(r.x+r.width/2-innerWidth/2)<1}")
            assert target.count() == 1 and target.locator('button').count() == 0
            box = target.bounding_box()
            assert abs(box['y'] - 24) < 1 and abs(box['x'] + box['width'] / 2 - page.viewport_size['width'] / 2) < 1
            assert page.locator('.account-profile-feedback,.account-save-status,.reply-scope-error,.assistant-account-notice').count() == 0

        def profile():
            page.get_by_role('button', name='账户：验收用户').click()
            page.get_by_role('button', name='账户设置', exact=True).click()
            page.get_by_role('button', name='修改昵称', exact=True).click()

        def geometry(selector):
            page.wait_for_function("document.getAnimations().every(a=>a.effect.getComputedTiming().iterations===Infinity||a.playState!=='running')")
            return page.locator(selector).evaluate('(e)=>{const r=e.getBoundingClientRect();return [r.x,r.y,r.width,r.height]}')

        def same(a, b):
            assert max((abs(x - y) for x, y in zip(a, b))) <= 1, (a, b)

        def test(name, fn):
            if args.match and args.match not in name:
                return
            try:
                fn()
                assert not errors, errors
                results.append({'name': name, 'passed': True})
            except Exception as e:
                try:
                    page.screenshot(path=str(args.output / (name + '.png')), full_page=True)
                except Exception:
                    pass
                metrics = page.locator('.toast').evaluate_all('es=>es.map(e=>({html:e.outerHTML,box:e.getBoundingClientRect().toJSON(),style:getComputedStyle(e).cssText}))') if not page.is_closed() else []
                results.append({'name': name, 'passed': False, 'error': traceback.format_exc()[-1800:], 'metrics': metrics})
            (args.output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))

        def unchanged():
            load()
            profile()
            before = geometry('.account-dialog')
            page.get_by_role('button', name='保存昵称', exact=True).click()
            toast('昵称未更改', 'status')
            same(before, geometry('.account-dialog'))
            assert page.locator('dialog .toast').count() == 1
            assert not any((w['path'] == '/profile' for w in writes))
            assert page.locator('.account-dialog').evaluate('e=>getComputedStyle(e).overflow') == 'visible'
            page.screenshot(path=str(args.output / 'profile-toast.png'), full_page=True)
            page.wait_for_timeout(4000)
            page.get_by_role('button', name='保存昵称', exact=True).click()
            page.wait_for_timeout(3000)
            toast('昵称未更改')
            page.get_by_role('button', name='关闭账户设置').click()
            toast('昵称未更改')
            assert page.locator('body > .toast').count() == 1
            page.wait_for_timeout(3800)
            expect(page.locator('.toast')).to_have_count(0)
        test('dialog-repeat-timer-rehost-no-layout-shift', unchanged)

        def name_save():
            load()
            profile()
            page.get_by_label('昵称', exact=True).fill('新昵称')
            page.get_by_role('button', name='保存昵称', exact=True).click()
            toast('昵称已保存', 'status')
            assert len([w for w in writes if w['path'] == '/profile']) == 1
            assert user['username'] == '新昵称'
        test('nickname-save-once', name_save)
        for mode in [(400, '未知错误 SECRET_TRACE'), 'network', (429, '操作太频繁，请稍后再试')]:

            def error_case(mode=mode):
                load()
                profile()
                controls['/profile', 'PATCH'] = mode
                page.get_by_label('昵称', exact=True).fill('失败草稿')
                before = geometry('.account-dialog')
                page.get_by_role('button', name='保存昵称', exact=True).click()
                toast(role='alert')
                same(before, geometry('.account-dialog'))
                assert 'SECRET_TRACE' not in page.locator('body').inner_text()
                assert page.get_by_label('昵称', exact=True).input_value() == '失败草稿'
            test('profile-error-' + str(mode[0] if isinstance(mode, tuple) else mode), error_case)

        def avatar():
            load()
            profile()
            before = geometry('.account-dialog')
            page.locator('input[type=file]').set_input_files({'name': 'bad.txt', 'mimeType': 'text/plain', 'buffer': b'bad'})
            toast('请选择 PNG')
            same(before, geometry('.account-dialog'))
            assert not any((w['path'] == '/profile/avatar' for w in writes))
            oversized = args.output / 'big.png'
            try:
                with oversized.open('wb') as output:
                    output.truncate(5 * 1024 * 1024 + 1)
                page.locator('input[type=file]').set_input_files(str(oversized))
                toast('头像不能超过5MiB')
            finally:
                oversized.unlink(missing_ok=True)
        test('avatar-validation-no-layout-shift', avatar)

        def contact():
            load()
            profile()
            page.get_by_role('button', name='绑定邮箱', exact=True).click()
            page.get_by_label('邮箱', exact=True).fill('fixture@example.com')
            before = geometry('.account-dialog')
            page.get_by_role('button', name='获取验证码', exact=True).click()
            toast('验证码已发送', 'status')
            same(before, geometry('.account-dialog'))
            page.get_by_label('邮箱验证码', exact=True).fill('123456')
            page.get_by_role('button', name='确认绑定', exact=True).click()
            toast('邮箱已绑定', 'status')
            assert len([w for w in writes if w['path'] == '/auth/contacts']) == 1
        test('contact-code-and-save-top-notice', contact)

        def model():
            load('settings')
            before = geometry('.model-settings')
            page.get_by_role('button', name='保存模型设置').click()
            toast('模型设置已保存', 'status')
            same(before, geometry('.model-settings'))
            controls['/settings', 'PATCH'] = (400, '模型名称不能为空')
            page.get_by_role('button', name='保存模型设置').click()
            toast('请填写模型名称', 'alert')
            same(before, geometry('.model-settings'))
        test('model-success-error-stable-geometry', model)

        def validation():
            load()
            profile()
            page.get_by_label('昵称', exact=True).fill('')
            page.locator('.account-profile-form').evaluate('form=>form.requestSubmit()')
            toast('请填写昵称', 'alert')
            assert page.get_by_label('昵称', exact=True).evaluate('e=>e===document.activeElement')
            assert not any((w['path'] == '/profile' for w in writes))
        test('native-validation-focus-and-chinese-toast', validation)

        def refresh_failure():
            load('settings')
            controls['/settings', 'GET'] = (503, 'SECRET_TRACE')
            page.get_by_role('button', name='保存模型设置').click()
            toast('操作已完成，请刷新页面', 'status')
            assert len([w for w in writes if w['path'] == '/settings']) == 1
        test('save-success-read-failure-not-write-failure', refresh_failure)

        def token():
            load('settings')
            before = geometry('.wechat-settings')
            controls['/wechat-config', 'PUT'] = (400, '官方回调绑定失败，请检查Token')
            page.get_by_role('button', name='保存并自动绑定').click()
            toast(role='alert')
            same(before, geometry('.wechat-settings'))
            controls['/wechat-config', 'GET'] = (503, 'SECRET_TRACE')
            expect(page.locator('.wechat-settings .badge')).to_have_text('读取失败', timeout=8000)
            same(before, geometry('.wechat-settings'))
            controls.pop(('/wechat-config', 'GET'))
            expect(page.locator('.wechat-settings .badge')).to_have_text('已绑定', timeout=8000)
        test('wechat-config-auto-retry-stable-caption', token)

        def polling():
            load('automation')
            expect(page.get_by_role('switch', name='允许 隔离好友')).to_be_enabled()
            before = geometry('.reply-scope')
            controls['/accounts/1/reply-scope', 'GET'] = (503, 'SECRET_TRACE')
            toast(role='alert')
            same(before, geometry('.reply-scope'))
            expect(page.get_by_role('switch', name='允许 隔离好友')).to_be_disabled()
            assert page.get_by_text('隔离好友', exact=True).count() == 1
            count = page.evaluate('noticeEvents.length')
            page.wait_for_timeout(7200)
            assert page.evaluate('noticeEvents.length') == count
            expect(page.locator('.toast')).to_have_count(0)
            assert page.get_by_role('button', name='刷新回复范围').count() == 0
            controls.pop(('/accounts/1/reply-scope', 'GET'))
            expect(page.get_by_role('switch', name='允许 隔离好友')).to_be_enabled()
        test('scope-poll-dedupe-cache-disabled-and-retry', polling)

        def login():
            load(loggedOut=True)
            expect(page.locator('form').get_by_role('button', name='登录', exact=True)).to_be_visible()
            controls['/auth/code/send', 'POST'] = (429, '操作太频繁，请稍后再试')
            page.get_by_label('邮箱或手机号').fill('13900000000')
            page.get_by_role('button', name='获取验证码', exact=True).click()
            toast(role='alert')
        test('login-rate-limit-top-notice', login)

        def expire():
            load('settings')
            page.get_by_role('button', name='保存模型设置').click()
            toast('模型设置已保存')
            controls['/wechat-config', 'GET'] = (401, '请登录管理台')
            expect(page.locator('form').get_by_role('button', name='登录', exact=True)).to_be_visible(timeout=8000)
            toast('登录已过期', 'alert')
            assert '模型设置已保存' not in page.locator('body').inner_text()
        test('session-expiry-clears-old-result', expire)

        def reduced_motion():
            load()
            page.emulate_media(reduced_motion='reduce')
            profile()
            page.get_by_role('button', name='保存昵称', exact=True).click()
            toast('昵称未更改')
            assert page.locator('.toast').evaluate('e=>e.getAnimations().length') == 0
            page.emulate_media(reduced_motion='no-preference')
        test('reduced-motion-toast', reduced_motion)

        def auth_flow():
            load(loggedOut=True)
            expect(page.get_by_role('button', name='注册', exact=True)).to_have_count(0)
            expect(page.get_by_label('用户名', exact=True)).to_have_count(0)
            page.get_by_label('邮箱或手机号').fill('fixture@example.com')
            page.get_by_role('button', name='获取验证码', exact=True).click()
            toast('验证码已发送', 'status')
            page.get_by_label('验证码', exact=True).fill('123456')
            page.locator('form button.primary').click()
            expect(page.get_by_role('button', name='账户：验收用户')).to_be_visible()
            expect(page.locator('.toast')).to_have_count(0, timeout=8000)
        test('login-success-clears-notice', auth_flow)
        for width, height in [(1440, 900), (1366, 768), (390, 844), (320, 740), (390, 500)]:

            def responsive(width=width, height=height):
                load(width=width, height=height)
                profile()
                before = geometry('.account-dialog')
                page.get_by_role('button', name='保存昵称', exact=True).click()
                toast('昵称未更改')
                same(before, geometry('.account-dialog'))
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert page.locator('.account-dialog').evaluate('e=>e.scrollWidth<=e.clientWidth')
                page.screenshot(path=str(args.output / f'profile-{width}x{height}.png'), full_page=True)
                page.get_by_role('button', name='关闭账户设置').click()
                page.get_by_role('button', name='智能助手', exact=True).click()
                expect(page.locator('.reply-scope')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            test(f'responsive-{width}x{height}', responsive)
        context.close()
        browser.close()
    server.shutdown()
    print(json.dumps({'passed': sum((x['passed'] for x in results)), 'total': len(results), 'failed': [x for x in results if not x['passed']]}, ensure_ascii=False))
    assert all((x['passed'] for x in results))
if __name__ == '__main__':
    main()
