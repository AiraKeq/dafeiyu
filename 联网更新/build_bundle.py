"""Package only the explicitly reviewed source files; no data, keys or models."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent / 'plugins/astrbot_plugin_whale_memory'
OUT = Path(__file__).resolve().parent
FILES = [
    '__init__.py', 'main.py', 'memory_store.py', 'daily_review.py', 'reply_queue.py',
    'segmentation.py', 'delivery.py', 'page_store.py', 'reply_polish.py',
    'vision_inbox.py', 'history_guard.py', 'semantic_state.py', 'semantic_index.py',
    'semantic_client.py', 'semantic_worker.py', 'command_policy.py', 'command_ingress.py', 'lookup_policy.py', 'lookup_client.py', 'lookup_worker.py', '_conf_schema.json', 'metadata.yaml', 'README.md',
    'tests/test_daily_review.py', 'tests/test_memory_store.py', 'tests/test_reply_queue.py',
    'tests/test_segmentation.py', 'tests/test_page_store.py', 'tests/test_delivery.py',
    'tests/test_reply_polish.py', 'tests/test_vision_inbox.py', 'tests/test_history_guard.py',
    'tests/test_semantic_state.py', 'tests/test_semantic_index.py',
    'tests/rehearsal_runtime.py', 'tests/rehearsal_queue_safety.py',
    'tests/rehearsal_segmentation.py', 'tests/rehearsal_enhancements.py', 'tests/test_command_policy.py', 'tests/test_lookup_policy.py', 'tests/rehearsal_command_policy.py', 'tests/rehearsal_lookup.py',
]
manifest = {}
target = OUT / 'whale-memory-1.6.0.zip'
with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for relative in FILES:
        source = ROOT / relative
        if not source.is_file() or source.is_symlink():
            raise RuntimeError('Missing reviewed source: ' + relative)
        raw = source.read_bytes()
        if source.suffix in ('.py', '.json', '.yaml', '.md'):
            decoded = raw.decode('utf-8')
            if '\ufffd' in decoded:
                raise RuntimeError('Replacement character in source: ' + relative)
        manifest[relative] = hashlib.sha256(raw).hexdigest()
        archive.writestr(relative, raw)
sha = hashlib.sha256(target.read_bytes()).hexdigest()
(OUT / 'bundle-manifest.json').write_text(json.dumps({'version':'1.6.0','zip_sha256':sha,'files':manifest},indent=2)+'\n',encoding='utf-8')
print(json.dumps({'files':len(FILES),'zip_sha256':sha,'package':str(target)}))

