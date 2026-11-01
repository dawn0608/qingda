"""仅服务器隔离浏览器：设置提交不灰化其他区域，覆盖回执、旧读取及防重入。"""
import argparse
import copy
import http.server
import json
import threading
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import expect, sync_playwright

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dist', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    dist = args.dist.resolve()

    class Handler(http.server.SimpleHTTPRequestHandler):

        def translate_path(self, path):
            p = (dist / urlsplit(path).path.removeprefix('/wechat/')).resolve()
            return str(p if p.is_relative_to(dist) and p.is_file() else dist / 'index.html')

        def log_message(self, *_):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = dict(model=dict(thinking_enabled=False, web_search_enabled=True), model_revision=1, model_key_configured=True, asr=dict(key_configured=True, model='qwen-audio-3.1-asr-flash'))
    config = dict(token='fixture-token', state='configured', revision='1', callback_url='https://203.0.113.10/wechatapi/callback/' + 'Ab3xQ_9z' * 6)
    devices = [dict(id='fixture-pc', name='隔离电脑', status='connected', version='0.6.1', last_seen=1)]
    writes, reads, held_writes, held_reads, checks, errors = ([], [], [], [], [], [])
    flags = dict(hold_write=False, hold_read='', fail_write=False)

    def check(name, passed):
        checks.append(dict(name=name, passed=bool(passed)))
        (args.output / 'progress.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
        assert passed, name

    def finish(r, path, body):
        if flags['fail_write']:
            flags['fail_write'] = False
            r.fulfill(status=400, json=dict(error='请检查填写内容'))
            return
        if path == '/settings':
            settings['model'].update(body.get('model', {}))
            settings['model_revision'] += 1
            data = settings
        elif path == '/wechat-config':
            config.update(token=body['token'], revision=str(int(config['revision']) + 1))
            data = config
        else:
            data = dict(state='approved', name='隔离电脑')
        r.fulfill(json=data)

    def route(r):
        path = urlsplit(r.request.url).path.split('/api/v1')[-1]
        if r.request.method != 'GET':
            body = r.request.post_data_json or {}
            writes.append(dict(path=path, body=body))
            if flags['hold_write']:
                held_writes.append((r, path, body))
            else:
                finish(r, path, body)
            return
        reads.append(path)
        if path == '/auth/session':
            data = dict(user_id=1, username='隔离用户', csrf='fixture')
        elif path == '/accounts':
            data = [dict(id=1, enabled=True, nickname='隔离微信')]
        elif path == '/settings':
            data = settings
        elif path == '/wechat-config':
            data = config
        elif path == '/conversations':
            data = [dict(id=11, name='好友一'), dict(id=12, name='好友二')]
        else:
            data = {}
        if flags['hold_read'] == path:
            held_reads.append((r, copy.deepcopy(data)))
        else:
            r.fulfill(json=data)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport=dict(width=1440, height=900))
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("sessionStorage.setItem('wechat-page','settings')")
        page.add_init_script("window.callbackCopies=[];Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>window.callbackCopies.push(value)}})")
        page.route('**/wechat/api/v1/**', route)
        page.goto(f'http://127.0.0.1:{server.server_port}/wechat/')
        first = page.get_by_label('DeepSeek API Key', exact=True)
        second = page.get_by_label('阿里云百炼 API Key', exact=True)
        token = page.locator('.wechat-settings input:not([readonly])')
        page.wait_for_timeout(250)
        callback = page.locator('.callback-copy')
        callback.click()
        callback.press('Enter')
        callback.press('Space')
        check('单行地址点击及键盘复制完整值', page.evaluate('callbackCopies') == [config['callback_url']] * 3)

        def snapshot(selector):
            return page.locator(selector).evaluate_all("els => els.flatMap(e => [e,...e.querySelectorAll('input,button')]).map(e => {\n                const s=getComputedStyle(e),r=e.getBoundingClientRect();return {tag:e.tagName,disabled:!!e.disabled,\n                opacity:s.opacity,color:s.color,bg:s.background,rect:[r.x,r.y,r.width,r.height]};})")

        def hold():
            flags['hold_write'] = True
            return len(writes)

        def release():
            flags['hold_write'] = False
            assert len(held_writes) == 1
            finish(*held_writes.pop())
        untouched = '.wechat-settings,.account-trigger'
        before = snapshot(untouched)
        first.fill('model-draft')
        second.fill('asr-draft')
        page.evaluate("window.settingsNode=document.querySelector('.settings-page')")
        count = hold()
        page.get_by_role('form', name='DeepSeek 密钥设置', exact=True).evaluate('e=>{e.requestSubmit();e.requestSubmit()}')
        expect(first).to_have_attribute('readonly', '')
        second.fill('new-asr-draft')
        check('仅冻结提交字段且密钥保存防重入', len(writes) == count + 1 and second.is_editable())
        check('密钥提交期间其他面板保持原样', snapshot(untouched) == before)
        release()
        expect(first).to_have_value('')
        check('成功仅清空已提交密钥', second.input_value() == 'new-asr-draft' and writes[-1]['body'] == dict(model_key='model-draft'))
        count = len(writes)
        page.get_by_role('button', name='保存 DeepSeek 密钥', exact=True).click()
        expect(page.get_by_text('请填写 DeepSeek API 密钥', exact=True)).to_be_visible()
        check('文本密钥留空不误提交另一组草稿', len(writes) == count and second.input_value() == 'new-asr-draft')
        flags['fail_write'] = True
        page.get_by_role('button', name='保存语音密钥', exact=True).click()
        expect(page.get_by_text('请检查填写内容', exact=True)).to_be_visible()
        check('密钥失败保留输入', second.input_value() == 'new-asr-draft')
        first.fill('new-model-draft')
        second.press('Enter')
        expect(second).to_have_value('')
        check('语音回车仅保存自身且保留文本密钥草稿', writes[-1]['body'] == dict(asr_key='new-asr-draft') and first.input_value() == 'new-model-draft')
        second.fill('new-asr-draft')
        flags['hold_read'] = '/settings'
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        page.wait_for_timeout(100)
        assert len(held_reads) == 1
        old_reads = reads.count('/settings')
        page.evaluate("for(let i=0;i<5;i++)window.dispatchEvent(new Event('focus'))")
        page.wait_for_timeout(100)
        check('读取期间多次焦点事件不重叠', reads.count('/settings') == old_reads)
        flags['hold_read'] = ''
        r, old = held_reads.pop()
        r.fulfill(json=old)
        page.wait_for_timeout(150)
        check('自动读取保留密钥草稿', first.input_value() == 'new-model-draft' and second.input_value() == 'new-asr-draft')
        stable = snapshot('.model-settings,.account-trigger')
        count = hold()
        page.locator('.wechat-settings form').evaluate('e=>{e.requestSubmit();e.requestSubmit()}')
        check('微信绑定不灰化其他区域且同步防重入', snapshot('.model-settings,.account-trigger') == stable and len(writes) == count + 1)
        release()
        expect(page.get_by_text('绑定成功', exact=True)).to_be_visible()
        page.evaluate("Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'))")
        old_reads = len(reads)
        page.wait_for_timeout(5300)
        check('隐藏页面暂停读取', len(reads) == old_reads)
        page.evaluate("Object.defineProperty(document,'hidden',{value:false,configurable:true});document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(150)
        check('回到可见页面立即恢复读取', len(reads) > old_reads)
        for width, height in [(1864, 893), (1440, 900), (1366, 768), (1280, 720), (1024, 768), (390, 844), (320, 640), (1440, 640)]:
            page.set_viewport_size(dict(width=width, height=height))
            page.locator('.settings-page').evaluate('e=>e.scrollTop=0')
            page.wait_for_timeout(250)
            check(f'{width}x{height}无横向溢出', page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            check(f'{width}x{height}地址只读且固定单行', callback.evaluate("e=>e.tagName==='INPUT'&&e.readOnly&&e.type==='text'&&e.getBoundingClientRect().height===36&&e.scrollHeight<=e.clientHeight"))
            if width == 1864:
                check('常用桌面宽度完整展示单行地址', callback.evaluate('e=>e.scrollWidth<=e.clientWidth'))
            if width == 320:
                callback.press('End')
                check('窄屏可在单行框内查看地址尾部', callback.evaluate('e=>e.scrollLeft>0') and callback.input_value() == config['callback_url'])
                callback.press('Home')
            check(f'{width}x{height}三个独立配置表单和按钮', page.locator('.model-provider-section').count() == 3 and page.get_by_role('button', name='保存 DeepSeek 密钥', exact=True).count() == 1 and (page.get_by_role('button', name='保存语音密钥', exact=True).count() == 1))
            geometry = page.evaluate("() => {\n              const box=s=>{const r=document.querySelector(s).getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,b:r.bottom,h:r.height}};\n              const w=box('.wechat-settings'),m=box('.model-settings');\n              return {w,m,headers:Array.from(document.querySelectorAll('.settings-grid > .panel > .panel-heading')).map(e=>e.getBoundingClientRect().height),\n                clipped:Array.from(document.querySelectorAll('.wechat-settings input,.wechat-settings textarea,.model-settings input,.model-settings button')).some(e=>{const p=e.closest('.panel').getBoundingClientRect(),r=e.getBoundingClientRect();return r.left<p.left-1||r.right>p.right+1||r.bottom>p.bottom+1})};\n            }")
            check(f'{width}x{height}标题栏高度保留且字段按钮不裁切', all((abs(h - (40 if width <= 780 else 44)) < 1 for h in geometry['headers'])) and (not geometry['clipped']))
            if width >= 1280:
                ratio = geometry['w']['w'] / (geometry['w']['w'] + geometry['m']['w'])
                check(f'{width}x{height}上下两面板等宽且间距24px', .49 < ratio < .51 and abs(geometry['m']['y']-geometry['w']['b']-24)<1)
                top_gap = page.locator('.settings-grid').evaluate("e=>e.getBoundingClientRect().top-document.querySelector('.topbar').getBoundingClientRect().bottom")
                page.locator('.settings-page').evaluate('e=>e.scrollTop=e.scrollHeight')
                bottom_gap = page.locator('.settings-grid').evaluate("e=>document.querySelector('.settings-page').getBoundingClientRect().bottom-e.getBoundingClientRect().bottom")
                check(f'{width}x{height}上边距24px且底部留白至少24px', abs(top_gap - 24) < 1 and bottom_gap >= 23)
                page.locator('.settings-page').evaluate('e=>e.scrollTop=0')
            page.screenshot(path=str(args.output / f'settings-{width}.png'), full_page=True)
        check('无浏览器异常', not errors)
        browser.close()
    server.shutdown()
    result = dict(checks=checks, errors=errors, writes=writes)
    (args.output / 'browser.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(passed=len(checks), errors=errors), ensure_ascii=False))
if __name__ == '__main__':
    main()
