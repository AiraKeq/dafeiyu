"""Read-only v1.2.0 queue audit, with local standard-library fixtures only.

Run from F:\\work:
    python -B qq-deepseek-deploy/research/queue-audit-v120-readonly.py

This never edits source/existing tests, loads no SDK or provider, blocks socket
connections, and removes only its checked, freshly-created state directory.
An issue_present=true observation is a local deterministic reproduction.
Conditional integration observations are labelled separately.
"""
from __future__ import annotations

import __future__
import ast
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random
import re
import socket
import traceback
import sys
import tempfile
import time as real_time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
RESEARCH = Path(__file__).resolve().parent
PROJECT = RESEARCH.parent
PLUGIN = PROJECT / 'plugins' / 'astrbot_plugin_whale_memory'
UPSTREAM = PROJECT / 'upstream' / 'integration-review'
GROUP = '1000000004'
BOT = '1000000001'
OWNER = '1000000002'
SCOPE = 'aiocqhttp:GroupMessage:' + GROUP
TZ = timezone(timedelta(hours=8))


def instant(hour=12, minute=0, second=0, day=13):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ).timestamp()


class Logger:
    def __init__(self):
        self.records = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.records.append((name, args))
        return record


logger = Logger()


class ComponentType(Enum):
    Plain = 'Plain'
    Face = 'Face'
    Record = 'Record'
    Video = 'Video'
    At = 'At'
    Image = 'Image'
    Reply = 'Reply'
    Poke = 'Poke'
    Node = 'Node'
    Nodes = 'Nodes'
    File = 'File'
    Json = 'Json'
    Share = 'Share'
    Music = 'Music'
    Forward = 'Forward'
    Location = 'Location'
    Contact = 'Contact'
    Shake = 'Shake'
    Dice = 'Dice'
    RPS = 'RPS'
    Unknown = 'Unknown'


class Component:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Plain(Component):
    type = ComponentType.Plain

    def __init__(self, text):
        self.text = text


components = SimpleNamespace(Plain=Plain)
for name in ComponentType.__members__:
    if name != 'Plain':
        setattr(components, name, type(name, (Component,), {'type': ComponentType[name]}))


class ResultContentType(Enum):
    GENERAL_RESULT = 'general'
    LLM_RESULT = 'llm'
    STREAMING_RESULT = 'stream'
    STREAMING_FINISH = 'finish'


class Chain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])
        self.type = 'normal'
        self.result_content_type = ResultContentType.LLM_RESULT
        self.async_stream = None
        self.use_t2i_ = None
        self.stopped = False

    def get_plain_text(self):
        return ''.join(c.text for c in self.chain if isinstance(c, Plain))

    def derive(self, chain):
        return Chain(chain)

    def is_model_result(self):
        return self.result_content_type == ResultContentType.LLM_RESULT

    def is_llm_result(self):
        return self.is_model_result()

    def message(self, text):
        self.chain.append(Plain(text))
        return self

    def stop_event(self):
        self.stopped = True
        return self


class Event:
    def __init__(self, text, message_id, *, direct=False, sender='1234567890',
                 group=GROUP, self_id=BOT, platform='aiocqhttp', pieces=None):
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message_id=message_id, timestamp=instant(), message=list(pieces or [Plain(text)]),
            self_id=self_id, group_id=group, session_id=group or sender,
            sender=SimpleNamespace(user_id=sender, nickname='离线朋友'),
        )
        self.platform = platform
        self.sender = sender
        self.group = group
        self.self_id = self_id
        self.unified_msg_origin = platform + ':GroupMessage:' + group
        self.is_at_or_wake_command = direct
        self.is_wake = True
        self.call_llm = False
        self._has_send_oper = False
        self.plugins_name = None
        self.extras = {}
        self.result = None
        self.stopped = False
        self.trace = SimpleNamespace(record=lambda *args, **kwargs: None)
        self.delivered = []
        self.collector = None

    def get_extra(self, key=None, default=None):
        return self.extras if key is None else self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_platform_id(self):
        return self.platform

    def get_platform_name(self):
        return self.platform

    def get_group_id(self):
        return self.group

    def get_self_id(self):
        return self.self_id

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return '离线朋友'

    def get_result(self):
        return self.result

    def set_result(self, result):
        self.result = result

    def clear_result(self):
        self.result = None

    def stop_event(self):
        self.stopped = True
        if self.result is None:
            self.result = Chain()
        self.result.stop_event()

    def is_stopped(self):
        return self.stopped

    def plain_result(self, text):
        return Chain([Plain(text)])

    def _outline_chain(self, chain):
        return ''.join(getattr(c, 'text', '') for c in chain)

    async def send(self, message, *args, **kwargs):
        if self.collector is not None:
            return await self.collector(message, *args, **kwargs)
        self.delivered.append(message.get_plain_text())
        self._has_send_oper = True


