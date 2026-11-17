"""Read QQ login status or refresh its QR code without printing credentials."""
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

ROOT = Path('/opt/qq-deepseek')
config = json.loads((ROOT / 'data/napcat/config/webui.json').read_text(encoding='utf-8-sig'))


def post(path, body, credential=None):
    headers = {'Content-Type': 'application/json'}
    if credential:
        headers['Authorization'] = 'Bearer ' + credential
    request = urllib.request.Request(
        'http://127.0.0.1:6099' + path,
        data=json.dumps(body).encode(), headers=headers,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


login = post('/api/auth/login', {
    'hash': hashlib.sha256((config['token'] + '.napcat').encode()).hexdigest(),
})
credential = (login.get('data') or {}).get('Credential')
if not credential:
    raise SystemExit('NapCat authentication failed; credential data hidden.')
if 'reset-auto-login' in sys.argv:
    response = post('/api/QQLogin/SetQuickLoginQQ', {'uin': ''}, credential)
    print('Automatic login temporarily cleared for a new QR session:', response.get('code') == 0)
elif 'qr-data' in sys.argv:
    response = post('/api/QQLogin/GetQQLoginQrcode', {}, credential)
    value = (response.get('data') or {}).get('qrcode')
    if response.get('code') != 0 or not isinstance(value, str) or not value:
        raise SystemExit('No live QQ login QR data was returned.')
    import os
    os.umask(0o077)
    (ROOT / 'qr-data.json').write_text(json.dumps({'qrcode': value}), encoding='utf-8')
    print('Live QR data saved privately; its value was not printed.')
elif 'auto-login' in sys.argv:
    response = post('/api/QQLogin/GetQQLoginInfo', {}, credential)
    info = response.get('data') or {}
    account = str(info.get('uin') or '')
    if not account.isdigit():
        raise SystemExit('No logged-in QQ account was returned; auto-login was not changed.')
    response = post('/api/QQLogin/SetQuickLoginQQ', {'uin': account}, credential)
    print('QQ account:', account, 'online:', info.get('online'))
    print('Auto-login saved:', response.get('code') == 0)
elif 'refresh' in sys.argv:
    response = post('/api/QQLogin/RefreshQRcode', {}, credential)
    print('QR refresh result:', response.get('code'), response.get('message', ''))
else:
    response = post('/api/QQLogin/CheckLoginStatus', {}, credential)
    data = response.get('data') or {}
    print(json.dumps({key: data.get(key) for key in (
        'isLogin', 'isOffline', 'loginPhase', 'qrLoginAccepted', 'coreReady', 'loginError',
    )}, ensure_ascii=False))
