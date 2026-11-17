"""Bounded async broker for the optional, private local-CPU worker.
Only standard library imports in the AstrBot interpreter. Worker owns all native
embedding/index activity and is stopped or killed on cancellation/timeout.
"""
import asyncio
import json
import math
from pathlib import Path
import sys
import time


class SemanticClient:
    def __init__(self, data_dir, enabled=False, timeout=3.0, sync_interval=60.0,
                 top_k=12, active_days=14, important_threshold=8):
        self.data_dir = Path(data_dir)
        self.enabled = bool(enabled)
        self.timeout = max(0.5, min(10.0, float(timeout)))
        self.sync_interval = max(15.0, min(600.0, float(sync_interval)))
        self.top_k = max(1, min(32, int(top_k)))
        self.active_days = max(1, int(active_days))
        self.important_threshold = float(important_threshold)
        self.process = None
        self.ready = False
        self.closing = False
        self.error_type = None
        self._counter = 0
        self._lock = asyncio.Lock()
        self._maintenance_task = None
        self._dirty = asyncio.Event()
        self._dirty.set()

    def start_background(self):
        if self.enabled and not self.closing and self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(self._maintain(), name='whale-semantic-index')

    def mark_dirty(self, scope=None):
        # Source commits are already durable. This best-effort hint only merges
        # work; each startup and periodic pass also performs full reconciliation.
        if self.enabled and not self.closing:
            self._dirty.set()

    async def _stop_process(self):
        process = self.process
        self.process = None
        self.ready = False
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            # This exact child is the only process this class can kill. Waiting
            # ensures no canceled native embedding can keep writing an old index.
            process.kill()
            await process.wait()

    async def _start_process(self):
        if self.closing or not self.enabled:
            return False
        async with self._lock:
            if self.process is not None and self.process.returncode is None and self.ready:
                return True
            await self._stop_process()
            script = Path(__file__).with_name('semantic_worker.py')
            deps = self.data_dir / '.semantic-deps'
            model = self.data_dir / 'semantic-model'
            if not script.is_file() or not deps.is_dir() or not model.is_dir():
                self.error_type = 'resources_missing'
                return False
            try:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, '-B', str(script), '--data-dir', str(self.data_dir),
                    '--deps-dir', str(deps), '--model-dir', str(model),
                    '--active-days', str(self.active_days),
                    '--important-threshold', str(self.important_threshold),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=65536)
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=45.0)
                response = json.loads(line.decode('utf-8'))
                if response.get('id') != 0 or response.get('ok') is not True:
                    self.error_type = str((response.get('error') or {}).get('type', 'worker_unavailable'))[:64]
                    await self._stop_process()
                    return False
                self.ready = True
                self.error_type = None
                return True
            except asyncio.CancelledError:
                await self._stop_process()
                raise
            except Exception as error:
                self.error_type = type(error).__name__
                await self._stop_process()
                return False

    async def _request(self, action, fields=None, timeout=None):
        if not self.ready or self.closing or self.process is None:
            return None
        acquired = False
        process = None
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self.timeout)
            acquired = True
            process = self.process
            if not self.ready or process is None or process.returncode is not None or self.closing:
                return None
            self._counter += 1
            request = dict(fields or {}, id=self._counter, action=action)
            raw = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8') + b'\n'
            if len(raw) > 65536:
                self.error_type = 'request_budget'
                return None
            process.stdin.write(raw)
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout or self.timeout)
            response = json.loads(line.decode('utf-8'))
            if response.get('id') != self._counter:
                raise ValueError('worker_protocol')
            if response.get('ok') is not True:
                self.error_type = str((response.get('error') or {}).get('type', 'worker_error'))[:64]
                return None
            self.error_type = None
            return response.get('data')
        except asyncio.TimeoutError:
            self.error_type = 'worker_timeout' if acquired else 'worker_busy'
            if acquired:
                await self._stop_process()
            return None
        except asyncio.CancelledError:
            if acquired:
                await self._stop_process()
            raise
        except Exception as error:
            self.error_type = type(error).__name__
            if acquired:
                await self._stop_process()
            return None
        finally:
            if acquired:
                self._lock.release()

    async def search(self, scope, query):
        if (not self.enabled or not self.ready or self.closing
                or not isinstance(query, str) or not query.strip()):
            return {}
        # ReplyQueue already bounds its batch; permit its complete query here.
        results = await self._request('search', {'scope': scope, 'query': query[:6000], 'limit': self.top_k})
        if not isinstance(results, list):
            return {}
        scores = {}
        for item in results:
            if not isinstance(item, dict) or item.get('scope') != scope:
                continue
            event_id, fingerprint, similarity = item.get('event_id'), item.get('fingerprint'), item.get('similarity')
            if (type(event_id) is not int or event_id < 1 or not isinstance(fingerprint, str)
                    or len(fingerprint) != 64 or type(similarity) not in (int, float)
                    or not math.isfinite(similarity) or not -1 <= similarity <= 1):
                continue
            scores[event_id] = {'fingerprint': fingerprint, 'similarity': float(similarity)}
        return scores

    async def _maintain(self):
        try:
            while not self.closing:
                self._dirty.clear()
                if await self._start_process():
                    await self._request('reconcile', timeout=120.0)
                if self.closing:
                    break
                try:
                    await asyncio.wait_for(self._dirty.wait(), timeout=self.sync_interval)
                except asyncio.TimeoutError:
                    pass
                # Coalesce bursts; no new concurrent native worker is created.
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error_type = type(error).__name__
        finally:
            await self._stop_process()

    async def close(self):
        self.closing = True
        self._dirty.set()
        task = self._maintenance_task
        self._maintenance_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            await self._stop_process()
