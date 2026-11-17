"""Scored segmentation through the real, isolated AstrBot native send stages.

Run only in the disposable network-disabled container, by the parent agent:
    docker exec -e WHALE_REHEARSAL=1 qq-memory-rehearsal python \
        /AstrBot/data/plugins/astrbot_plugin_whale_memory/tests/rehearsal_segmentation.py

The host must only py_compile this file, never import or run it.  Compose the
existing RuntimeRehearsal's setup/teardown/helpers; do not inherit its TestCase
or expose an imported TestCase to discovery, which would repeat its tests.
All runtime data stays in the helper's TemporaryDirectory.  Native send calls
are local collectors, providers stay unused, and the helper blocks networking.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
if os.environ.get('WHALE_REHEARSAL') != '1':
    raise RuntimeError('Run only in the disposable rehearsal container with WHALE_REHEARSAL=1.')

# Keep the original TestCase inside its module, not in this module's globals.
import rehearsal_runtime as runtime_rehearsal


class SegmentationNativeRehearsal(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def fixture(self, **configuration):
        runtime = runtime_rehearsal.RuntimeRehearsal()
        await runtime.asyncSetUp()
        try:
            # Select deterministically for native delivery assertions; pure unit
            # tests cover micro-random selection. Enabled/weights use real defaults.
            runtime.config['segment_score_micro_random'] = False
            runtime.config.update(configuration)
            yield runtime
        finally:
            try:
                self.assertEqual(runtime.context.model_calls, [], 'No extra model call is permitted.')
            finally:
                await runtime.asyncTearDown()

    def temporary_path(self, runtime, path):
        path = Path(path).resolve()
        self.assertTrue(path.is_relative_to(Path(runtime.temporary.name).resolve()))
        return path

    def journal(self, runtime):
        path = self.temporary_path(runtime, runtime.plugin.segment_state_path)
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def queue_state(self, runtime):
        path = self.temporary_path(runtime, runtime.plugin.replies.path)
        return json.loads(path.read_text(encoding='utf-8'))['scopes'][runtime_rehearsal.SCOPE_A]

    def assert_one_complete_memory(self, runtime, event, text):
        rows = [row for row in runtime.rows() if row['message_id'] == runtime.reply_id(event)]
        self.assertEqual(len(rows), 1, 'Remember the full response once, not one row per bubble.')
        self.assertEqual(rows[0]['role'], 'assistant')
        # Existing retention policy trims only the outside and caps at 4000;
        # native delivery below must still preserve every original character.
        self.assertEqual(rows[0]['text'], text.strip()[:4000])

    async def native(self, runtime, text, *, incoming='今天一起散步好吗？', event=None,
                     result=None, after_send=None):
        if event is None:
            event = runtime.event(incoming, 'segmented-' + str(len(runtime.events)), direct=True)
        event.set_result(result if result is not None else runtime_rehearsal.MessageEventResult().message(text))
        sent = []
        chains = []

        async def collect(message, *args, **kwargs):
            plain = [item.text for item in message.chain if isinstance(item, runtime_rehearsal.Plain)]
            plan = event.get_extra('whale_segment_plan')
            if plan is not None:
                index = len(sent)
                self.assertEqual(event.get_extra('whale_segment_cursor'), index)
                self.assertEqual(plain, [plan['segments'][index]],
                                 'Restore stripped whitespace before the actual protocol call.')
            sent.append(''.join(plain))
            chains.append(tuple((type(item).__name__, str(getattr(item, 'qq', '')))
                                for item in message.chain))
            if after_send is not None:
                await after_send(event, sent)

        await runtime.native_segmented_delivery(event, collect)
        return event, sent, chains

    def assert_scored_complete(self, runtime, event, text, sent):
        plan = event.get_extra('whale_segment_plan')
        self.assertIsInstance(plan, dict, 'A safe plain response must take the scoring path.')
        self.assertEqual(''.join(sent), text)
        self.assertEqual(sent, plan['segments'], 'Native regex must not resplit the planned bubbles.')
        self.assertTrue(all(1 <= len(piece) <= 20 for piece in sent))
        self.assertEqual(plan['source_chars'], len(text))
        self.assertEqual(plan['chosen_count'], len(sent))
        self.assertEqual(event.get_extra('whale_segment_cursor'), plan['chosen_count'])
        self.assertEqual(event.get_extra('whale_send_count'), plan['chosen_count'])
        self.assertFalse(event.get_extra('whale_reply_blocked'))
        self.assertFalse(event.get_extra('whale_send_failed'))
        state = self.queue_state(runtime)
        self.assertEqual(state['pending'], [])
        self.assertEqual(state['covered'], state['seq'])
        self.assert_one_complete_memory(runtime, event, text)
        log = self.journal(runtime)
        self.assertIsNotNone(log, 'Only a fully successful native response gets a score journal.')
        scope = log['scopes'][runtime_rehearsal.SCOPE_A]
        self.assertEqual(sum(scope['counts'].values()), 1)
        bucket = str(plan['chosen_count']) if plan['chosen_count'] <= 3 else 'long'
        self.assertEqual(scope['counts'][bucket], 1)
        self.assertEqual(scope['last']['segments'], plan['chosen_count'])
        self.assertEqual(scope['last']['chars'], len(text))
        return plan

    def assert_legacy_complete(self, runtime, event, text, sent, expected_delivery=None):
        self.assertIsNone(event.get_extra('whale_segment_plan'))
        self.assertEqual(''.join(sent), text if expected_delivery is None else expected_delivery)
        self.assertTrue(all(1 <= len(piece) <= 20 for piece in sent if piece))
        self.assertEqual(event.get_extra('whale_send_count'), len(sent))
        self.assertFalse(event.get_extra('whale_reply_blocked'))
        self.assert_one_complete_memory(runtime, event, text)
        state = self.queue_state(runtime)
        self.assertEqual(state['pending'], [])
        self.assertEqual(state['covered'], state['seq'])

    def assert_not_acknowledged(self, runtime, event):
        self.assertTrue(event.is_stopped())
        self.assertTrue(event.get_extra('whale_reply_blocked'))
        self.assertFalse(any(row['message_id'] == runtime.reply_id(event) for row in runtime.rows()))
        self.assertIsNone(self.journal(runtime), 'Failed/partial/forged completion must not log success.')
        state = self.queue_state(runtime)
        self.assertEqual(state['covered'], 0)
        self.assertIsNone(state['last_reply'])
        self.assertIn(str(event.message_obj.message_id),
                      [packet['message_id'] for packet in state['pending']],
                      'Unanswered source input must remain available for retry.')

    async def test_short_ordinary_multisentence_reply_stays_one_scored_send(self):
        async with self.fixture() as runtime:
            text = '花开了。风来了。一起走呀。'
            self.assertLessEqual(len(text), 20)
            event, sent, _ = await self.native(runtime, text)
            plan = self.assert_scored_complete(runtime, event, text, sent)
            self.assertEqual(sent, [text])
            self.assertEqual(plan['chosen_count'], 1)
            for actual, expected in zip(plan['weights'], (0.6, 0.3, 0.1)):
                self.assertAlmostEqual(actual, expected, places=12)

    async def test_capacity_edges_and_long_chinese_deliver_the_entire_plan(self):
        long_text = ('春风拂过花海' * 1000)[:2044] + '末尾保全'
        self.assertEqual(len(long_text), 2048)
        for text in ['春' * length for length in (21, 40, 41, 60, 61)] + [long_text]:
            with self.subTest(length=len(text)):
                async with self.fixture() as runtime:
                    event, sent, _ = await self.native(runtime, text)
                    plan = self.assert_scored_complete(runtime, event, text, sent)
                    self.assertGreaterEqual(len(sent), (len(text) + 19) // 20)
                    self.assertEqual(sent[-1][-1], text[-1])
                    if len(text) > 60:
                        self.assertEqual(plan['mode'], 'long')
                        self.assertGreater(len(sent), 3)
                    if len(text) == 60:
                        self.assertEqual([len(piece) for piece in sent], [20, 20, 20])

    async def test_spaces_indentation_and_crlf_survive_native_stripping_without_resplit(self):
        fixtures = (
            '  春风来了，花开了！  ',
            '第一句。\n    第二句？\n\t第三句！\n',
            '\r\n    一、准备材料。\r\n    二、慢慢搅拌。\r\n\t三、等待完成。  ',
            '甲  乙   丙。\n\n  丁\t戊；己……庚——辛。  ',
        )
        for text in fixtures:
            with self.subTest(text=repr(text)):
                async with self.fixture() as runtime:
                    event, sent, _ = await self.native(runtime, text)
                    self.assert_scored_complete(runtime, event, text, sent)
                    self.assertEqual(''.join(sent), text)

    async def test_weight_three_100_selects_three_complete_natural_bubbles(self):
        async with self.fixture(segment_weight_1=0, segment_weight_2=0, segment_weight_3=100) as runtime:
            clauses = ['风来了呀。', '花也开了。', '一起散步吧。']
            text = ''.join(clauses)
            event, sent, _ = await self.native(runtime, text)
            plan = self.assert_scored_complete(runtime, event, text, sent)
            self.assertEqual(plan['chosen_count'], 3)
            self.assertEqual(list(plan['weights']), [0.0, 0.0, 1.0])
            self.assertEqual(sent, clauses)
            self.assertTrue(all(piece.strip() for piece in sent))

    async def test_disabled_rollback_and_planner_exception_keep_the_legacy_safe_path(self):
        async with self.fixture(segment_score_enabled=False) as runtime:
            text = '关闭评分后保留原生发送。后半句仍然要完整留下。'
            with patch.object(runtime_rehearsal.plugin_module, 'plan_segments',
                              side_effect=AssertionError('Disabled planner must not run')) as planner:
                event, sent, _ = await self.native(runtime, text)
            planner.assert_not_called()
            self.assert_legacy_complete(runtime, event, text, sent)
            self.assertIsNone(self.journal(runtime))

            runtime.config['segment_score_enabled'] = True
            enabled_text = '这一轮正常启用评分。'
            enabled, enabled_sent, _ = await self.native(runtime, enabled_text)
            self.assert_scored_complete(runtime, enabled, enabled_text, enabled_sent)
            saved = self.journal(runtime)
            runtime.config['segment_score_enabled'] = False
            rollback_text = '配置回滚后走原生路径。尾部仍然安全保留。'
            with patch.object(runtime_rehearsal.plugin_module, 'plan_segments',
                              side_effect=AssertionError('Rolled-back planner must not run')) as planner:
                event, sent, _ = await self.native(runtime, rollback_text)
            planner.assert_not_called()
            self.assert_legacy_complete(runtime, event, rollback_text, sent)
            self.assertEqual(self.journal(runtime), saved, 'Rollback must not add a scored success.')

        async with self.fixture() as runtime:
            text = '规划器异常也不丢回复。第二句保持二十字安全分段。最后检查尾部。'
            with patch.object(runtime_rehearsal.plugin_module, 'plan_segments',
                              side_effect=RuntimeError('Offline fixture planner failure')) as planner:
                event, sent, _ = await self.native(runtime, text)
            self.assertEqual(planner.call_count, 1)
            self.assertEqual(event.get_extra('whale_segment_fallback'), 'RuntimeError')
            self.assert_legacy_complete(runtime, event, text, sent)
            self.assertIsNone(self.journal(runtime))

    async def test_invalid_config_weights_fall_back_to_normalized_60_30_10(self):
        for weights in ((True, 30, 10), (float('nan'), 30, 10), (0, 0, 0), (-1, 30, 10)):
            with self.subTest(weights=repr(weights)):
                async with self.fixture(segment_weight_1=weights[0], segment_weight_2=weights[1],
                                        segment_weight_3=weights[2]) as runtime:
                    original = runtime_rehearsal.plugin_module.plan_segments
                    with patch.object(runtime_rehearsal.plugin_module, 'plan_segments', wraps=original) as planner:
                        text = '风来了呀。花也开了。一起散步吧。'
                        event, sent, _ = await self.native(runtime, text)
                    self.assertEqual(planner.call_count, 2)
                    self.assertEqual(planner.call_args_list[-1].kwargs['weights'], (60, 30, 10))
                    plan = self.assert_scored_complete(runtime, event, text, sent)
                    for actual, expected in zip(plan['weights'], (0.6, 0.3, 0.1)):
                        self.assertAlmostEqual(actual, expected, places=12)

    async def test_mixed_header_and_nonplain_result_keep_legacy_components_without_scoring(self):
        for header_first in (True, False):
            with self.subTest(header_first=header_first):
                async with self.fixture() as runtime:
                    header = runtime_rehearsal.At(qq=runtime_rehearsal.ADMIN)
                    # Keep the text below 20 even if legacy native handling
                    # batches mixed components; this case checks passthrough,
                    # not a new segmentation policy for non-Plain results.
                    parts = [runtime_rehearsal.Plain('头部后的正文。'),
                             runtime_rehearsal.Plain('后续也留下。')]
                    chain = [header, *parts] if header_first else [parts[0], header, parts[1]]
                    result = runtime_rehearsal.MessageEventResult(chain=chain)
                    text = result.get_plain_text()
                    with patch.object(runtime_rehearsal.plugin_module, 'plan_segments',
                                      side_effect=AssertionError('Mixed result must not be scored')) as planner:
                        event, sent, chains = await self.native(runtime, text, result=result)
                    planner.assert_not_called()
                    # Native get_plain_text inserts a display separator between
                    # separate Plain components; the existing protocol sender
                    # concatenates their actual texts. Test both legacy contracts.
                    delivery = ''.join(item.text for item in parts)
                    self.assert_legacy_complete(runtime, event, text, sent, expected_delivery=delivery)
                    self.assertTrue(any(('At', runtime_rehearsal.ADMIN) in piece for piece in chains))
                    self.assertIsNone(self.journal(runtime))

    async def test_forged_after_sent_cannot_ack_when_segment_cursor_is_incomplete(self):
        for cursor in (0, 2):
            with self.subTest(cursor=cursor):
                async with self.fixture(segment_weight_1=0, segment_weight_2=0, segment_weight_3=100) as runtime:
                    text = '先准备好杯子。再慢慢倒清水。最后轻轻搅拌。'
                    event = runtime.event('请说明三个步骤。', 'forged-after', direct=True)
                    await runtime.prepare_reply(event)
                    event.set_result(runtime_rehearsal.MessageEventResult().message(text))
                    await runtime.plugin.remember_reply(event, runtime_rehearsal.LLMResponse(
                        role='assistant', completion_text=text))
                    await runtime.decorating_hooks(event)
                    plan = event.get_extra('whale_segment_plan')
                    self.assertIsInstance(plan, dict)
                    self.assertEqual(plan['chosen_count'], 3)
                    event.set_extra('whale_send_count', 3)  # A fake count alone proves nothing.
                    event.set_extra('whale_segment_cursor', cursor)
                    await runtime.plugin.remember_complete_reply(event)
                    self.assertTrue(event.get_extra('whale_reply_blocked'))
                    self.assertTrue(event.is_stopped())
                    self.assert_not_acknowledged(runtime, event)

    async def test_native_first_partial_errors_midnight_and_force_never_log_partial_success(self):
        for failure in ('first-error', 'partial-error', 'midnight', 'force'):
            with self.subTest(failure=failure):
                async with self.fixture() as runtime:
                    if failure == 'midnight':
                        runtime.clock[0] = runtime_rehearsal.instant(23, 59, 50)
                    event = runtime.event('普通聊天触发这条较长说明。', 'failed-' + failure,
                                          direct=failure != 'force')
                    await runtime.prepare_reply(event)
                    old_token = event.get_extra('whale_reply_token')
                    text = '第一小段已经准备好。' + '这段合成说明必须保留，但过期或失败时不能继续发送。' * 3
                    event.set_result(runtime_rehearsal.MessageEventResult().message(text))
                    attempts = []
                    sent = []
                    forced = []

                    async def collect(message, *args, **kwargs):
                        piece = ''.join(item.text for item in message.chain
                                        if isinstance(item, runtime_rehearsal.Plain))
                        attempts.append(piece)
                        plan = event.get_extra('whale_segment_plan')
                        self.assertIsInstance(plan, dict)
                        self.assertEqual(event.get_extra('whale_segment_cursor'), len(sent))
                        self.assertEqual(piece, plan['segments'][len(sent)])
                        self.assertIsNone(self.journal(runtime))
                        if failure == 'first-error' or (failure == 'partial-error' and len(sent) == 1):
                            raise OSError('Offline synthetic send failure')
                        sent.append(piece)
                        if failure == 'midnight':
                            runtime.clock[0] = runtime_rehearsal.instant(0, day=14)
                        elif failure == 'force' and len(sent) == 1:
                            incoming = runtime.event('大肥鱼现在回答这个。', 'forced-new', direct=True)
                            await runtime.prepare_reply(incoming)
                            forced.append(incoming)

                    await runtime.native_segmented_delivery(event, collect)
                    plan = event.get_extra('whale_segment_plan')
                    self.assertIsInstance(plan, dict)
                    expected = 0 if failure == 'first-error' else 1
                    self.assertEqual(len(sent), expected)
                    self.assertEqual(event.get_extra('whale_send_count', 0), expected)
                    self.assertEqual(event.get_extra('whale_segment_cursor'), expected)
                    self.assertLess(expected, plan['chosen_count'])
                    self.assertEqual(len(attempts), 2 if failure == 'partial-error' else 1)
                    self.assertTrue(event.get_extra('whale_reply_blocked'))
                    self.assertFalse(runtime.plugin.replies.is_valid(runtime_rehearsal.SCOPE_A, old_token))
                    if failure.endswith('error'):
                        self.assertTrue(event.get_extra('whale_send_failed'))
                    self.assert_not_acknowledged(runtime, event)
                    await runtime.plugin.remember_complete_reply(event)
                    self.assert_not_acknowledged(runtime, event)
                    if failure == 'force':
                        self.assertEqual(len(forced), 1)
                        replacement_text = '好呀，听见啦。'
                        replacement, replacement_sent, _ = await self.native(
                            runtime, replacement_text, event=forced[0])
                        self.assert_scored_complete(runtime, replacement, replacement_text, replacement_sent)
                        self.assertEqual(replacement_sent, [replacement_text])
                        self.assertFalse(any(row['message_id'] == runtime.reply_id(event) for row in runtime.rows()))

    async def test_success_journal_is_metadata_only_incremented_once_and_memory_is_whole(self):
        async with self.fixture() as runtime:
            incoming = 'INPUT_PRIVACY_SENTINEL_8049'
            text = '这是输出排练标记。OUTPUT_PRIVACY_SENTINEL_8049，正文只用于离线排练。'

            async def before_success_hook(event, sent):
                self.assertIsNone(self.journal(runtime), 'No durable score log before the whole response succeeds.')
                self.assertEqual(event.get_extra('whale_segment_cursor'), len(sent) - 1)

            event, sent, _ = await self.native(runtime, text, incoming=incoming, after_send=before_success_hook)
            plan = self.assert_scored_complete(runtime, event, text, sent)
            log = self.journal(runtime)
            self.assertEqual(set(log), {'version', 'scopes'})
            self.assertEqual(log['version'], 1)
            scope = log['scopes'][runtime_rehearsal.SCOPE_A]
            self.assertEqual(set(scope), {'counts', 'last'})
            last = scope['last']
            self.assertEqual(set(last), {'segments', 'chars', 'scene', 'mode', 'weights',
                                        'exception', 'candidates', 'sent_at'})
            self.assertIsInstance(last['segments'], int)
            self.assertEqual(last['scene'], plan['scene'])
            self.assertEqual(last['mode'], plan['mode'])
            self.assertEqual(list(last['weights']), list(plan['weights']))
            self.assertEqual(last['exception'], plan['exception'])
            self.assertEqual(len(last['candidates']), len(plan['candidates']))
            fields = {'count', 'prior', 'scene_fit', 'completeness', 'naturalness', 'score'}
            for saved, candidate in zip(last['candidates'], plan['candidates']):
                self.assertEqual(set(saved), fields)
                self.assertEqual(saved, {key: candidate[key] for key in fields})
            raw = self.temporary_path(runtime, runtime.plugin.segment_state_path).read_text(encoding='utf-8')
            for secret in (incoming, text, 'INPUT_PRIVACY_SENTINEL', 'OUTPUT_PRIVACY_SENTINEL'):
                self.assertNotIn(secret, raw)
            self.assertNotIn('boundaries', raw)
            await runtime.plugin.remember_complete_reply(event)
            await runtime.plugin.remember_complete_reply(event)
            self.assertEqual(self.journal(runtime), log, 'Repeated after_sent must not increment counts twice.')
            self.assert_one_complete_memory(runtime, event, text)
            self.assertEqual(len([row for row in runtime.rows() if row['role'] == 'assistant']), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
