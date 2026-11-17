"""Verify the deployed services; only emit non-secret connection results."""
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path('/opt/qq-deepseek')
config = json.loads((ROOT / 'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
source = next(item for item in config['provider_sources'] if item['id'] == 'deepseek')
model = next(item for item in config['provider'] if item['id'] == 'deepseek/deepseek-flash')
print('Administrator:', config['admins_id'])
print('Whitelist:', config['platform_settings']['id_whitelist'])
print('Model:', model['model'], 'enabled:', model['enable'])
print('API key present:', bool(source['key']))

logs = subprocess.check_output(
    ['docker', 'compose', 'logs', '--no-color', '--tail', '300', 'astrbot'], cwd=str(ROOT),
).decode('utf-8', errors='replace')
print('OneBot connected event seen:', '适配器已连接' in logs)
print('Model provider loaded:', 'openai_chat_completion(deepseek/deepseek-flash)' in logs)
metadata = (ROOT / 'data/astrbot/plugins/astrbot_plugin_whale_memory/metadata.yaml').read_text(encoding='utf-8')
plugin_version = next(line.split(':', 1)[1].strip() for line in metadata.splitlines() if line.startswith('version:'))
print('Memory plugin version:', plugin_version, 'load event seen:', 'whale_memory (' + plugin_version + ')' in logs)
print('Provider startup errors seen:', any(
    phrase in logs for phrase in ('载入提供商失败', 'Failed to load provider', 'Traceback (most recent call last)')
))

if '--model-test' in sys.argv:
    if not source['key']:
        raise SystemExit('Waiting for the user to enter the API key.')
    request = urllib.request.Request(
        source['api_base'].rstrip('/') + '/chat/completions',
        data=json.dumps({
            'model': model['model'],
            'messages': [{'role': 'user', 'content': '请只回复四个字：连接成功'}],
            'max_tokens': 32, 'thinking': {'type': 'disabled'},
        }).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + source['key'][0]},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.load(response)
        print('DeepSeek reply:', payload['choices'][0]['message']['content'])
        print('Usage:', payload.get('usage', {}))
    except urllib.error.HTTPError as error:
        print('DeepSeek request failed with HTTP', error.code)
        try:
            payload = json.load(error)
            print('Error type:', payload.get('error', {}).get('type'))
            print('Error code:', payload.get('error', {}).get('code'))
        except Exception:
            pass
        raise SystemExit(1)
