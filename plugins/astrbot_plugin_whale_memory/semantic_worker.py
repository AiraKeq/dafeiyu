"""Private semantic worker process. No third-party imports in AstrBot's process.
Protocol stdout contains sanitized JSON only; all inference is local and offline.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

MODEL_ID = 'BAAI/bge-small-zh-v1.5'
MODEL_REVISION = '46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59'
ONNX_SHA256 = '1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38'
DIMENSION = 512


def emit(request_id, data=None, error=None):
    response = {'id': request_id, 'ok': error is None}
    if error is None:
        response['data'] = data
    else:
        response['error'] = {'type': str(error)[:64]}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def verify_model(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError('ModelUnavailable')
    manifest_path = directory / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError('ModelUnavailable')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('model') != MODEL_ID or manifest.get('revision') != MODEL_REVISION
            or manifest.get('dimension') != DIMENSION):
        raise RuntimeError('ModelIdentity')
    files = manifest.get('files', {})
    required = {'config.json', 'model_optimized.onnx', 'ort_config.json', 'special_tokens_map.json',
                'tokenizer.json', 'tokenizer_config.json', 'vocab.txt'}
    if set(files) != required or files['model_optimized.onnx'].get('sha256') != ONNX_SHA256:
        raise RuntimeError('ModelIdentity')
    for name, expected in files.items():
        target = directory / name
        if target.is_symlink() or not target.is_file() or target.stat().st_size != expected.get('size'):
            raise RuntimeError('ModelIntegrity')
        sha = hashlib.sha256()
        with target.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                sha.update(chunk)
        if sha.hexdigest() != expected.get('sha256'):
            raise RuntimeError('ModelIntegrity')
    return manifest


def load_runtime(args):
    directory = Path(args.model_dir).absolute()
    dependencies = Path(args.deps_dir).absolute()
    verify_model(directory)
    if dependencies.is_symlink() or not dependencies.is_dir():
        raise RuntimeError('DependenciesUnavailable')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    sys.path.insert(0, str(dependencies))
    # These imports are confined to this worker interpreter, not the main bot.
    from fastembed import TextEmbedding
    import sqlite_vec
    from semantic_index import SemanticIndex
    model = TextEmbedding(model_name=MODEL_ID, specific_model_path=str(directory),
                          local_files_only=True, threads=1, providers=['CPUExecutionProvider'])

    def encode(texts):
        if not isinstance(texts, list) or len(texts) > 32:
            raise ValueError('EmbeddingBudget')
        output = [vector.tolist() for vector in model.embed(texts, batch_size=1)]
        if len(output) != len(texts):
            raise ValueError('EmbeddingShape')
        for vector in output:
            if len(vector) != DIMENSION or not all(type(x) in (int, float) and math.isfinite(x) for x in vector):
                raise ValueError('EmbeddingShape')
        return output

    index = SemanticIndex(Path(args.data_dir).absolute(), encode,
                          active_days=args.active_days, important_threshold=args.important_threshold,
                          dimension=DIMENSION)
    return index, encode


def serve(args):
    try:
        index, encode = load_runtime(args)
        emit(0, {'model': MODEL_ID, 'dimension': DIMENSION, 'execution': 'local_cpu',
                 'external_text_transfer': False})
    except Exception as error:
        emit(0, error=type(error).__name__)
        return 1
    while True:
        line = sys.stdin.buffer.readline(65537)
        if not line:
            break
        request_id = 0
        try:
            if len(line) > 65536 or not line.endswith(b'\n'):
                raise ValueError('RequestSize')
            request = json.loads(line.decode('utf-8'))
            if not isinstance(request, dict) or type(request.get('id')) is not int or request['id'] < 1:
                raise ValueError('Protocol')
            request_id = request['id']
            action = request.get('action')
            allowed = {'id', 'action', 'scope', 'query', 'limit'}
            if set(request) - allowed:
                raise ValueError('Protocol')
            if action == 'stop':
                emit(request_id, {'stopped': True})
                break
            if action == 'reconcile':
                emit(request_id, index.reconcile(now=time.time()))
            elif action == 'status':
                from semantic_state import index_status
                emit(request_id, index_status(Path(args.data_dir).absolute(), now=time.time(),
                                              active_days=args.active_days,
                                              important_threshold=args.important_threshold))
            elif action == 'search':
                scope, query, limit = request.get('scope'), request.get('query'), request.get('limit', 12)
                if (not isinstance(scope, str) or not scope or len(scope) > 240
                        or not isinstance(query, str) or not query.strip() or len(query) > 6000
                        or type(limit) is not int or not 1 <= limit <= 32):
                    raise ValueError('QueryBudget')
                # Cover the complete bounded reply-batch query. Chinese text is
                # split into small pieces rather than silently embedding only its first message.
                chunks = [query[offset:offset + 400] for offset in range(0, len(query), 400)]
                vectors = encode(chunks)
                matches = {}
                for vector in vectors:
                    for item in index.search(scope, vector, limit=limit, now=time.time()):
                        key = item['event_id']
                        if key not in matches or item['similarity'] > matches[key]['similarity']:
                            matches[key] = item
                results = sorted(matches.values(), key=lambda item: (-item['similarity'], item['event_id']))[:limit]
                emit(request_id, results)
            else:
                raise ValueError('Protocol')
        except Exception as error:
            emit(request_id, error=getattr(error, 'error_type', type(error).__name__))
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--deps-dir', required=True)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--active-days', type=int, default=14)
    parser.add_argument('--important-threshold', type=float, default=8)
    parsed = parser.parse_args()
    sys.exit(serve(parsed))
