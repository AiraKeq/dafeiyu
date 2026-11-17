"""Preserve expired login data and prepare a clean QR login when QQ service gets stuck."""
import datetime
import json
import os
from pathlib import Path
import secrets
import subprocess

ROOT = Path('/opt/qq-deepseek')
os.umask(0o077)
profile = (ROOT / 'data/napcat/qq').resolve()
expected = Path('/opt/qq-deepseek/data/napcat/qq')
if profile != expected or not profile.is_dir():
    raise SystemExit('QQ profile target did not match the intended deployment.')
backup = profile.with_name('qq-before-relogin-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
subprocess.run(['docker', 'compose', 'stop', 'napcat'], cwd=str(ROOT), check=True)
try:
    profile.rename(backup)
    profile.mkdir(mode=0o700)
    token = secrets.token_hex(24)
    webui_path = ROOT / 'data/napcat/config/webui.json'
    webui = json.loads(webui_path.read_text(encoding='utf-8-sig'))
    webui['autoLoginAccount'] = ''
    webui['token'] = token
    webui_path.write_text(json.dumps(webui, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(str(webui_path), 0o600)
    env_path = ROOT / '.env'
    lines = env_path.read_text().splitlines()
    lines = [('NAPCAT_WEBUI_TOKEN=' + token) if line.startswith('NAPCAT_WEBUI_TOKEN=') else line for line in lines]
    env_path.write_text('\n'.join(lines) + '\n')
    os.chmod(str(env_path), 0o600)
    print('Old QQ data preserved in:', backup.name)
    print('WebUI credential rotated; its value has not been printed.')
finally:
    subprocess.run(['docker', 'compose', 'up', '-d', '--force-recreate', 'napcat'], cwd=str(ROOT), check=True)