class FakeContext:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.platform = SimpleNamespace(create_event=self.create_event)

    def get_platform_inst(self, platform_id):
        return self.platform if platform_id == 'aiocqhttp' else None

    def get_event_queue(self):
        return self.queue

    async def get_using_tts_provider_async(self, *args, **kwargs):
        return None

    def create_event(self, message):
        event = Event(message.message_str, message.message_id,
                      sender=message.sender.user_id, group=message.group_id,
                      self_id=message.self_id, pieces=message.message)
        event.message_obj = message
        return event


class ProviderRequest:
    def __init__(self, prompt='', system_prompt='', contexts=None):
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = list(contexts or [])


class LLMResponse:
    def __init__(self, role='assistant', completion_text='', is_chunk=False):
        self.role = role
        self.completion_text = completion_text
        self.is_chunk = is_chunk


def module(name, **items):
    obj = ModuleType(name)
    obj.__dict__.update(items)
    sys.modules[name] = obj
    if '.' in name:
        parent, child = name.rsplit('.', 1)
        if parent in sys.modules:
            setattr(sys.modules[parent], child, obj)
    return obj


def identity_decorator(*args, **kwargs):
    return lambda item: item


class Filters:
    EventMessageType = SimpleNamespace(ALL='all')

    def __getattr__(self, name):
        return identity_decorator


class Star:
    def __init__(self, context):
        self.context = context


class StarTools:
    current_dir = None

    @classmethod
    def get_data_dir(cls, name):
        return cls.current_dir

    @staticmethod
    async def create_message(**kwargs):
        return SimpleNamespace(**kwargs)


class Reviewer:
    def __init__(self, store, generate, base_style):
        self.base_style = base_style


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    target = importlib.util.module_from_spec(spec)
    sys.modules[name] = target
    spec.loader.exec_module(target)
    return target


