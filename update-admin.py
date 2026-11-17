import datetime
import json
import os
from pathlib import Path
import shutil

root = Path('/opt/qq-deepseek')
path = root / 'data/astrbot/cmd_config.json'
backup = root / 'backups' / ('admin-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
shutil.copy2(str(path), str(backup))
os.chmod(str(backup), 0o600)
config = json.loads(path.read_text(encoding='utf-8-sig'))
config['admins_id'] = ['1000000002']
config['platform_settings']['id_whitelist'] = [
    entry for entry in config['platform_settings']['id_whitelist']
    if not entry.startswith('aiocqhttp:FriendMessage:')
] + [
    'aiocqhttp:FriendMessage:1000000002',
]
pending = path.with_suffix('.pending')
pending.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
os.chmod(str(pending), 0o600)
pending.replace(path)
print('Administrator updated to 1000000002; group whitelist retained.')
