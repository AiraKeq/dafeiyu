"""Print deployment status without exposing credentials or chat logs."""
import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path('/opt/qq-deepseek')


def redact(value):
    if isinstance(value, dict):
        return {
            key: '[REDACTED]' if re.search(r'key|token|password|secret', key, re.I) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


for path in (ROOT / 'data/astrbot/cmd_config.json', ROOT / 'data/napcat/config/webui.json'):
    if path.exists():
        print(str(path))
        print(json.dumps(redact(json.loads(path.read_text(encoding='utf-8-sig'))), ensure_ascii=False, indent=2))

for service in ('astrbot', 'napcat'):
    result = subprocess.run(
        ['docker', 'compose', 'logs', '--no-color', '--tail', '35', service],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    print('STARTUP LOG:', service)
    for line in result.stdout.splitlines():
        if re.search(r'password|token|secret|密码|密钥|qrcode|二维码|http.*\?', line, re.I):
            print('[credential or login line hidden]')
        else:
            print(line)
