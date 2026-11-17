"""Read-only metadata verification for the formal 1.4.0 enhanced bot."""
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT=Path('/opt/qq-deepseek')
c=json.loads((ROOT/'data/astrbot/cmd_config.json').read_text(encoding='utf-8-sig'))
p=json.loads((ROOT/'data/astrbot/config/astrbot_plugin_whale_memory_config.json').read_text(encoding='utf-8-sig'))
metadata=(ROOT/'data/astrbot/plugins/astrbot_plugin_whale_memory/metadata.yaml').read_text(encoding='utf-8')
version=next(line.split(':',1)[1].strip() for line in metadata.splitlines() if line.startswith('version:'))
m=next(x for x in c['provider'] if x['id']=='deepseek/deepseek-flash')
checks={'version_1_4_1':version=='1.4.1','delivery_guard':p.get('delivery_guard_enabled') is True,
        'optional_polish':p.get('reply_polish_enabled') is True,'vision_enabled':p.get('vision_enabled') is True,
        'model_image_modality':'image' in m.get('modalities',[]),'model_enabled':m.get('enable') is True,
        'formal_group':p.get('group_ids')==['1000000003'],'admin':c.get('admins_id')==['1000000002'],
        'weights_unchanged':[p.get('segment_weight_1'),p.get('segment_weight_2'),p.get('segment_weight_3')]==[60,30,10],
        'hours_unchanged':[p.get('reply_start_hour'),p.get('reply_end_hour')]==[8,24],
        'memory_14':p.get('active_days')==14,'cooldown_10':p.get('proactive_gap_seconds')==10,
        'nonstream':c['provider_settings']['streaming_response'] is False,
        'native_limit_60':'{1,60}' in c['platform_settings']['segmented_reply']['regex']}
style=p.get('base_style') or ''
data=ROOT/'data/astrbot/plugin_data/whale_memory'
journal=json.loads((data/'segment-score-state.json').read_text(encoding='utf-8')) if (data/'segment-score-state.json').exists() else None
pages=[]
if (data/'pagination.sqlite3').exists():
    with sqlite3.connect('file:'+str(data/'pagination.sqlite3')+'?mode=ro',uri=True) as db:
        pages=db.execute('SELECT scope,sender_id,next_index,page_count,expires_at FROM page_responses').fetchall()
print(json.dumps({'version':version,'checks':checks,'style_chars':len(style),
      'style_sha256':hashlib.sha256(style.encode('utf-8')).hexdigest(),'model':m['model'],
      'modalities':m.get('modalities'),'delivered_segment_metadata':journal,
      'pagination_metadata':pages},ensure_ascii=False,indent=2))
raise SystemExit(0 if all(checks.values()) else 1)
