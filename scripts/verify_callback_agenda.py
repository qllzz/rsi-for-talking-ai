"""Isolated browser acceptance for callback scheduling; never writes to the demo database."""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright


def run():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory() as folder:
        env = {**os.environ, 'ZHIYU_RUNTIME':folder, 'ZHIYU_TTS_ENGINE':'melo',
               'ZHIYU_AGENT_CONFIG':str(Path(folder)/'unused.json'), 'ZHIYU_AGENT_URL':''}
        with open(Path(folder)/'server.log','wb') as log:
            server = subprocess.Popen([sys.executable,'-m','uvicorn','zhiyu.app:api','--host','127.0.0.1','--port',str(port)],env=env,stdout=log,stderr=log)
            try:
                with httpx.Client(base_url=url,trust_env=False) as c:
                    for _ in range(100):
                        if server.poll() is not None: raise RuntimeError('Isolated service failed to start')
                        try:
                            if c.get('/api/status').is_success: break
                        except httpx.TransportError: pass
                        time.sleep(.2)
                    def post(path,body):
                        r=c.post(path,json=body);r.raise_for_status();return r.json()
                    sid=post('/api/text',{'case_id':'POP-001'})['session_id']; result=post(f'/api/text/{sid}/turn',{'text':'现在不方便，晚点再打'})
                    ticket=result['ticket']; assert ticket
                    with sync_playwright() as p:
                        executable = os.getenv('CHROME_PATH') or shutil.which('google-chrome')
                        if not executable and os.name=='nt': executable=r'C:\Program Files\Google\Chrome\Application\chrome.exe'
                        b=p.chromium.launch(executable_path=executable); page=b.new_page();page.goto(url)
                        page.get_by_role('button',name='回电待办',exact=True).click(); row=page.locator(f'[data-ticket="{ticket}"]');expect(row).to_contain_text('未排期');row.get_by_text('安排时间').click();expect(page.locator('#agenda-utterance')).to_contain_text('现在不方便')
                        when=datetime.now()+timedelta(days=1);page.locator('#agenda-time').fill(when.strftime('%Y-%m-%dT%H:%M'));page.locator('#agenda-save').click();expect(row).to_contain_text('未到期');expect(row.get_by_text('文字回联')).to_be_disabled()
                        evidence=Path('runtime/callback-check'); evidence.mkdir(parents=True,exist_ok=True); page.screenshot(path=evidence/'scheduled.png')
                        blocked=c.post('/api/text',json={'case_id':'POP-001','followup_ticket':ticket});assert not blocked.is_success
                        page.reload();page.get_by_role('button',name='回电待办',exact=True).click();expect(row).to_contain_text('未到期')
                        page.once('dialog',lambda d:d.accept());row.get_by_text('取消排期').click();expect(row).to_contain_text('未排期');row.get_by_text('文字回联').click();expect(page.locator('#message-input')).to_be_visible();page.locator('#message-input').fill('我是负责人');page.locator('#text-send').click();expect(page.locator('.msg.assistant').last).to_contain_text('方便了解');page.locator('#message-input').fill('可以，请说吧');page.locator('#text-send').click();expect(page.locator('.msg.assistant').last).to_contain_text('这次驳回');page.locator('#message-input').fill('我明白了，会补齐材料');page.locator('#text-send').click();expect(page.locator('#outcome')).to_have_text('已通知');page.get_by_role('button',name='回电待办',exact=True).click();expect(row).to_have_count(0);b.close()
                        print('PASS: callback -> scheduled -> early claim rejected -> persisted after reload -> unscheduled -> manual follow-up -> notified and removed')
            finally:
                if server.poll() is None:
                    if os.name=='nt':
                        # A Windows venv launcher has a child interpreter holding the SQLite/log handles.
                        subprocess.run(['taskkill','/PID',str(server.pid),'/T','/F'],check=True,stdout=subprocess.DEVNULL)
                    else: server.terminate()
                    server.wait(timeout=15)


if __name__=='__main__': run()
