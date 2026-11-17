"""Read deployed group settings and memory counts without printing chat text."""
import json
from pathlib import Path
import sqlite3

root = Path('/opt/qq-deepseek')
config = json.loads((root / 'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
plugin = json.loads((root / 'data/astrbot/config/astrbot_plugin_whale_memory_config.json').read_text(encoding='utf-8-sig'))
settings = config['platform_settings']
print('Whitelist enabled:', settings['enable_id_white_list'])
print('Whitelist:', settings['id_whitelist'])
print('Memory groups:', plugin['group_ids'])
print('Wake names:', plugin['names'])
print('Proactive cooldown seconds:', plugin['proactive_gap_seconds'])
print('Reply hours:', plugin['reply_start_hour'], plugin['reply_end_hour'])
path = root / 'data/astrbot/plugin_data/whale_memory/memory.sqlite3'
with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as connection:
    for group in plugin['group_ids']:
        scope = 'aiocqhttp:GroupMessage:' + str(group)
        counts = connection.execute(
            'SELECT role,count(*) FROM messages WHERE scope=? GROUP BY role', (scope,),
        ).fetchall()
        print('Memory role counts for ' + str(group) + ':', dict(counts))