def load_native_class(path, name, namespace):
    """Execute the existing upstream class, never a rewritten method imitation."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    namespace = dict(namespace, register_stage=lambda cls: cls, Stage=object)
    code = compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec',
                   flags=__future__.annotations.compiler_flag)
    exec(code, namespace)
    return namespace[name]


def install_targets():
    module('astrbot', logger=logger)
    module('astrbot.api', AstrBotConfig=dict, logger=logger)
    module('astrbot.api.event', AstrMessageEvent=Event, filter=Filters())
    module('astrbot.api.message_components', Plain=Plain)
    module('astrbot.api.platform', MessageMember=SimpleNamespace)
    module('astrbot.api.provider', LLMResponse=LLMResponse, ProviderRequest=ProviderRequest)
    module('astrbot.api.star', Context=FakeContext, Star=Star, StarTools=StarTools,
           register=identity_decorator)
    package = module('queue_audit_target')
    package.__path__ = [str(PLUGIN)]
    queue_module = load_file('queue_audit_target.reply_queue', PLUGIN / 'reply_queue.py')
    store_module = load_file('queue_audit_target.memory_store', PLUGIN / 'memory_store.py')
    module('queue_audit_target.daily_review', DailyReviewer=Reviewer)
    main_module = load_file('queue_audit_target.main', PLUGIN / 'main.py')

    async def call_hook(event, kind):
        await event.audit_plugin.remember_complete_reply(event)
        return False

    native_respond = load_native_class(UPSTREAM / 'pipeline' / 'respond' / 'stage.py',
        'RespondStage', {
            'Comp': components, 'ComponentType': ComponentType,
            'MessageChain': Chain, 'ResultContentType': ResultContentType,
            'asyncio': asyncio, 'math': math, 'random': random, 'logger': logger,
            'DEFAULT_LOG_BASE': 2.6, 'EventType': SimpleNamespace(OnAfterMessageSentEvent='after'),
            'call_event_hook': call_hook, 'path_Mapping': lambda mappings, value: value,
        })
    native_process = load_native_class(UPSTREAM / 'pipeline' / 'process_stage' / 'stage.py',
        'ProcessStage', {'FILTERS': [], 'USER_SESSIONS': {}, 'ProviderRequest': ProviderRequest})
    native_internal = load_native_class(
        UPSTREAM / 'pipeline' / 'process_stage' / 'method' / 'agent_sub_stages' / 'internal.py',
        'InternalAgentSubStage', {
            'asyncio': asyncio, 'logger': logger,
            **{name: getattr(components, name) for name in ('Image', 'File', 'Record', 'Video', 'Reply')},
        })
    return queue_module, main_module, native_respond, native_process, native_internal


class Fixture:
    def __init__(self, root, label, targets, **config):
        self.clock = [instant()]
        self.queue_module, self.main_module, self.respond_class, self.process_class, self.internal_class = targets
        StarTools.current_dir = root / label
        self.main_module.time = SimpleNamespace(time=lambda: self.clock[0])
        self.context = FakeContext()
        settings = {'enabled': True, 'group_ids': [GROUP], 'admin_ids': [OWNER],
                    'proactive_enabled': True, 'proactive_gap_seconds': 10,
                    'reply_start_hour': 8, 'reply_end_hour': 24,
                    'names': ['大肥鱼', '鲸鱼娘', '鲸鲸', '大鲸鱼', '小鲸鱼']}
        settings.update(config)
        self.plugin = self.main_module.WhaleMemory(self.context, settings)

    async def prepare(self, event):
        await self.plugin.observe(event)
        if not event.is_stopped():
            await self.plugin.inject(event, ProviderRequest(prompt=event.message_str))
        return event.get_extra('whale_reply_token')

    async def deliver(self, event, texts, collector=None, model_response=True):
        event.audit_plugin = self.plugin
        event.set_result(Chain([Plain(text) for text in texts]))
        if collector is not None:
            event.collector = collector
        if model_response and event.get_extra('whale_reply_token'):
            await self.plugin.remember_reply(event, LLMResponse(completion_text=''.join(texts)))
        await self.plugin.enforce_reply_hours(event)
        native = self.respond_class()
        native.config = {'provider_settings': {}}
        native.platform_settings = {}
        native.enable_seg = True
        native.only_llm_result = False
        native.interval_method = 'random'
        native.interval = [0, 0]
        if not event.is_stopped():
            await native.process(event)

    def pending(self):
        return [p['message_id'] for p in self.plugin.replies._state(SCOPE)['pending']]

    async def close(self):
        await self.plugin.replies.close()


def packet(message_id, text='离线消息'):
    return {'message_id': message_id, 'text': text, 'ts': instant(),
            'group_id': GROUP, 'platform_id': 'aiocqhttp', 'self_id': BOT,
            'session_id': GROUP, 'sender_id': '1234567890', 'sender_name': '离线朋友'}


def queue_at(path, target, clock, deliveries=None, allowed=None):
    async def collect(scope, payload, token):
        if deliveries is not None:
            deliveries.append((scope, payload, token))
    return target.ReplyQueue(path, collect, lambda: clock[0], lambda: 10,
                            allowed or (lambda scope: True), lambda: 1, lambda error: None)


def force_overlap(root, targets):
    clock = [instant()]
    q = queue_at(root / 'force-overlap.json', targets[0], clock)
    first = q.offer(SCOPE, packet('force-a'), force=True)
    a = q.claim(SCOPE, first)
    clock[0] += 1
    second = q.offer(SCOPE, packet('force-b'), force=True)
    b = q.claim(SCOPE, second)
    intersection = sorted({p['message_id'] for p in a} & {p['message_id'] for p in b})
    q.finish(SCOPE, second, sent=True)
    stale_valid = q.is_valid(SCOPE, first)
    return {'case': 'force_claim_overlap', 'issue_present': bool(intersection),
            'first_claim': [p['message_id'] for p in a],
            'second_claim': [p['message_id'] for p in b], 'overlap': intersection,
            'older_job_still_valid_after_newer_finish': stale_valid}


def finish_replace_failure(root, targets):
    clock = [instant()]
    q = queue_at(root / 'finish-failure.json', targets[0], clock)
    token = q.offer(SCOPE, packet('finish-a'))
    q.claim(SCOPE, token)
    clock[0] += 1
    raised = None
    with patch.object(targets[0].os, 'replace', side_effect=OSError('offline replacement failure')):
        try:
            q.finish(SCOPE, token, sent=True)
        except OSError as error:
            raised = str(error)
    in_memory = [p['message_id'] for p in q._state(SCOPE)['pending']]
    valid = q.is_valid(SCOPE, token)
    recovered = queue_at(q.path, targets[0], clock)
    on_disk = [p['message_id'] for p in recovered._state(SCOPE)['pending']]
    retry_finish = q.finish(SCOPE, token, sent=True)
    return {'case': 'finish_replace_failure', 'issue_present': in_memory != on_disk or not valid,
            'raised': raised, 'in_memory_pending': in_memory, 'reloaded_pending': on_disk,
            'token_valid': valid, 'second_finish': retry_finish,
            'in_memory_last_reply': q._state(SCOPE)['last_reply']}


async def external_cooldown(root, targets):
    f = Fixture(root, 'external-cooldown', targets)
    try:
        seed = Event('普通起始消息', 'external-seed')
        assert await f.prepare(seed)
        await f.deliver(seed, ['已经回复。'])
        started = f.clock[0]
        f.clock[0] = started + 1
        pending = Event('冷却中积累的消息', 'external-pending')
        await f.prepare(pending)
        assert pending.is_stopped()
        f.clock[0] = started + 10
        count = await f.plugin.replies.pump()
        assert count == 1
        deferred = f.context.queue.get_nowait()
        old_token = deferred.get_extra('whale_deferred')
        external = Event('鲸鱼记忆', 'external-command', sender=OWNER)
        await f.deliver(external, ['命令回复已经完成。'])
        external_finished = f.clock[0]
        valid_after_external = f.plugin.replies.is_valid(SCOPE, old_token)
        f.clock[0] += 0.1
        await f.prepare(deferred)
        accepted = not deferred.is_stopped()
        if accepted:
            await f.deliver(deferred, ['过早的普通回复。'])
        actual_gap = f.clock[0] - external_finished
        return {'case': 'external_reply_leaves_unclaimed_ordinary_job',
                'issue_present': accepted and bool(deferred.delivered) and actual_gap < 10,
                'ordinary_token_still_valid_after_external_reply': valid_after_external,
                'deferred_accepted': accepted, 'delivered': deferred.delivered,
                'seconds_after_external_reply': round(actual_gap, 6),
                'pending_after': f.pending()}
    finally:
        await f.close()


async def last_piece_expires(root, targets):
    f = Fixture(root, 'last-piece-expires', targets)
    try:
        event = Event('要求两段回答', 'lease-last')
        token = await f.prepare(event)
        assert token
        started = f.clock[0]
        f.clock[0] = started + 179
        delivered = []
        async def slow_last(message, *args, **kwargs):
            delivered.append(message.get_plain_text())
            if len(delivered) == 2:
                f.clock[0] = started + 181
        await f.deliver(event, ['完整第一段。', '完整最后一段。'], slow_last)
        pending = f.pending()
        retry_at = f.plugin.replies._state(SCOPE)['retry_at']
        f.clock[0] = max(retry_at, started + 191)
        retry_dispatches = await f.plugin.replies.pump()
        return {'case': 'last_send_returns_after_lease',
                'issue_present': len(delivered) == 2 and 'lease-last' in pending,
                'delivered': delivered, 'pending_after_complete_delivery': pending,
                'retry_seconds_from_claim': retry_at - started if retry_at else None,
                'duplicate_dispatches': retry_dispatches,
                'assistant_rows': f.plugin.store.statistics(SCOPE)['messages'] - 1}
    finally:
        await f.close()


async def native_send_failure(root, targets):
    f = Fixture(root, 'native-send-failure', targets)
    try:
        event = Event('生成一段回复', 'native-fail')
        assert await f.prepare(event)
        attempts = []
        async def fail_send(message, *args, **kwargs):
            attempts.append(message.get_plain_text())
            raise OSError('offline simulated send failure')
        await f.deliver(event, ['根本未送达。'], fail_send)
        return {'case': 'native_respond_swallow_send_exception',
                'issue_present': not f.pending(), 'attempts': attempts,
                'pending_after': f.pending(),
                'assistant_rows': f.plugin.store.statistics(SCOPE)['messages'] - 1,
                'send_failed_flag': event.get_extra('whale_send_failed'),
                'blocked_flag': event.get_extra('whale_reply_blocked')}
    finally:
        await f.close()


async def preempt_awaited_send(root, targets):
    f = Fixture(root, 'preempt-awaited-send', targets)
    try:
        old = Event('普通消息', 'preempt-old')
        entered = asyncio.Event()
        release = asyncio.Event()
        delivered = []
        async def waiting_send(message, *args, **kwargs):
            entered.set()
            await release.wait()
            delivered.append({'text': message.get_plain_text(),
                              'valid_when_irreversible_send_occurs': f.plugin.replies.is_valid(
                                  SCOPE, old.get_extra('whale_reply_token'))})
        async def original_pipeline():
            assert await f.prepare(old)
            await f.deliver(old, ['不应继续发的旧回答。'], waiting_send)
        task = asyncio.create_task(original_pipeline())
        await entered.wait()
        token = old.get_extra('whale_reply_token')
        force = Event('大肥鱼现在回答这条', 'preempt-new')
        assert await f.prepare(force)
        invalidated = not f.plugin.replies.is_valid(SCOPE, token)
        release.set()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        return {'case': 'inflight_send_not_cancelled_by_force',
                'issue_present': invalidated and bool(delivered),
                'native_pipeline_cancelled': cancelled,
                'invalidated_before_mock_api_send': invalidated, 'delivered': delivered,
                'boundary': 'await inside the original send before a mock irreversible API send'}
    finally:
        await f.close()


async def native_decorating_stage(f, event):
    handlers = SimpleNamespace(get_handlers_by_event_type=lambda *args, **kwargs: [
        SimpleNamespace(handler_module_path='queue_audit_target.main',
                        handler_name='enforce_reply_hours', handler=f.plugin.enforce_reply_hours)])
    cls = load_native_class(UPSTREAM / 'pipeline' / 'result_decorate' / 'stage.py',
        'ResultDecorateStage', {
            'logger': logger, 'traceback': traceback, 're': re, 'random': random,
            'ResultContentType': ResultContentType,
            'EventType': SimpleNamespace(OnDecoratingResultEvent='decorating'),
            'star_handlers_registry': handlers,
            'star_map': {'queue_audit_target.main': SimpleNamespace(name='whale_memory')},
            **{name: getattr(components, name) for name in ('At', 'Image', 'Json', 'Node', 'Plain', 'Record', 'Reply')},
        })
    native = cls()
    native.content_safe_check_reply = False
    native.content_safe_check_stage = None
    native.reply_prefix = ''
    native.enable_segmented_reply = True
    native.only_llm_result = False
    native.words_count_threshold = 1000000
    native.split_mode = 'regex'
    native.regex = r'^[\s\S]{1,20}$|[\s\S]{1,19}?[。！？!?\n]|[\s\S]{1,20}'
    native.content_cleanup_rule = ''
    native.show_reasoning = False
    native.forward_threshold = 1000000
    native.reply_with_mention = False
    native.reply_with_quote = False
    native.ctx = SimpleNamespace(plugin_manager=SimpleNamespace(context=f.context),
                                 astrbot_config={'provider_tts_settings': {'enable': False}, 't2i': False})
    async for _ in native.process(event):
        pass


async def night_finish_save_failure(root, targets):
    f = Fixture(root, 'night-finish-save-failure', targets)
    try:
        f.clock[0] = instant(23, 59, 59)
        event = Event('午夜前正常请求', 'night-finish-save')
        assert await f.prepare(event)
        event.audit_plugin = f.plugin
        event.set_result(Chain([Plain('午夜后禁止发送。')]))
        f.clock[0] = instant(0, day=14)
        with patch.object(f.plugin.replies, '_save', side_effect=OSError('offline durable-state failure')):
            await native_decorating_stage(f, event)
            after_decorating = {'stopped': event.is_stopped(),
                                'result_present': bool(event.get_result() and event.get_result().chain),
                                'guard_installed': bool(event.get_extra('whale_send_guard'))}
            native = f.respond_class()
            native.config = {'provider_settings': {}}
            native.platform_settings = {}
            native.enable_seg = True
            native.only_llm_result = False
            native.interval_method = 'random'
            native.interval = [0, 0]
            if not event.is_stopped():
                await native.process(event)
        return {'case': 'night_guard_fails_open_on_finish_save_error',
                'issue_present': bool(event.delivered), 'after_decorating': after_decorating,
                'blocked_flag': event.get_extra('whale_reply_blocked'),
                'send_clock': datetime.fromtimestamp(f.clock[0], TZ).isoformat(),
                'delivered': event.delivered}
    finally:
        await f.close()


async def night_inside_original_send(root, targets):
    f = Fixture(root, 'night-inside-original-send', targets)
    base = instant(23, 59, 59) + 0.25
    monotonic_start = real_time.monotonic()
    now = lambda: base + real_time.monotonic() - monotonic_start
    # Both the injected wall clock and asyncio deadlines advance together. This
    # can verify a real wait_for/send-deadline fix, not only pre-send predicates.
    f.main_module.time = SimpleNamespace(time=now)
    entered = asyncio.Event()
    release = asyncio.Event()
    delivered = []
    event = Event('午夜前开始生成的请求', 'night-internal-await')
    async def waiting_send(message, *args, **kwargs):
        entered.set()
        await release.wait()
        delivered.append({'text': message.get_plain_text(),
                          'awake_when_mock_api_called': f.plugin._awake(),
                          'send_clock': datetime.fromtimestamp(now(), TZ).isoformat()})
    async def pipeline():
        assert await f.prepare(event)
        await f.deliver(event, ['这条只能在午夜前发送。'], waiting_send)
    task = asyncio.create_task(pipeline())
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        await asyncio.sleep(0.85)
        assert now() >= instant(0, day=14)
        await f.plugin.replies.pump()
        release.set()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        return {'case': 'original_send_internal_await_crosses_midnight',
                'issue_present': bool(delivered) and not delivered[0]['awake_when_mock_api_called'],
                'native_pipeline_cancelled': cancelled, 'delivered': delivered,
                'pending_after': f.pending(),
                'assistant_rows': f.plugin.store.statistics(SCOPE)['messages'] - 1,
                'boundary': 'adapter awaits before the mock irreversible API call, not a delayed client receipt'}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await f.close()


async def whitelist_revocation(root, targets):
    f = Fixture(root, 'whitelist-revocation', targets)
    try:
        event = Event('原先在白名单内发起的请求', 'revoke-inflight')
        assert await f.prepare(event)
        await f.plugin.remember_reply(event, LLMResponse(completion_text='已经生成了回答。'))
        f.plugin.config['group_ids'] = []
        await f.deliver(event, ['白名单撤销后仍发送。'], model_response=False)
        return {'case': 'live_whitelist_revocation_not_rechecked_by_bound_guard',
                'conditional': True, 'issue_present': False,
                'scope_after_revocation': f.plugin._scope(event), 'delivered': event.delivered,
                'boundary': 'only applies if an existing config dict can change without plugin termination'}
    finally:
        await f.close()


async def proactive_off_direct(root, targets):
    f = Fixture(root, 'proactive-off-direct', targets, proactive_enabled=False)
    try:
        outcomes = []
        for index, event in enumerate((Event('大肥鱼回应我', 'off-alias'),
                                       Event('回应我', 'off-at', direct=True))):
            token = await f.prepare(event)
            outcomes.append({'type': 'alias' if index == 0 else 'at',
                             'stopped': event.is_stopped(), 'token': token})
        return {'case': 'proactive_off_also_blocks_direct_address',
                'issue_present': all(item['stopped'] for item in outcomes), 'outcomes': outcomes}
    finally:
        await f.close()


async def self_echo_process(root, targets):
    f = Fixture(root, 'self-echo-process', targets)
    try:
        calls = []
        class Agent:
            async def process(self, event):
                calls.append(event.message_str)
                yield None
        native = f.process_class()
        native.ctx = SimpleNamespace(astrbot_config={'provider_settings': {'enable': True}})
        native.agent_sub_stage = Agent()
        for index in range(3):
            event = Event('大肥鱼自己的唤醒回复', 'self-' + str(index), sender=BOT, direct=True)
            await f.plugin.observe(event)
            event.set_extra('activated_handlers', [])
            async for _ in native.process(event):
                pass
        return {'case': 'self_echo_reaches_native_llm', 'conditional': True,
                'issue_present': len(calls) == 3, 'native_llm_starts': len(calls),
                'precondition': 'adapter forwards a self echo; upstream ignore_bot_self_message is false',
                'queue_pending': f.pending()}
    finally:
        await f.close()


async def empty_at(root, targets):
    f = Fixture(root, 'empty-at', targets)
    try:
        event = Event('', 'empty-at', direct=True, pieces=[components.At(qq=BOT)])
        await f.plugin.observe(event)
        native = f.internal_class()
        native.streaming_response = False
        native.show_reasoning = False
        yields = []
        if not event.message_str.strip():
            async for item in native.process(event, ''):
                yields.append(item)
        return {'case': 'at_only_empty_plain_text',
                'issue_present': not event.get_extra('whale_reply_token') and not yields,
                'stopped_by_plugin': event.is_stopped(),
                'admitted_by_native_empty_check': bool(event.message_str.strip()),
                'native_internal_yields': len(yields), 'queue_pending': f.pending(),
                'boundary': 'literal @bot without accompanying Plain text'}
    finally:
        await f.close()


async def err_response_hook(root, targets):
    f = Fixture(root, 'err-response', targets)
    try:
        event = Event('需要真正模型回复的请求', 'err-query')
        assert await f.prepare(event)
        response = LLMResponse(role='err', completion_text='模拟模型错误。')
        await f.plugin.remember_reply(event, response)
        await f.deliver(event, [response.completion_text], model_response=False)
        return {'case': 'err_response_nonempty_result_is_acked', 'conditional': True,
                'issue_present': not f.pending(), 'pending_after': f.pending(),
                'delivered': event.delivered,
                'boundary': 'main hooks only; native run_agent error-to-result binding is not cached locally'}
    finally:
        await f.close()


async def terminate_native_leak(root, targets):
    f = Fixture(root, 'terminate-native-leak', targets)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def pipeline():
        event = Event('尚在模型生成中的请求', 'terminate-inflight')
        assert await f.prepare(event)
        entered.set()
        await release.wait()
    task = asyncio.create_task(pipeline())
    try:
        await entered.wait()
        tracked_before = len(getattr(f.plugin, '_reply_tasks', {}))
        await f.plugin.terminate()
        await asyncio.sleep(0)
        alive_after = not task.done()
        return {'case': 'terminate_does_not_cancel_tracked_native_pipeline',
                'issue_present': tracked_before > 0 and alive_after,
                'tracked_native_tasks_before': tracked_before,
                'native_pipeline_alive_after_terminate': alive_after,
                'queue_closed': f.plugin.replies._closed,
                'boundary': 'plugin termination/hot reload, not whole-process OS termination'}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await f.close()


async def close_save_failure(root, targets):
    clock = [instant()]
    q = queue_at(root / 'close-save-failure.json', targets[0], clock)
    await q.start()
    worker = q._worker
    raised = None
    try:
        with patch.object(q, '_save', side_effect=OSError('offline close durable-state failure')):
            try:
                await q.close()
            except OSError as error:
                raised = str(error)
        await asyncio.sleep(0)
        return {'case': 'close_save_error_skips_worker_cancellation',
                'issue_present': not worker.done(), 'raised': raised,
                'worker_alive_after_close': not worker.done(),
                'closed': q._closed, 'running_flag': q._running}
    finally:
        await q.close()


async def active_model_lease_boundary(root, targets):
    f = Fixture(root, 'active-model-lease', targets)
    heartbeat_present = hasattr(f.plugin, 'HEARTBEAT_SECONDS')
    if heartbeat_present:
        f.plugin.HEARTBEAT_SECONDS = 0.005  # Fixture acceleration only.
    await f.plugin.replies.start()  # Production-only heartbeat binding requires running.
    entered = asyncio.Event()
    release = asyncio.Event()
    event = Event('需要长时间模型思考的请求', 'active-model')
    async def pipeline():
        assert await f.prepare(event)
        entered.set()
        await release.wait()
    task = asyncio.create_task(pipeline())
    try:
        await entered.wait()
        start = f.clock[0]
        for _ in range(4):
            f.clock[0] += 60
            await asyncio.sleep(0.015)
        await f.plugin.replies.pump()
        await asyncio.sleep(0)
        valid = f.plugin.replies.is_valid(SCOPE, event.get_extra('whale_reply_token'))
        kept_alive = valid and not task.done()
        state = f.plugin.replies._state(SCOPE)
        return {'case': 'running_native_model_lease_heartbeat',
                'issue_present': heartbeat_present and not kept_alive,
                'heartbeat_present': heartbeat_present,
                'native_model_alive_beyond_original_180_seconds': kept_alive,
                'virtual_seconds_elapsed': f.clock[0] - start,
                'pending': f.pending(), 'retry_at': state['retry_at'],
                'boundary': 'real running queue and periodic heartbeat; only its fixture interval is accelerated'}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await f.close()


async def recent_dedup_boundary(root, targets):
    f = Fixture(root, 'recent-dedup-boundary', targets)
    try:
        first = Event('已被回复过的老消息', 'dedup-original')
        assert await f.prepare(first)
        await f.deliver(first, ['已经正确回答。'])
        q = f.plugin.replies
        saved_save = q._save
        # Isolate the same real state transitions while omitting repeated fsync
        # solely for fixture speed; persist the final state before replaying.
        q._save = lambda: None
        try:
            for index in range(q.RECENT_SEEN_LIMIT):
                f.clock[0] += 10
                token = q.offer(SCOPE, packet('later-' + str(index)))
                assert token
                q.claim(SCOPE, token)
                q.finish(SCOPE, token, sent=True)
        finally:
            q._save = saved_save
        q._save()
        original_seen = 'dedup-original' in q._state(SCOPE)['seen']
        first_still_in_memory = f.plugin.store.statistics(SCOPE)['messages'] == 2
        f.clock[0] += 10
        replay = Event('已被回复过的老消息', 'dedup-original')
        token = await f.prepare(replay)
        return {'case': 'recent_dedup_forgets_old_id_while_memory_row_remains',
                'conditional': True, 'issue_present': False,
                'old_id_in_queue_seen': original_seen,
                'old_message_still_in_memory': first_still_in_memory,
                'duplicate_gets_new_reply_token': bool(token),
                'elapsed_seconds': q.RECENT_SEEN_LIMIT * 10 + 10,
                'boundary': 'dedup is explicitly limited to 2048 completed IDs; no duration contract was stated'}
    finally:
        await f.close()


async def randomized_claim_and_restart_controls(root, targets):
    clock = [instant()]
    deliveries = []
    q = queue_at(root / 'concurrency-control.json', targets[0], clock, deliveries)
    q.external_reply(SCOPE)
    for index in range(50):
        assert q.offer(SCOPE, packet('ordinary-' + str(index))) is None
    tokens = [q.offer(SCOPE, packet('direct-' + str(index)), force=True) for index in range(40)]
    order = list(tokens)
    random.Random(429).shuffle(order)
    seen_sequences = set()
    all_disjoint = True
    for token in order:
        batch = q.claim(SCOPE, token)
        selected = {p['seq'] for p in batch}
        all_disjoint &= not bool(seen_sequences & selected)
        seen_sequences |= selected
    for token in reversed(order):
        assert q.finish(SCOPE, token, sent=True)
    assert all_disjoint
    while q._state(SCOPE)['pending']:
        clock[0] += 10
        assert await q.pump() == 1
        scope, payload, token = deliveries.pop()
        q.claim(scope, token)
        q.finish(scope, token, sent=True)
    assert len(q._state(SCOPE)['seen']) == 90
    clock[0] += 1
    assert q.offer(SCOPE, packet('restart-pending')) is None
    await q.close()
    recovered = queue_at(q.path, targets[0], clock, deliveries)
    await recovered.start()
    assert not deliveries
    # Wake the real worker with the injected clock after the durable cooldown.
    clock[0] += 9
    recovered._notify()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await recovered.pump()
    assert len(deliveries) == 1
    scope, payload, token = deliveries.pop()
    assert [p['message_id'] for p in recovered.claim(scope, token)] == ['restart-pending']
    recovered.finish(scope, token, sent=True)
    assert recovered.offer(SCOPE, packet('restart-pending')) is None
    await recovered.close()
    return {'case': 'multi_force_and_restart_controls', 'passed': [
        '40 concurrent force reservations and randomized claims have disjoint input sequences',
        '50 ordinary inputs plus all direct inputs finish without lost pending sequences',
        'restart preserves gap, pending input and same-ID dedup without a fresh QQ message']}


async def controls(root, targets):
    f = Fixture(root, 'controls', targets)
    try:
        seed = Event('正常发言', 'control-seed')
        assert await f.prepare(seed)
        await f.deliver(seed, ['完整回复。'])
        started = f.clock[0]
        pending = Event('普通冷却消息', 'control-pending')
        await f.prepare(pending)
        assert pending.is_stopped()
        f.clock[0] = started + 9.999
        assert await f.plugin.replies.pump() == 0
        f.clock[0] = started + 10
        assert await f.plugin.replies.pump() == 1
        replay = f.context.queue.get_nowait()
        assert await f.prepare(replay)
        await f.deliver(replay, ['到期自动完整回复。'])
        assert not f.pending()
        repeated = Event('普通冷却消息', 'control-pending')
        assert not await f.prepare(repeated)
        assert repeated.is_stopped()
        outside = Event('白名单外的消息', 'outside', group='999999999')
        await f.plugin.observe(outside)
        assert f.plugin.store.statistics()['messages'] == 4
        f.clock[0] = instant(23, 59, 50)
        q = f.plugin.replies
        q.external_reply(SCOPE)
        q.offer(SCOPE, packet('control-overnight'))
        f.clock[0] = instant(0, day=14)
        assert await q.pump() == 0
        f.clock[0] = instant(8, day=14)
        assert await q.pump() == 1
        return {'case': 'baseline_controls', 'passed': [
            'ordinary deadline exact at 10 seconds without fresh input',
            'same-message dedup within recent window', 'group whitelist isolation',
            'midnight suppresses timer and 08:00 resumes persisted backlog']}
    finally:
        await f.close()


async def main():
    source_paths = [PLUGIN / 'reply_queue.py', PLUGIN / 'main.py']
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    print(json.dumps({'source_sha256': before, 'network': 'socket connections blocked',
                      'targets': [str(p) for p in source_paths]}, ensure_ascii=False))
    attempts = []
    def forbid_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('Network access is forbidden in the read-only audit')
    temporary = tempfile.TemporaryDirectory(prefix='queue-audit-state-', dir=str(RESEARCH))
    root = Path(temporary.name).resolve()
    assert root.parent == RESEARCH.resolve() and root.name.startswith('queue-audit-state-')
    try:
        with ExitStack() as guards:
            for obj, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                              (socket, 'create_connection'), (socket, 'getaddrinfo')):
                guards.enter_context(patch.object(obj, name, side_effect=forbid_network))
            targets = install_targets()
            for case in (force_overlap, finish_replace_failure):
                print(json.dumps(case(root, targets), ensure_ascii=False))
            for case in (external_cooldown, last_piece_expires, native_send_failure,
                         preempt_awaited_send, night_finish_save_failure,
                         night_inside_original_send, whitelist_revocation,
                         proactive_off_direct, self_echo_process,
                         empty_at, err_response_hook, terminate_native_leak,
                         close_save_failure, active_model_lease_boundary,
                         recent_dedup_boundary, randomized_claim_and_restart_controls, controls):
                print(json.dumps(await case(root, targets), ensure_ascii=False))
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
        print(json.dumps({'source_unchanged_during_run': before == after,
                          'ending_sha256': after, 'network_attempts': len(attempts)}, ensure_ascii=False))
        assert not attempts
    finally:
        # Cleanup is limited to this exact verified, newly-created fixture root.
        assert root.parent == RESEARCH.resolve() and root.name.startswith('queue-audit-state-')
        temporary.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
