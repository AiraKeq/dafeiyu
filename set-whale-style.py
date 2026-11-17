"""Apply only whale_memory.base_style through the native authenticated hot-reload API.

Credentials are read locally for this operation and never printed or persisted.
No core config, permissions, provider credentials, or QQ data are changed.
"""
import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
if not (ROOT / 'plugins').is_dir():
    ROOT = ROOT.parent
BASE = 'http://127.0.0.1:6185'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--prompt', type=Path, default=ROOT / 'persona-魔丸风格.txt')
parser.add_argument('--check-only', action='store_true')
parser.add_argument('--group', default='1000000003', help='Expected configured group; readback guard only')
args = parser.parse_args()
prompt = args.prompt.read_text(encoding='utf-8-sig').strip()
if not prompt or len(prompt) > 6000:
    raise SystemExit('Style prompt must contain 1 to 6000 characters.')
saved = (Path.home() / '.ssh/qq-deepseek-panels.txt').read_text(encoding='utf-8-sig')
password = next(line for line in saved.splitlines() if line.startswith('密码：')).split('：', 1)[1]


def request(path, method='GET', body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None)
    with urllib.request.urlopen(req, timeout=90) as response:
        payload = json.load(response)
    if payload.get('status') != 'ok':
        raise RuntimeError('Dashboard API rejected the operation.')
    return payload


try:
    login = request('/api/auth/login', 'POST', {'username': 'astrbot', 'password': password})
    token = login['data']['token']
    before = request('/api/v1/plugins/whale_memory/config', token=token)['data']['config']
    if before.get('group_ids') != [args.group] or before.get('admin_ids') != ['1000000002']:
        raise RuntimeError('Unexpected plugin group or administrator; nothing was changed.')
    if args.check_only:
        print('Live style equals approved prompt:', before.get('base_style') == prompt)
        print('Style characters:', len(before.get('base_style') or ''))
        raise SystemExit(0 if before.get('base_style') == prompt else 1)
    after = copy.deepcopy(before)
    after['base_style'] = prompt
    backup_dir = ROOT / '.tools/style-backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / ('before-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
    with backup.open('x', encoding='utf-8') as handle:
        json.dump(before, handle, ensure_ascii=False, indent=2)
    response = request('/api/v1/plugins/whale_memory/config', 'PUT', {'config': after}, token)
    observed = request('/api/v1/plugins/whale_memory/config', token=token)['data']['config']
    changed = sorted(key for key in set(before) | set(observed) if before.get(key) != observed.get(key))
    if observed.get('base_style') != prompt or any(key != 'base_style' for key in changed):
        raise RuntimeError('Live configuration readback did not match the intended single-field update.')
    print('Native save and plugin hot reload: OK')
    print('Changed fields:', changed)
    print('Live style characters:', len(prompt))
    print('Old plugin configuration saved privately:', backup.name)
    print('Other plugin rules, credentials and QQ account were not changed.')
except urllib.error.HTTPError as error:
    print('Dashboard API HTTP error:', error.code)
    raise SystemExit(1)
except Exception as error:
    print('Style update failed:', type(error).__name__)
    raise SystemExit(1)
