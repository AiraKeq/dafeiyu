"""Inspect live segment-scoring settings and metadata without exposing chat or keys."""
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--group', default='1000000003')
args = parser.parse_args()
ROOT = Path('/opt/qq-deepseek')
plugin = json.loads((ROOT/'data/astrbot/config/astrbot_plugin_whale_memory_config.json').read_text(encoding='utf-8-sig'))
metadata = (ROOT/'data/astrbot/plugins/astrbot_plugin_whale_memory/metadata.yaml').read_text(encoding='utf-8')
version = next(line.split(':',1)[1].strip() for line in metadata.splitlines() if line.startswith('version:'))
style = plugin.get('base_style') or ''
fields = {key:plugin.get(key) for key in ('segment_score_enabled','segment_weight_1','segment_weight_2','segment_weight_3','segment_score_micro_random')}
checks = {
    'version_1_3_0': version == '1.3.0',
    'scoring_enabled': fields['segment_score_enabled'] is True,
    'weights_60_30_10': [fields['segment_weight_1'],fields['segment_weight_2'],fields['segment_weight_3']] == [60,30,10],
    'small_tie_random_enabled': fields['segment_score_micro_random'] is True,
    'approved_style_present': bool(style),
    'cooldown_10_unchanged': plugin.get('proactive_gap_seconds') == 10,
    'hours_08_24_unchanged': [plugin.get('reply_start_hour'),plugin.get('reply_end_hour')] == [8,24],
    'memory_14_unchanged': plugin.get('active_days') == 14,
    'group_unchanged': plugin.get('group_ids') == [args.group],
}
path = ROOT/'data/astrbot/plugin_data/whale_memory/segment-score-state.json'
journal = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
print(json.dumps({'version':version,'checks':checks,'settings':fields,
                  'style_chars':len(style),'style_sha256':hashlib.sha256(style.encode('utf-8')).hexdigest(),
                  'segment_journal':journal},ensure_ascii=False,indent=2))
raise SystemExit(0 if all(checks.values()) else 1)
