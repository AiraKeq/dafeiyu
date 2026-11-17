"""Isolated synthetic model test. No QQ messages and no production memory writes."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, '/rehearsal/data/plugins/astrbot_plugin_whale_memory')
from memory_store import MemoryStore
from daily_review import DailyReviewer

config = json.loads(Path('/run/model-config.json').read_text(encoding='utf-8-sig'))
source = next(item for item in config['provider_sources'] if item['id'] == 'deepseek')


def generate_sync(prompt, system_prompt):
    request = urllib.request.Request(
        source['api_base'].rstrip('/') + '/chat/completions',
        data=json.dumps({
            'model': 'deepseek-flash', 'max_tokens': 8192,
            'thinking': {'type': 'disabled'}, 'response_format': {'type': 'json_object'},
            'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': prompt}],
        }).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + source['key'][0]},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    print('Model-test tokens:', payload.get('usage', {}).get('total_tokens'))
    return payload['choices'][0]['message']['content']


async def generate(prompt, system_prompt):
    return await asyncio.to_thread(generate_sync, prompt, system_prompt)


async def main():
    scope = 'test:GroupMessage:synthetic'
    now = time.time()
    yesterday = datetime.now(timezone(timedelta(hours=8))).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    with tempfile.TemporaryDirectory(prefix='whale-daily-test-') as temporary:
        store = MemoryStore(Path(temporary) / 'memory.sqlite3')
        rows = [
            ('1', 'user', 'friend-a', '测试朋友甲', '我喜欢草莓蛋糕，以后生日我们也一起庆祝吧。'),
            ('2', 'user', 'friend-b', '测试朋友乙', '小鲸鱼，谢谢你昨晚安慰我。我也希望你好好的，我们一直是好朋友。'),
            ('3', 'assistant', 'synthetic-bot', '测试小鲸鱼', '记下草莓蛋糕啦，也谢谢朋友们的关心。希望每次聊天都让大家暖一点。'),
        ]
        for index, (mid, role, uid, name, text) in enumerate(rows):
            store.add_message(scope, mid, role, uid, name, text, yesterday.timestamp() + index)
        reviewer = DailyReviewer(store, generate, '活泼可爱、温柔体贴、善意玩笑；所有人都是好朋友，在稳定基调上成长表达习惯。')
        result = await reviewer.run(before_ts=now, now=now, max_batches=1)
        print('Daily-review result:', json.dumps(result, ensure_ascii=False))
        stats = store.statistics(scope)
        print('Synthetic store:', json.dumps(stats, ensure_ascii=False))
        assert result['processed_batches'] == 1 and result['failed_batches'] == 0 and not result['errors']
        assert stats['pending_messages'] == 0 and stats['events'] > 0
        assert store.get_style(scope)
        assert store.recall(scope, '草莓蛋糕 好朋友', max_chars=500)
        print('REAL_MODEL_DAILY_REVIEW_OK')


asyncio.run(main())
