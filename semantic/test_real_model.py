"""Offline synthetic Chinese semantic benchmark; never touches production facts."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import resource
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace

parser = argparse.ArgumentParser()
parser.add_argument('--plugin-dir', required=True)
parser.add_argument('--deps-dir', required=True)
parser.add_argument('--model-dir', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--min-similarity', type=float, default=0.42)
args = parser.parse_args()
sys.path.insert(0, args.plugin_dir)
from memory_store import MemoryStore, SHANGHAI
from semantic_worker import load_runtime
from semantic_state import event_fingerprint

cases = [
    ('同事推卸责任', '阿青在公司替同事承担项目失误，觉得非常委屈。', '阿青替同事承担项目失误。',
     '上回工作里背黑锅那件事后来怎么样了？'),
    ('团子拒食', '团子的猫拒绝吃东西，宠物医生发现肠胃不适，已经开始治疗。', '团子的猫因肠胃不适接受治疗。',
     '那只毛孩子生病以后恢复得好吗？'),
    ('购入RTX4060', '阿青购入RTX4060显卡用来玩游戏，希望画面更流畅。', '阿青购入RTX4060，期待画面更流畅。',
     '换了电脑硬件之后帧率有提升吗？'),
]
now = time.time()
with tempfile.TemporaryDirectory(prefix='whale-semantic-quality-') as temporary:
    data = Path(temporary)
    store = MemoryStore(data / 'memory.sqlite3')
    when = now - 5 * 86400
    day = datetime.fromtimestamp(when, SHANGHAI).date().isoformat()
    for serial, (title, detail, core, query) in enumerate(cases, 1):
        for scope in ('synthetic-group-a', 'synthetic-group-b'):
            mid = scope + '-' + str(serial)
            store.add_message(scope, mid, 'user', '101', '合成朋友', detail, when)
            with sqlite3.connect(str(store.db_path)) as db:
                rid = db.execute('SELECT id FROM messages WHERE scope=? AND message_id=?', (scope, mid)).fetchone()[0]
            store.commit_events(scope, day, [rid], [{'title': title, 'detail': detail, 'core': core,
                'importance': 8, 'participants': ['101'], 'keywords': []}], now=now)
    index, encode = load_runtime(SimpleNamespace(data_dir=str(data), deps_dir=args.deps_dir,
        model_dir=args.model_dir, active_days=14, important_threshold=8))
    started = time.monotonic()
    sync = index.reconcile(now=now)
    results = []
    for title, detail, core, query in cases:
        vector = encode([query])[0]
        candidates = index.search('synthetic-group-a', vector, limit=3, now=now)
        with sqlite3.connect(str(store.db_path)) as db:
            db.row_factory = sqlite3.Row
            source = {row['id']: dict(row) for row in db.execute('SELECT * FROM events WHERE scope=?', ('synthetic-group-a',))}
        top = candidates[0]
        if source[top['event_id']]['title'] != title or any(x['scope'] != 'synthetic-group-a' for x in candidates):
            raise AssertionError('Semantic top result or scope isolation failed.')
        lexical = store.recall('synthetic-group-a', query, now=now)
        scores = {x['event_id']: {'fingerprint': x['fingerprint'], 'similarity': x['similarity']} for x in candidates}
        semantic = store.recall('synthetic-group-a', query, now=now, semantic_scores=scores, min_similarity=args.min_similarity)
        results.append({'query': query, 'expected': title, 'top_similarity': top['similarity'],
                        'lexical_hit': title in lexical, 'semantic_hit': title in semantic,
                        'scope_isolated': True})
    negatives = []
    for query in ('明天天气怎样？', '午饭吃什么？', '马里亚纳海沟有多深？', '今天欧元汇率多少？', '周末看什么电影？'):
        candidates = index.search('synthetic-group-a', encode([query])[0], limit=3, now=now)
        best = candidates[0]['similarity']
        negatives.append({'query':query,'best_similarity':best,'below_threshold':best<args.min_similarity})
    # Deleted source events must never survive through a previously captured score.
    first_query = cases[0][3]
    old = index.search('synthetic-group-a', encode([first_query])[0], limit=3, now=now)[0]
    store.forget_event('synthetic-group-a', old['event_id'])
    current = store.recall('synthetic-group-a', first_query, now=now,
                          semantic_scores={old['event_id']: {'fingerprint': old['fingerprint'],
                          'similarity': old['similarity']}}, min_similarity=args.min_similarity)
    if cases[0][0] in current:
        raise AssertionError('Deleted fact appeared through stale vector.')
    report = {'model': 'BAAI/bge-small-zh-v1.5', 'dimension': 512, 'local_only': True,
              'synthetic_only': True, 'index': sync, 'cases': results, 'negative_controls': negatives,
              'min_similarity':args.min_similarity,
              'deleted_vector_rejected': True, 'elapsed_seconds': time.monotonic()-started,
              'rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    if not all(case['semantic_hit'] for case in results) or not all(case['below_threshold'] for case in negatives):
        raise AssertionError('Calibrated similarity threshold needs tuning.')
