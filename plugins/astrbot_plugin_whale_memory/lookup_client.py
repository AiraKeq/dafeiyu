"""Async one-shot search broker; bounded subprocess, no QQ send capabilities."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time


class LookupClient:
    def __init__(self, data_dir, enabled=False, timeout=8.0):
        self.data_dir = Path(data_dir)
        self.enabled = bool(enabled)
        self.timeout = min(12.0, max(2.0, float(timeout)))
        self.closing = False
        self._gate = asyncio.Semaphore(1)
        self._processes = set()
        self._cache = {}
        self.error_type = None

    async def search(self, queries, scope=''):
        unavailable = {'state': 'unavailable', 'results': []}
        if not self.enabled or self.closing:
            return unavailable
        try:
            from .lookup_policy import public_query, clean_results
        except ImportError:
            from lookup_policy import public_query, clean_results
        if not isinstance(queries, list) or not 1 <= len(queries) <= 2:
            return unavailable
        checked = [public_query(query) for query in queries]
        if any(not query for query in checked):
            return {'state': 'privacy_blocked', 'results': []}
        key = (scope, hashlib.sha256(json.dumps(checked, ensure_ascii=False).encode()).hexdigest())
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return {'state': 'ready', 'results': cached[1], 'cached': True}
        script = Path(__file__).with_name('lookup_worker.py')
        deps = self.data_dir / '.lookup-deps'
        if not script.is_file() or not deps.is_dir():
            self.error_type = 'search_dependency_missing'
            return unavailable
        process = None
        acquired = False
        try:
            await asyncio.wait_for(self._gate.acquire(), timeout=0.5)
            acquired = True
            if self.closing:
                return unavailable
            env = dict(os.environ)
            for name in ('DDGS_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
                env.pop(name, None)
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-B', str(script), '--deps-dir', str(deps),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env)
            self._processes.add(process)
            payload = json.dumps({'queries': checked}, ensure_ascii=False, allow_nan=False).encode()
            stdout, _ = await asyncio.wait_for(process.communicate(payload), timeout=self.timeout)
            if len(stdout) > 65536 or process.returncode != 0:
                raise ValueError('search_response')
            response = json.loads(stdout.decode('utf-8'))
            if response.get('ok') is not True:
                self.error_type = str((response.get('error') or {}).get('type', 'search_failed'))[:64]
                return unavailable
            results = clean_results((response.get('data') or {}).get('results', []), max_results=3)
            if not results:
                self.error_type = 'no_evidence'
                return {'state': 'no_evidence', 'results': []}
            self.error_type = None
            if len(self._cache) >= 64:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic() + 3600, results)
            return {'state': 'ready', 'results': results, 'cached': False}
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error_type = type(error).__name__
            return unavailable
        finally:
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                self._processes.discard(process)
            if acquired:
                self._gate.release()

    async def close(self):
        self.closing = True
        self._cache.clear()
        processes = list(self._processes)
        for process in processes:
            if process.returncode is None:
                process.kill()
        if processes:
            await asyncio.gather(*(process.wait() for process in processes), return_exceptions=True)
