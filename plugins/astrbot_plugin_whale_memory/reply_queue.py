"""Durable per-conversation reply batching, independent of daily memory review.

Native message handlers may use a token returned by ``offer`` immediately.
Delayed work is dispatched through the supplied callback; that callback must
arrange ``claim`` before generation and ``finish`` after the complete response.
Only one live ReplyQueue should own a given state file.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
import inspect
import json
import math
import os
from pathlib import Path
import threading
import uuid


class ReplyQueue:
    LEASE_SECONDS = 180.0
    MAX_PACKETS = 32
    MAX_TEXT_CHARS = 6000
    MAX_PACKET_CHARS = 4000
    RECENT_SEEN_LIMIT = 2048

    def __init__(self, path, dispatch, now, gap_seconds, can_reply, quiet_delay, on_error,
                 on_cancel=None, can_force_reply=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dispatch = dispatch
        self.now = now
        self.gap_seconds = gap_seconds
        self.can_reply = can_reply
        self.can_force_reply = can_force_reply or can_reply
        self.quiet_delay = quiet_delay
        self.on_error = on_error
        self.on_cancel = on_cancel
        self._lock = threading.RLock()
        self._scopes = {}
        self._jobs = {}
        self._dispatch_tasks = {}
        self._worker = None
        self._wake = None
        self._loop = None
        self._running = False
        self._closed = False
        self._load()

    @property
    def running(self):
        return self._running and not self._closed

    @staticmethod
    def _number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError('Reply queue time must be finite')
        try:
            result = float(value)
        except (ValueError, OverflowError):
            raise ValueError('Reply queue time must be finite') from None
        if not math.isfinite(result):
            raise ValueError('Reply queue time must be finite')
        return result

    def _time(self):
        return self._number(self.now())

    def _gap(self):
        return max(0.0, self._number(self.gap_seconds()))

    @staticmethod
    def _scope_name(scope):
        if not isinstance(scope, str) or not scope or len(scope) > 512:
            raise ValueError('Invalid reply queue scope')
        return scope

    def _payload(self, payload):
        if type(payload) is not dict:
            raise ValueError('Reply queue payload must be a plain object')
        message_id = str(payload.get('message_id') or '')
        text = payload.get('text', '')
        if not message_id or len(message_id) > 240 or not isinstance(text, str):
            raise ValueError('Reply queue payload requires a message id and text')
        packet = {'message_id': message_id, 'text': text[:self.MAX_PACKET_CHARS],
                  'ts': self._number(payload.get('ts', self._time()))}
        for key in ('sender_id', 'sender_name', 'group_id', 'platform_id', 'self_id', 'session_id'):
            value = payload.get(key, '')
            if value is None:
                value = ''
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise ValueError('Invalid reply queue identity field')
            packet[key] = str(value)[:512]
        images = payload.get('images', [])
        if not isinstance(images, list) or len(images) > 3 or any(not isinstance(ref, str) or not ref or len(ref) > 2048 for ref in images):
            raise ValueError('Reply queue image references must be a bounded string list')
        if images:
            packet['images'] = list(images)
        return packet

    @staticmethod
    def _empty_scope():
        return {'seq': 0, 'covered': 0, 'last_reply': None, 'pending': [],
                'seen': {}, 'retry_at': 0.0, 'failures': 0}

    def _state(self, scope):
        return self._scopes.setdefault(scope, self._empty_scope())

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') != 1 or type(data.get('scopes')) is not dict:
                raise ValueError('Invalid reply queue state version')
            for scope, saved in data['scopes'].items():
                self._scope_name(scope)
                if type(saved) is not dict:
                    raise ValueError('Invalid reply queue scope state')
                state = self._empty_scope()
                state['seq'], state['covered'] = saved.get('seq', 0), saved.get('covered', 0)
                if (type(state['seq']) is not int or type(state['covered']) is not int
                        or not 0 <= state['covered'] <= state['seq']):
                    raise ValueError('Invalid reply queue sequence')
                if saved.get('last_reply') is not None:
                    state['last_reply'] = self._number(saved['last_reply'])
                state['retry_at'] = self._number(saved.get('retry_at', 0))
                state['failures'] = max(0, int(saved.get('failures', 0)))
                if not isinstance(saved.get('pending', []), list) or type(saved.get('seen', {})) is not dict:
                    raise ValueError('Invalid reply queue pending state')
                sequences = set()
                message_ids = set()
                for item in saved.get('pending', []):
                    packet = self._payload(item)
                    seq = item.get('seq')
                    if (type(seq) is not int or not state['covered'] < seq <= state['seq']
                            or seq in sequences or packet['message_id'] in message_ids):
                        raise ValueError('Invalid reply queue pending sequence')
                    packet['seq'] = seq
                    state['pending'].append(packet)
                    sequences.add(seq)
                    message_ids.add(packet['message_id'])
                state['pending'].sort(key=lambda item: item['seq'])
                for message_id, seq in saved.get('seen', {}).items():
                    if not isinstance(message_id, str) or type(seq) is not int or not 0 < seq <= state['seq']:
                        raise ValueError('Invalid reply queue deduplication state')
                    state['seen'][message_id] = seq
                for packet in state['pending']:
                    state['seen'][packet['message_id']] = packet['seq']
                self._prune_seen(state)
                self._scopes[scope] = state
        except Exception as error:
            self._report(error)
            # Fail closed rather than silently discard a corrupted durable queue.
            raise ValueError('Cannot load reply queue state') from None

    def _save(self):
        pending = self.path.with_name(self.path.name + '.pending')
        data = {'version': 1, 'scopes': self._scopes}
        with pending.open('w', encoding='utf-8', newline='\n') as handle:
            os.chmod(str(pending), 0o600)
            json.dump(data, handle, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        # The replacement already inherits the private mode set on the temp file.
        # Nothing fallible follows the commit boundary.
        os.replace(str(pending), str(self.path))

    @contextmanager
    def _transaction(self):
        # Do not consume an input, forget a token, or cancel its native task until
        # the atomic state replacement succeeds. Bad settings also roll back.
        scopes_before = copy.deepcopy(self._scopes)
        jobs_before = copy.deepcopy(self._jobs)
        try:
            yield
            self._save()
        except BaseException:
            self._scopes = scopes_before
            self._jobs = jobs_before
            raise
        else:
            for token in jobs_before.keys() - self._jobs.keys():
                self._cancel_dispatch(token)

    def _prune_seen(self, state):
        pending_ids = {item['message_id'] for item in state['pending']}
        finished = sorted(((seq, key) for key, seq in state['seen'].items() if key not in pending_ids),
                          reverse=True)
        for _, key in finished[self.RECENT_SEEN_LIMIT:]:
            state['seen'].pop(key, None)

    def _report(self, error):
        try:
            result = self.on_error(error)
            if inspect.isawaitable(result):
                try:
                    asyncio.get_running_loop().create_task(result)
                except RuntimeError:
                    if inspect.iscoroutine(result):
                        result.close()
        except Exception:
            pass

    def _notify(self):
        if self._running and self._wake is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

    def _cancel_dispatch(self, token):
        task = self._dispatch_tasks.get(token)
        if task and not task.done():
            try:
                if task is not asyncio.current_task():
                    task.cancel()
            except RuntimeError:
                if self._loop and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(task.cancel)
        if self.on_cancel is not None:
            try:
                self.on_cancel(token)
            except Exception as error:
                self._report(error)

    def _jobs_for(self, scope):
        return [job for job in self._jobs.values() if job['scope'] == scope]

    def _new_job(self, scope, packet, force, timestamp):
        token = uuid.uuid4().hex
        self._jobs[token] = {'scope': scope, 'force': bool(force), 'packet': copy.deepcopy(packet),
                             'expires': timestamp + self.LEASE_SECONDS, 'snapshot': None}
        return token

    def _backoff(self, state, timestamp):
        state['failures'] = min(16, state['failures'] + 1)
        try:
            gap = self._gap()
        except (ValueError, OverflowError):
            gap = 10.0
        delay = min(60.0, max(1.0, gap) * 2 ** min(5, state['failures'] - 1))
        state['retry_at'] = max(state['retry_at'], timestamp + delay)

    def _expire(self, timestamp):
        changed = False
        for token, job in list(self._jobs.items()):
            if timestamp >= job['expires']:
                self._jobs.pop(token, None)
                self._backoff(self._state(job['scope']), timestamp)
                changed = True
        return changed

    def offer(self, scope, payload, force=False):
        scope = self._scope_name(scope)
        packet = self._payload(payload)
        timestamp = self._time()
        with self._lock:
            if self._closed:
                raise RuntimeError('Reply queue is closed')
            gap = self._gap()  # Reject invalid settings before admitting an input.
            with self._transaction():
                self._expire(timestamp)
                state = self._state(scope)
                token = None
                if packet['message_id'] not in state['seen']:
                    state['seq'] += 1
                    packet['seq'] = state['seq']
                    state['pending'].append(packet)
                    state['seen'][packet['message_id']] = packet['seq']
                    self._prune_seen(state)
                    if force:
                        for old_token, job in list(self._jobs.items()):
                            if job['scope'] == scope and not job['force']:
                                self._jobs.pop(old_token, None)
                    allowed = bool((self.can_force_reply if force else self.can_reply)(scope))
                    if allowed:
                        due = state['last_reply'] is None or timestamp >= state['last_reply'] + gap
                        if force or (not self._jobs_for(scope) and due and timestamp >= state['retry_at']):
                            token = self._new_job(scope, packet, force, timestamp)
        self._notify()
        return token

    def _get_job(self, scope, token, timestamp):
        if any(timestamp >= job['expires'] for job in self._jobs.values()):
            with self._transaction():
                self._expire(timestamp)
            self._notify()
        job = self._jobs.get(token)
        return job if job and job['scope'] == scope else None

    def claim(self, scope, token):
        with self._lock:
            if self._closed:
                return None
            timestamp = self._time()
            job = self._get_job(scope, token, timestamp)
            if job is None:
                return None
            if job['snapshot'] is None:
                state = self._state(scope)
                chosen = []
                total = 0
                # Direct addressing always includes the exact request that created this job.
                if job['force']:
                    chosen.append(job['packet'])
                    total = len(job['packet']['text'])
                selected_seqs = {packet['seq'] for packet in chosen}
                image_count = sum(len(packet.get('images', [])) for packet in chosen)
                # Concurrent direct requests keep their own packet and any frozen
                # batch reserved. No input may belong to two active responses.
                reserved = set()
                for other_token, other in self._jobs.items():
                    if other_token != token and other['scope'] == scope:
                        reserved.update(packet['seq'] for packet in
                                        (other['snapshot'] if other['snapshot'] is not None else [other['packet']]))
                for packet in state['pending']:
                    if packet['seq'] in selected_seqs or packet['seq'] in reserved:
                        continue
                    if len(chosen) >= self.MAX_PACKETS or total + len(packet['text']) > self.MAX_TEXT_CHARS:
                        break
                    if image_count + len(packet.get('images', [])) > 3:
                        continue
                    chosen.append(packet)
                    image_count += len(packet.get('images', []))
                    selected_seqs.add(packet['seq'])
                    total += len(packet['text'])
                job['snapshot'] = sorted(copy.deepcopy(chosen), key=lambda item: item['seq'])
                job['expires'] = timestamp + self.LEASE_SECONDS
            return copy.deepcopy(job['snapshot'])

    def is_valid(self, scope, token):
        with self._lock:
            return not self._closed and self._get_job(scope, token, self._time()) is not None

    def keepalive(self, scope, token):
        """Renew an active generation/send lease without reviving a fenced job."""
        with self._lock:
            if self._closed:
                return False
            timestamp = self._time()
            job = self._get_job(scope, token, timestamp)
            if job is None:
                return False
            job['expires'] = timestamp + self.LEASE_SECONDS
            return True

    def finish(self, scope, token, sent=True):
        with self._lock:
            if self._closed:
                return False
            timestamp = self._time()
            job = self._get_job(scope, token, timestamp)
            if job is None:
                return False
            with self._transaction():
                self._jobs.pop(token, None)
                state = self._state(scope)
                if sent:
                    snapshot = job['snapshot']
                    if snapshot is None:
                        # A non-LLM command may have answered only its triggering message.
                        snapshot = [job['packet']]
                    confirmed = {packet['seq'] for packet in snapshot}
                    state['pending'] = [packet for packet in state['pending'] if packet['seq'] not in confirmed]
                    state['covered'] = (min(packet['seq'] for packet in state['pending']) - 1
                                        if state['pending'] else state['seq'])
                    state['last_reply'] = timestamp if state['last_reply'] is None else max(timestamp, state['last_reply'])
                    state['retry_at'] = 0.0
                    state['failures'] = 0
                    self._prune_seen(state)
                else:
                    self._backoff(state, timestamp)
        self._notify()
        return True

    def external_reply(self, scope):
        scope = self._scope_name(scope)
        with self._lock:
            if self._closed:
                return
            timestamp = self._time()
            with self._transaction():
                state = self._state(scope)
                state['last_reply'] = timestamp if state['last_reply'] is None else max(timestamp, state['last_reply'])
                # A command reply restarts the gap even if a deferred native event
                # had already been enqueued. Its input remains pending for later.
                for token, job in list(self._jobs.items()):
                    if job['scope'] == scope and not job['force']:
                        self._jobs.pop(token, None)
        self._notify()

    async def _dispatch_one(self, scope, payload, token):
        try:
            if self.is_valid(scope, token):
                await self.dispatch(scope, payload, token)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.finish(scope, token, sent=False)
            self._report(error)
        finally:
            self._dispatch_tasks.pop(token, None)

    async def pump(self):
        """Dispatch ready scopes once; useful with an injected clock in runtime tests."""
        deliveries = []
        with self._lock:
            if self._closed:
                return 0
            timestamp = self._time()
            with self._transaction():
                self._expire(timestamp)
                for scope, state in self._scopes.items():
                    if not state['pending'] or self._jobs_for(scope):
                        continue
                    try:
                        allowed = bool(self.can_reply(scope))
                        due = state['last_reply'] is None or timestamp >= state['last_reply'] + self._gap()
                    except Exception as error:
                        self._report(error)
                        self._backoff(state, timestamp)
                        continue
                    if not allowed or not due or timestamp < state['retry_at']:
                        continue
                    packet = state['pending'][-1]
                    token = self._new_job(scope, packet, False, timestamp)
                    deliveries.append((scope, {key: value for key, value in packet.items() if key != 'seq'}, token))
        for scope, payload, token in deliveries:
            task = asyncio.create_task(self._dispatch_one(scope, payload, token))
            self._dispatch_tasks[token] = task
        # A dispatch normally enqueues a native event and returns immediately.
        # Do not wait for generation here: leases and other scopes must remain live.
        await asyncio.sleep(0)
        return len(deliveries)

    def _next_delay(self):
        timestamp = self._time()
        delays = []
        for job in self._jobs.values():
            delays.append(max(0.01, job['expires'] - timestamp))
        for scope, state in self._scopes.items():
            if not state['pending'] or self._jobs_for(scope):
                continue
            try:
                if not self.can_reply(scope):
                    delays.append(max(1.0, self._number(self.quiet_delay())))
                    continue
                due = state['last_reply'] + self._gap() if state['last_reply'] is not None else timestamp
                delays.append(max(0.01, max(due, state['retry_at']) - timestamp))
            except Exception as error:
                self._report(error)
                delays.append(30.0)
        return min(delays) if delays else None

    async def _run(self):
        while self._running:
            self._wake.clear()
            try:
                await self.pump()
                with self._lock:
                    delay = self._next_delay()
                if delay is None:
                    await self._wake.wait()
                else:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._report(error)
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    async def start(self):
        if self._running:
            return
        self._closed = False
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._worker = asyncio.create_task(self._run(), name='whale-reply-queue')
        await asyncio.sleep(0)

    async def close(self):
        current = asyncio.current_task()
        with self._lock:
            self._closed = True
            self._running = False
            tokens = list(self._jobs)
            self._jobs.clear()
            try:
                self._save()
            except Exception as error:
                # A shutdown must still fence/drain tasks if the disk is full.
                # Last committed pending inputs remain available on restart.
                self._report(error)
            finally:
                for token in tokens:
                    self._cancel_dispatch(token)
                tasks = [task for task in self._dispatch_tasks.values() if task is not current]
                if self._worker is not None and self._worker is not current:
                    tasks.append(self._worker)
                for task in tasks:
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatch_tasks.clear()
        self._worker = None
        self._wake = None
        self._loop = None
