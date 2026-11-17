"""Verify the saved management password without revealing its value or hash."""
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

saved = (Path.home() / '.ssh/qq-deepseek-panels.txt').read_text(encoding='utf-8-sig')
password = next(line for line in saved.splitlines() if line.startswith('密码：')).split('：', 1)[1]
remote = '''python3 -c 'import json; d=json.load(open("/opt/qq-deepseek/data/astrbot/cmd_config.json",encoding="utf-8-sig"))["dashboard"]; print(json.dumps({"password":d["password"],"pbkdf2":d["pbkdf2_password"],"username":d["username"],"required":d["password_change_required"]}))' '''
result = subprocess.run(
    ['ssh', '-i', str(Path.home() / '.ssh/qq_deepseek_20261113'), '-o', 'IdentitiesOnly=yes',
     '-o', 'BatchMode=yes', 'root@8.217.178.5', remote],
    capture_output=True, text=True, check=True,
)
state = json.loads(result.stdout)
print('Password username:', state['username'])
print('Password change required:', state['required'])
for suffix, label in (('', 'plain'), ('\r', 'trailing CR'), ('\n', 'trailing LF')):
    print('Stored MD5 matches', label + ':', hashlib.md5((password + suffix).encode()).hexdigest() == state['password'])
parts = state['pbkdf2'].split('$')
if len(parts) == 4:
    computed = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(parts[2]), int(parts[1])).hex()
    print('Stored PBKDF2 matches plain password:', computed == parts[3])
request = urllib.request.Request(
    'http://127.0.0.1:6185/api/auth/login',
    data=json.dumps({'username': 'astrbot', 'password': password}).encode(),
    headers={'Content-Type': 'application/json'},
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    print('Dashboard login:', result.get('status'), result.get('message'))
except urllib.error.HTTPError as error:
    print('Dashboard login HTTP:', error.code)
    data = json.load(error)
    print('Dashboard message:', data.get('message'))
    raise SystemExit(1)
