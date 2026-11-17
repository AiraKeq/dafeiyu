"""Keep dashboard credentials in a local user-only file, outside chat output."""
import json
from pathlib import Path
import secrets
import subprocess

key = Path.home() / '.ssh/qq_deepseek_20261113'
remote_code = '''
import json, subprocess
from pathlib import Path
root = Path('/opt/qq-deepseek')
webui = json.loads((root/'data/napcat/config/webui.json').read_text(encoding='utf-8-sig'))
print(json.dumps({'napcat_token': webui['token']}))
'''
result = subprocess.run(
    ['ssh', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
     'root@8.217.178.5', 'python3 -'],
    input=remote_code, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
)
data = json.loads(result.stdout)
password = 'Qq7_' + secrets.token_urlsafe(24)
subprocess.run(
    ['ssh', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
     'root@8.217.178.5', 'cd /opt/qq-deepseek && docker compose stop astrbot'], check=True,
)
try:
    password_result = subprocess.run(
        ['ssh', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
         'root@8.217.178.5', 'cd /opt/qq-deepseek && docker compose run --rm --no-deps -T --entrypoint astrbot astrbot password'],
        # Binary input preserves LF. Windows text mode adds CR, which Click stores in hidden input.
        input=(password + '\n' + password + '\n').encode('utf-8'),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
finally:
    subprocess.run(
        ['ssh', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
         'root@8.217.178.5', 'cd /opt/qq-deepseek && docker compose up -d astrbot'], check=True,
    )
if password_result.returncode != 0:
    raise SystemExit('Built-in password setup failed; its output was hidden to protect credentials.')
destination = Path.home() / '.ssh/qq-deepseek-panels.txt'
destination.write_text(
    'QQ DeepSeek 管理信息（仅保存在本机，请勿转发）\n\n'
    'AstrBot：http://127.0.0.1:6185/\n用户名：astrbot\n密码：' + password + '\n\n'
    'NapCat：http://127.0.0.1:6099/webui/\nToken：' + data['napcat_token'] + '\n\n'
    '服务器：root@8.217.178.5\n部署目录：/opt/qq-deepseek\n'
    'QQ群：1000000004\n机器人QQ：1000000001\n管理员QQ：1000000002\n', encoding='utf-8-sig',
)
subprocess.run(['icacls', str(destination), '/inheritance:r', '/grant:r',
                'ASUS:(F)', 'SYSTEM:(F)'], check=True, stdout=subprocess.DEVNULL)
print('Credentials saved to', destination, '(values hidden).')
