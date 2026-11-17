"""Configure this deployment. API keys enter through stdin and are never printed."""
import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

ROOT = Path('/opt/qq-deepseek')
CONFIG = ROOT / 'data/astrbot/cmd_config.json'
MODEL_ID = 'deepseek/deepseek-flash'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path, value):
    pending = path.with_suffix(path.suffix + '.pending')
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(str(pending), 0o600)
    pending.replace(path)


if '--set-key' in sys.argv:
    api_key = sys.stdin.read().strip()
    if not api_key.startswith('sk-') or len(api_key) < 16 or any(c.isspace() for c in api_key):
        raise SystemExit('Key format was not accepted; no configuration was changed.')
    subprocess.run(['docker', 'compose', 'stop', 'astrbot'], cwd=str(ROOT), check=True)
    config = read_json(CONFIG)
    source = next(s for s in config['provider_sources'] if s['id'] == 'deepseek')
    source['key'] = [api_key]
    source['enable'] = True
    for provider in config['provider']:
        if provider['id'] == MODEL_ID:
            provider['enable'] = True
    config['agent_runner']['config']['model']['provider_id'] = MODEL_ID
    write_json(CONFIG, config)
    subprocess.run(['docker', 'compose', 'up', '-d', 'astrbot'], cwd=str(ROOT), check=True)
    print('DeepSeek key saved on the server. Its value has not been printed.')
else:
    marker = ROOT / '.bootstrap-complete'
    if marker.exists():
        raise SystemExit('Bootstrap is already complete. Use the dashboard or --set-key for later changes.')
    os.umask(0o077)
    backup = ROOT / 'backups' / datetime.datetime.now().strftime('initial-%Y%m%d-%H%M%S')
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(CONFIG), str(backup / 'cmd_config.json'))
    config = read_json(CONFIG)
    settings = config['platform_settings']
    settings.update({
        'enable_id_white_list': True,
        'id_whitelist': [
            'aiocqhttp:GroupMessage:1000000004',
            'aiocqhttp:FriendMessage:1000000002',
        ],
        'wl_ignore_admin_on_group': False,
        'wl_ignore_admin_on_friend': False,
        'ignore_bot_self_message': True,
        'ignore_at_all': True,
        'no_permission_reply': False,
    })
    config['admins_id'] = ['1000000002']
    config['provider_settings']['proactive_capability']['add_cron_tools'] = False
    config['provider_ltm_settings']['active_reply']['enable'] = False
    config['pypi_index_url'] = 'https://pypi.org/simple/'
    config['disable_metrics'] = True
    token_path = ROOT / 'onebot-token'
    if not token_path.exists():
        token_path.write_text(secrets.token_hex(24))
    onebot_token = token_path.read_text().strip()
    config['platform'] = [{
        'id': 'aiocqhttp', 'type': 'aiocqhttp', 'enable': True,
        'ws_reverse_host': '0.0.0.0', 'ws_reverse_port': 6199,
        'ws_reverse_token': onebot_token,
    }]
    config['provider_sources'] = [{
        'id': 'deepseek', 'provider': 'deepseek',
        'type': 'openai_chat_completion', 'provider_type': 'chat_completion',
        'enable': False, 'key': [], 'api_base': 'https://api.deepseek.com/v1',
        'timeout': 120, 'proxy': '', 'custom_headers': {},
    }]
    config['provider'] = [{
        'id': MODEL_ID, 'provider_source_id': 'deepseek',
        'type': 'openai_chat_completion', 'provider_type': 'chat_completion',
        'model': 'deepseek-flash', 'enable': False,
        'modalities': ['text'], 'custom_extra_body': {'max_tokens': 2048, 'thinking': {'type': 'disabled'}},
    }]
    config['agent_runner']['config']['model']['provider_id'] = MODEL_ID
    write_json(CONFIG, config)

    onebot = read_json(ROOT / 'upstream/napcat-onebot-template.json')
    client = onebot['network']['websocketClients'][0]
    client.update({
        'name': 'astrbot', 'url': 'ws://astrbot:6199/ws',
        'token': onebot_token, 'heartInterval': 15000, 'reconnectInterval': 5000,
    })
    write_json(ROOT / 'data/napcat/config/onebot11.json', onebot)
    webui_path = ROOT / 'data/napcat/config/webui.json'
    webui = read_json(webui_path)
    env = dict(line.split('=', 1) for line in (ROOT / '.env').read_text().splitlines() if '=' in line)
    webui['token'] = env['NAPCAT_WEBUI_TOKEN']
    write_json(webui_path, webui)
    marker.write_text(datetime.datetime.now().isoformat() + '\n')
    print('Group whitelist, administrator, OneBot connection, and DeepSeek template configured.')
