"""Read-only verification of the deployed reply queue; never print chat or keys."""
import argparse
import datetime
import json
from pathlib import Path
import sqlite3

ROOT = Path('/opt/qq-deepseek')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--expect-version', default='1.3.0')
parser.add_argument('--group', default='1000000003')
args = parser.parse_args()
config = json.loads((ROOT / 'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
plugin = json.loads((ROOT / 'data/astrbot/config/astrbot_plugin_whale_memory_config.json').read_text(encoding='utf-8-sig'))
metadata = (ROOT / 'data/astrbot/plugins/astrbot_plugin_whale_memory/metadata.yaml').read_text(encoding='utf-8')
version = next(line.split(':', 1)[1].strip() for line in metadata.splitlines() if line.startswith('version:'))
settings = config['platform_settings']
expected_group = 'aiocqhttp:GroupMessage:' + args.group
checks = {
    'plugin_version': version == args.expect_version,
    'memory_enabled': plugin.get('enabled', True) is True,
    'ordinary_queue_enabled': plugin.get('proactive_enabled', True) is True,
    'probability_removed': 'proactive_probability' not in plugin,
    'cooldown_10_seconds': plugin.get('proactive_gap_seconds') == 10,
    'reply_hours_08_to_24': (plugin.get('reply_start_hour'), plugin.get('reply_end_hour')) == (8, 24),
    'active_memory_14_days': plugin.get('active_days') == 14,
    'group_unchanged': plugin.get('group_ids') == [args.group],
    'admin_unchanged': config.get('admins_id') == ['1000000002'],
    'group_whitelist_enabled': settings.get('enable_id_white_list') is True and set(settings['id_whitelist']) == set([expected_group, 'aiocqhttp:FriendMessage:1000000002']),
    'native_llm_prefix_empty': config['provider_settings']['wake_prefix'] == '',
    'native_streaming_disabled': config['provider_settings']['streaming_response'] is False,
    'native_count_limit_disabled': settings['rate_limit']['count'] == 0,
    'native_segmentation_enabled': settings['segmented_reply']['enable'] is True,
    'all_wake_names_present': set(['大肥鱼', '鲸鱼娘', '鲸鲸', '大鲸鱼', '小鲸鱼']).issubset(set(plugin['names'])),
}
queue_path = ROOT / 'data/astrbot/plugin_data/whale_memory/reply-queue.json'
queue_info = {'exists': queue_path.exists()}
if queue_path.exists():
    saved = json.loads(queue_path.read_text(encoding='utf-8'))
    queue_info['version'] = saved.get('version')
    queue_info['scopes'] = {}
    for scope, state in saved.get('scopes', {}).items():
        last = state.get('last_reply')
        queue_info['scopes'][scope] = {
            'offered': state['seq'], 'covered_through': state['covered'],
            'pending_count': len(state['pending']),
            'last_reply': datetime.datetime.fromtimestamp(last).isoformat() if last is not None else None,
            'failures': state['failures'],
        }
review_path = ROOT / 'data/astrbot/plugin_data/whale_memory/review-state.json'
review_info = {}
if review_path.exists():
    review = json.loads(review_path.read_text(encoding='utf-8'))
    review_info['completed_day'] = review.get('completed_day')
    result = review.get('last_result') or {}
    review_info['last_counts'] = {key: result.get(key) for key in ('processed_batches', 'events', 'failed_batches')}
memory_path = ROOT / 'data/astrbot/plugin_data/whale_memory/memory.sqlite3'
counts = {}
with sqlite3.connect('file:' + str(memory_path) + '?mode=ro', uri=True) as connection:
    counts = dict(connection.execute('SELECT role,count(*) FROM messages WHERE scope=? GROUP BY role', (expected_group,)).fetchall())
print(json.dumps({'plugin_version': version, 'checks': checks, 'queue': queue_info,
                  'memory_role_counts': counts, 'daily_review': review_info}, ensure_ascii=False, indent=2))
raise SystemExit(0 if all(checks.values()) else 1)
