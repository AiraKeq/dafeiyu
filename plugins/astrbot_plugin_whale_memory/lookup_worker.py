"""One-shot bounded search worker. Input has public queries only, never images/keys.
DDGS is imported only here from isolated optional deps; no QQ or AstrBot API.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deps-dir', required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError('query_budget')
        request = json.loads(raw.decode('utf-8'))
        if not isinstance(request, dict) or set(request) != {'queries'}:
            raise ValueError('query_schema')
        from lookup_policy import public_query, clean_results
        queries = request['queries']
        if not isinstance(queries, list) or not 1 <= len(queries) <= 2:
            raise ValueError('query_budget')
        queries = [public_query(query) for query in queries]
        if any(not query for query in queries):
            raise ValueError('private_query_rejected')
        deps = Path(args.deps_dir).absolute()
        if deps.is_symlink() or not deps.is_dir():
            raise RuntimeError('search_dependency_missing')
        for key in ('DDGS_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            os.environ.pop(key, None)
        sys.path.insert(0, str(deps))
        from ddgs import DDGS
        DDGS.threads = 1
        search = DDGS(proxy=None, timeout=2.5, verify=True)
        results, errors, used_backend = [], [], None
        deadline = time.monotonic() + 6.0
        # One failed engine/query must not discard another one's valid evidence.
        # Return as soon as bounded useful sources exist, rather than waiting for
        # a redundant second query to exhaust the parent's process deadline.
        for backend in ('yahoo', 'wikipedia', 'yandex'):
            for query in queries:
                if time.monotonic() >= deadline:
                    break
                try:
                    rows = search.text(query, region='cn-zh', safesearch='moderate', max_results=3,
                                       backend=backend)
                    results = clean_results(rows or [], max_results=3)
                    if results:
                        used_backend = backend
                        break
                except Exception as error:
                    errors.append(type(error).__name__)
            if results or time.monotonic() >= deadline:
                break
        response = {'ok': True, 'data': {'results': results,
                                        'backend': used_backend, 'errors': errors[:6],
                                        'evidence_level': 'search_summary'}}
    except Exception as error:
        response = {'ok': False, 'error': {'type': type(error).__name__}}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
