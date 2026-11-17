"""Additional offline queue-safety rehearsal using the real AstrBot stages.

Run only inside the disposable, network-disabled rehearsal container:
    docker exec -e WHALE_REHEARSAL=1 qq-memory-rehearsal python \
      /AstrBot/data/plugins/astrbot_plugin_whale_memory/tests/rehearsal_queue_safety.py

On the host, only py_compile syntax verification is permitted.  Importing this
module intentionally fails without WHALE_REHEARSAL=1, before importing AstrBot.
All events, configuration, context, storage, and network-denial fixtures come
from rehearsal_runtime.  No live provider, adapter, or deployment data is used.

The new TestCase borrows helper functions rather than inheriting RuntimeRehearsal
or importing that TestCase into this module's globals.  Both direct execution
and unittest discovery therefore collect only these additional safety cases,
never the original runtime rehearsal's tests.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
if os.environ.get("WHALE_REHEARSAL") != "1":
    raise RuntimeError("Run only in the disposable rehearsal container with WHALE_REHEARSAL=1.")

_TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TEST_DIR))
import rehearsal_runtime as runtime

# These are the actual classes imported by the isolated runtime fixture.
from astrbot.core.astr_main_agent import _matches_provider_wake_prefix
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter

FakeEvent = runtime.FakeEvent
FakeConfig = runtime.FakeConfig
FakeContext = runtime.FakeContext
LLMResponse = runtime.LLMResponse
ProviderRequest = runtime.ProviderRequest
MessageEventResult = runtime.MessageEventResult
At = runtime.At
Plain = runtime.Plain
SCOPE_A = runtime.SCOPE_A
BOT = runtime.BOT
GROUP_A = runtime.GROUP_A
GROUP_B = runtime.GROUP_B


class QueueSafetyRehearsal(unittest.IsolatedAsyncioTestCase):
    # Reuse only the existing fixture/helper functions, not its test methods.
    _patch = runtime.RuntimeRehearsal._patch
    _deny_network = runtime.RuntimeRehearsal._deny_network
    event = runtime.RuntimeRehearsal.event
    rows = runtime.RuntimeRehearsal.rows
    complete_fixture_reply = runtime.RuntimeRehearsal.complete_fixture_reply
    prepare_reply = runtime.RuntimeRehearsal.prepare_reply
    reply_id = runtime.RuntimeRehearsal.reply_id
    decorating_hooks = runtime.RuntimeRehearsal.decorating_hooks
    native_segmented_delivery = runtime.RuntimeRehearsal.native_segmented_delivery

    async def asyncSetUp(self):
        self.safety_tasks = []
        await runtime.RuntimeRehearsal.asyncSetUp(self)
        self.assertIsInstance(self.config, FakeConfig)
        self.assertIsInstance(self.context, FakeContext)

    async def asyncTearDown(self):
        try:
            for task in self.safety_tasks:
                if not task.done():
                    task.cancel()
            if self.safety_tasks:
                await asyncio.gather(*self.safety_tasks, return_exceptions=True)
            await self._settle_callbacks()
        finally:
            await runtime.RuntimeRehearsal.asyncTearDown(self)

    def _spawn(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self.safety_tasks.append(task)
        return task

    async def _settle_callbacks(self):
        # Event-loop barriers only; no real cooldown/lease sleep or polling.
        loop = asyncio.get_running_loop()
        for _ in range(2):
            barrier = loop.create_future()
            loop.call_soon(barrier.set_result, None)
            await barrier

    def _queue_state(self, scope=SCOPE_A):
        path = self.plugin.replies.path
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["scopes"].get(scope)

    def _pending_ids(self, scope=SCOPE_A):
        state = self._queue_state(scope)
        return [] if state is None else [item["message_id"] for item in state["pending"]]

    def _assistant_rows(self):
        return [row for row in self.rows() if row["role"] == "assistant"]

    def _actual_reply_timestamp(self, scope=SCOPE_A):
        # Only the inherited fixture's fresh temporary database is opened.
        with sqlite3.connect(str(self.plugin.store.db_path)) as connection:
            row = connection.execute(
                "SELECT last_reply FROM reply_state WHERE scope=?", (scope,)).fetchone()
        return None if row is None else row[0]

    def _assert_unacknowledged(self, message_ids):
        self.assertEqual(self._pending_ids(), list(message_ids))
        state = self._queue_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["covered"], 0)
        self.assertIsNone(state["last_reply"])
        self.assertEqual(self._assistant_rows(), [])
        self.assertIsNone(self._actual_reply_timestamp())

    async def _assert_native_sender_failure(self, fail_at):
        started = self.clock[0]
        message_id = "sender-failure-{}".format(fail_at)
        event = self.event("鲸鲸，保留这个协议故障输入", message_id, direct=True)
        await self.prepare_reply(event)
        token = event.get_extra("whale_reply_token")
        full_text = ("第一段合成输出。后面仍需保留的重要消息，协议故障时不能把这些内容当作完整回答。"
                     "最后这一句也不能提前记忆。")
        event.set_result(MessageEventResult().message(full_text))
        attempted, delivered = [], []

        async def fail_sender(message, *args, **kwargs):
            attempted.append(message.get_plain_text())
            if len(attempted) == fail_at:
                raise OSError("simulated local protocol sender failure")
            delivered.append(message.get_plain_text())

        # The real RespondStage catches this OSError; the test must complete
        # normally without falsely treating its late after-message hook as sent.
        await self.native_segmented_delivery(event, fail_sender)
        # Adversarial repeated late hook: do NOT fake success with the positive
        # complete_fixture_reply helper when the protocol send actually failed.
        await self.plugin.remember_complete_reply(event)
        self.assertEqual(len(attempted), fail_at)
        self.assertEqual(len(delivered), fail_at - 1)
        self.assertTrue(event.get_extra("whale_send_failed"))
        self.assertTrue(event.get_extra("whale_reply_blocked"))
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.get_extra("whale_send_count", 0), fail_at - 1)
        self.assertFalse(self.plugin.replies.is_valid(SCOPE_A, token))
        self._assert_unacknowledged([message_id])
        self.assertEqual(self._queue_state()["failures"], 1)
        self.assertEqual(self._queue_state()["retry_at"], started + 10.0)
        self.assertNotIn(full_text, json.dumps(self.rows(), ensure_ascii=False))

        # Retry the same durable input through a fresh real native event, without
        # offering a new message or waiting for the 180-second lease.
        self.clock[0] = started + 9.999
        self.assertEqual(await self.plugin.replies.pump(), 0)
        self.assertTrue(self.context.event_queue.empty())
        self.clock[0] = started + 10.0
        self.assertEqual(await self.plugin.replies.pump(), 1)
        retry = self.context.event_queue.get_nowait()
        await self.prepare_reply(retry)
        retry_token = retry.get_extra("whale_reply_token")
        self.assertNotEqual(retry_token, token)
        retry_text = "重试后的完整合成回答。"
        retry.set_result(MessageEventResult().message(retry_text))
        sent = []

        async def collect_retry(message, *args, **kwargs):
            sent.append(message.get_plain_text())

        await self.native_segmented_delivery(retry, collect_retry)
        self.assertEqual("".join(sent), retry_text)
        self.assertEqual(self._pending_ids(), [])
        self.assertEqual(self._queue_state()["covered"], 1)
        self.assertEqual(self._queue_state()["last_reply"], started + 10.0)
        replies = self._assistant_rows()
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["message_id"], self.reply_id(retry))
        self.assertEqual(replies[0]["text"], retry_text)
        self.assertEqual(self._actual_reply_timestamp(), started + 10.0)
        self.assertEqual(self.context.model_calls, [])

    async def test_native_first_send_oserror_is_not_acknowledged_and_can_retry(self):
        await self._assert_native_sender_failure(fail_at=1)

    async def test_native_partial_send_oserror_does_not_store_full_reply_or_drop_pending(self):
        await self._assert_native_sender_failure(fail_at=2)

    async def test_night_guard_save_oserror_still_clears_stops_and_never_sends(self):
        self.clock[0] = runtime.instant(23, 59, 59)
        event = self.event("午夜前保留安全输入", "night-save-failure", direct=True)
        await self.prepare_reply(event)
        token = event.get_extra("whale_reply_token")
        text = "不得越过午夜发送的合成回答。"
        await self.plugin.remember_reply(event, LLMResponse(role="assistant", completion_text=text))
        event.set_result(MessageEventResult().message(text))
        before_disk = self.plugin.replies.path.read_text(encoding="utf-8")
        self.clock[0] = runtime.instant(0, day=14)
        with patch.object(self.plugin.replies, "_save", side_effect=OSError("night queue save denied")):
            await self.decorating_hooks(event)
            self.assertTrue(event.is_stopped())
            self.assertTrue(event.get_extra("whale_reply_blocked"))
            result = event.get_result()
            self.assertTrue(result is None or not result.chain)
            await RespondStage().process(event)
            # Even a direct send attempt after a failed native guard stays closed.
            await event.send(MessageEventResult().message(text))
            self.assertEqual(event.send_attempts, 0)
            self.assertEqual(event.get_extra("whale_send_count", 0), 0)
            self.assertEqual(self.plugin.replies.path.read_text(encoding="utf-8"), before_disk)
        self.assertTrue(self.plugin.replies.is_valid(SCOPE_A, token),
                        "storage failure must preserve the unsent token and its batch")
        self._assert_unacknowledged(["night-save-failure"])

    async def test_bare_real_at_without_plain_is_queued_and_has_generation_input(self):
        event = self.event("", "bare-real-at", direct=True, components=[At(qq=BOT)])
        self.assertIsInstance(event, FakeEvent)
        self.assertEqual(event.message_obj.message_str, "")
        self.assertTrue(any(isinstance(component, At) for component in event.get_messages()))
        self.assertFalse(any(isinstance(component, Plain) for component in event.get_messages()))
        request = await self.prepare_reply(event)
        self.assertIsInstance(request, ProviderRequest)
        self.assertTrue(event.get_extra("whale_empty_mention"))
        self.assertTrue(event.message_str.strip())
        self.assertIn("@", event.message_str)
        self.assertIn(event.message_str, request.prompt)
        self.assertEqual(self._pending_ids(), ["bare-real-at"])
        packet = self._queue_state()["pending"][0]
        self.assertEqual(packet["text"], event.message_str)
        self.assertEqual(packet["sender_id"], event.get_sender_id())
        self.assertFalse(any(isinstance(component, Plain) for component in event.get_messages()),
                         "the fixture must remain a real bare At, not a fabricated Plain input")
        event.set_result(MessageEventResult().message("我在呢，听见啦。"))
        sent = []

        async def collect(message, *args, **kwargs):
            sent.append(message.get_plain_text())

        await self.native_segmented_delivery(event, collect)
        self.assertEqual(sent, ["我在呢，听见啦。"])
        self.assertEqual(self._pending_ids(), [])
        self.assertEqual(len(self._assistant_rows()), 1)
        self.assertEqual(self.context.model_calls, [])

    async def test_proactive_disabled_alias_and_real_at_wake_but_ordinary_is_not_queued(self):
        self.config["proactive_enabled"] = False
        ordinary = self.event("普通输入只能观察，不参与主动排队", "disabled-ordinary")
        await self.plugin.observe(ordinary)
        self.assertFalse(ordinary.is_at_or_wake_command)
        self.assertFalse(ordinary.get_extra("whale_reply_token"))
        self.assertEqual(self._pending_ids(), [])
        self.assertEqual(self.plugin.replies._jobs, {})
        direct_events = [
            self.event("鲸鲸", "disabled-alias"),
            self.event("仍需回应真实称呼", "disabled-at", direct=True,
                       components=[At(qq=BOT), Plain("仍需回应真实称呼")]),
        ]
        tokens, sent = [], []

        async def collect(message, *args, **kwargs):
            sent.append(message.get_plain_text())

        for index, event in enumerate(direct_events):
            self.clock[0] += 1.0  # Both requests are still well inside a ten-second gap.
            request = await self.prepare_reply(event)
            self.assertIn(event.message_str, request.prompt)
            self.assertFalse(event.get_extra("whale_proactive"))
            token = event.get_extra("whale_reply_token")
            tokens.append(token)
            self.assertEqual(self._pending_ids(), [str(event.message_obj.message_id)])
            event.set_result(MessageEventResult().message("第{}次称呼已收到。".format(index + 1)))
            await self.native_segmented_delivery(event, collect)
            self.assertEqual(self._pending_ids(), [])
        self.assertEqual(len(set(tokens)), 2)
        self.assertEqual(sent, ["第1次称呼已收到。", "第2次称呼已收到。"])
        self.assertEqual(len(self._assistant_rows()), 2)
        self.assertEqual(self.context.model_calls, [])

    async def test_self_echo_with_alias_real_at_and_direct_flag_is_stopped_before_llm(self):
        ordinary = self.event("已有朋友输入不能被自回声抢占", "before-self-echo")
        await self.prepare_reply(ordinary)
        token = ordinary.get_extra("whale_reply_token")
        before_rows = self.rows()
        before_queue = self.plugin.replies.path.read_text(encoding="utf-8")
        echo = self.event("鲸鲸，机器人自己的合成回声", "direct-self-echo", sender=BOT,
                          direct=True, components=[At(qq=BOT), Plain("鲸鲸，机器人自己的合成回声")])
        await self.plugin.observe(echo)
        self.assertTrue(echo.is_stopped())
        self.assertFalse(echo.get_extra("whale_reply_token"))
        self.assertEqual(self.rows(), before_rows)
        self.assertEqual(self.plugin.replies.path.read_text(encoding="utf-8"), before_queue)
        self.assertTrue(self.plugin.replies.is_valid(SCOPE_A, token))
        self.assertEqual(self._pending_ids(), ["before-self-echo"])
        self.assertEqual(self.context.model_calls, [])
        await RespondStage().process(echo)
        self.assertEqual(echo.send_attempts, 0)

    async def test_error_llm_response_never_acknowledges_or_records_error_body(self):
        event = self.event("鲸鲸，错误输出不得当作回答", "llm-error", direct=True)
        await self.prepare_reply(event)
        token = event.get_extra("whale_reply_token")
        error_text = "DO_NOT_STORE_PROVIDER_ERROR_BODY"
        event.set_result(MessageEventResult().message(error_text))
        response = LLMResponse(role="err", completion_text=error_text,
                               reasoning_content="DO_NOT_STORE_ERROR_REASONING")
        await self.plugin.remember_reply(event, response)
        self.assertTrue(event.is_stopped())
        self.assertTrue(event.get_extra("whale_reply_blocked"))
        self.assertFalse(event.get_extra("whale_model_response_seen", False))
        self.assertFalse(self.plugin.replies.is_valid(SCOPE_A, token))
        # Provider errors can attempt direct sends before decoration. The bound
        # real send guard must prevent even this adversarial path from escaping.
        await event.send(MessageEventResult().message(error_text))
        await RespondStage().process(event)
        await self.plugin.remember_complete_reply(event)  # No fabricated send success.
        self.assertEqual(event.send_attempts, 0)
        self.assertEqual(event.get_extra("whale_send_count", 0), 0)
        self._assert_unacknowledged(["llm-error"])
        self.assertNotIn("DO_NOT_STORE", json.dumps(self.rows(), ensure_ascii=False))
        self.assertEqual(self.context.model_calls, [])

    async def _assert_native_delivery_across_lease(self, text, segmented):
        started = self.clock[0]
        event = self.event("普通长耗时输入仍只回复一批", "lease-native-" + str(segmented))
        await self.prepare_reply(event)
        token = event.get_extra("whale_reply_token")
        self.assertTrue(event.get_extra("whale_proactive"))
        event.set_result(MessageEventResult().message(text))
        self.clock[0] = started + 179.0
        sent, pump_results = [], []
        real_keepalive = self.plugin.replies.keepalive

        async def collect(message, *args, **kwargs):
            self.assertTrue(self.plugin.replies.is_valid(SCOPE_A, token))
            sent.append(message.get_plain_text())
            if len(sent) == 1:
                self.clock[0] = started + 181.0
                pump_results.append(await self.plugin.replies.pump())
                self.assertTrue(self.plugin.replies.is_valid(SCOPE_A, token))
                self.assertTrue(self.context.event_queue.empty())

        with patch.object(self.plugin.replies, "keepalive", wraps=real_keepalive) as keepalive:
            await self.native_segmented_delivery(event, collect)
        self.assertGreaterEqual(keepalive.call_count, 1 + 2 * len(sent))
        self.assertEqual(pump_results, [0])
        self.assertEqual("".join(sent), text)
        if segmented:
            self.assertGreater(len(sent), 3)
        else:
            self.assertEqual(sent, [text])
        self.assertTrue(all(0 < len(piece) <= 20 for piece in sent))
        self.assertFalse(event.get_extra("whale_reply_blocked", False))
        self.assertEqual(event.get_extra("whale_send_count"), len(sent))
        self.assertEqual(self._pending_ids(), [])
        self.assertEqual(self._queue_state()["failures"], 0)
        self.assertEqual(self._queue_state()["last_reply"], started + 181.0)
        replies = self._assistant_rows()
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["message_id"], self.reply_id(event))
        self.assertEqual(replies[0]["text"], text)
        self.assertEqual(self._actual_reply_timestamp(), started + 181.0)
        self.clock[0] += 10.0
        self.assertEqual(await self.plugin.replies.pump(), 0)
        self.assertTrue(self.context.event_queue.empty())

    async def test_native_short_send_crossing_179_to_181_renews_lease_without_duplicate(self):
        await self._assert_native_delivery_across_lease("长耗时也只回应一次。", segmented=False)

    async def test_native_segmented_send_crossing_179_to_181_renews_lease_without_duplicate(self):
        text = ("这是一段跨越旧租约边界的完整合成回复。每一个小段都必须自然送达，不能因原先一百八十秒期限重复创建任务。"
                "后续必要说明仍要完整保留。最后这句也属于同一个回答。")
        await self._assert_native_delivery_across_lease(text, segmented=True)

    async def test_force_task_cancels_real_native_ordinary_sender_await_and_releases_it(self):
        old = self.event("ORDINARY_NATIVE_AWAIT_INPUT", "native-await-old")
        force = self.event("鲸鲸，FORCE_NATIVE_INPUT", "native-await-force")
        sender_entered, sender_released, pipeline_released = (asyncio.Event() for _ in range(3))
        never_release = asyncio.Event()
        old_sent, force_sent, force_requests = [], [], []

        async def waiting_native_sender(message, *args, **kwargs):
            sender_entered.set()
            try:
                await never_release.wait()
                old_sent.append(message.get_plain_text())  # Must never run after preemption.
            finally:
                sender_released.set()

        async def old_native_pipeline():
            try:
                await self.prepare_reply(old)  # _bind_reply owns THIS task, not the test task.
                old.set_result(MessageEventResult().message("旧模型合成正文不得在被抢占后发出。"))
                await self.native_segmented_delivery(old, waiting_native_sender)
            finally:
                pipeline_released.set()

        async def collect_force(message, *args, **kwargs):
            force_sent.append(message.get_plain_text())

        async def forced_native_pipeline():
            request = await self.prepare_reply(force)
            force_requests.append(request.prompt)
            # Old native ExitStack registry bindings must unwind before the new
            # native helper installs its own bindings for the same plugin instance.
            await asyncio.wait_for(pipeline_released.wait(), timeout=5.0)
            force.set_result(MessageEventResult().message("新称呼现在回应。"))
            await self.native_segmented_delivery(force, collect_force)

        old_task = self._spawn(old_native_pipeline(), "safety-old-real-native-pipeline")
        await asyncio.wait_for(sender_entered.wait(), timeout=5.0)
        old_token = old.get_extra("whale_reply_token")
        self.assertTrue(old.get_extra("whale_proactive"))
        self.assertIs(self.plugin._reply_tasks.get(old_token), old_task)
        self.assertEqual(self.plugin.replies._dispatch_tasks, {},
                         "this is cancellation of the actual native task, not dispatch cancellation")
        force_task = self._spawn(forced_native_pipeline(), "safety-force-real-native-pipeline")
        await asyncio.wait_for(force_task, timeout=5.0)
        old_outcome = (await asyncio.gather(old_task, return_exceptions=True))[0]
        await self._settle_callbacks()
        self.assertIsInstance(old_outcome, asyncio.CancelledError)
        self.assertTrue(old_task.cancelled())
        self.assertTrue(sender_released.is_set())
        self.assertTrue(pipeline_released.is_set())
        self.assertTrue(old.get_extra("whale_reply_blocked"))
        self.assertEqual(old_sent, [])
        self.assertEqual(old.get_extra("whale_send_count", 0), 0)
        self.assertEqual(force_sent, ["新称呼现在回应。"])
        self.assertEqual(len(force_requests), 1)
        self.assertIn("ORDINARY_NATIVE_AWAIT_INPUT", force_requests[0])
        self.assertIn("FORCE_NATIVE_INPUT", force_requests[0])
        self.assertFalse(self.plugin.replies.is_valid(SCOPE_A, old_token))
        self.assertFalse(self.plugin.replies.finish(SCOPE_A, old_token))
        self.assertEqual(self.plugin._reply_tasks, {})
        self.assertEqual(self._pending_ids(), [])
        self.assertEqual(self._queue_state()["covered"], 2)
        replies = self._assistant_rows()
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["message_id"], self.reply_id(force))
        self.assertEqual(replies[0]["text"], "新称呼现在回应。")
        self.assertFalse(any(row["message_id"] == self.reply_id(old) for row in replies))
        self.assertEqual(self.context.model_calls, [])

    async def _assert_pipeline_done_without_send(self, reason):
        started = self.clock[0]
        message_id = "native-done-" + reason
        event = self.event("未完成的原生输入必须及时回收", message_id)

        async def native_pipeline():
            await self.prepare_reply(event)
            token = event.get_extra("whale_reply_token")
            if reason == "empty":
                await self.plugin.remember_reply(event, LLMResponse(role="assistant", completion_text=""))
            else:
                event.should_call_llm(True)  # Another native/plugin stage suppresses the default model.
            event.clear_result()
            await RespondStage().process(event)  # Real empty-output path has no successful after hook.
            self.assertTrue(self.plugin.replies.is_valid(SCOPE_A, token),
                            "prove cleanup has not happened before the native task actually finishes")
            return token

        task = self._spawn(native_pipeline(), "safety-native-done-" + reason)
        token = await asyncio.wait_for(task, timeout=5.0)
        await self._settle_callbacks()
        self.assertFalse(task.cancelled())
        self.assertEqual(self.clock[0], started, "done cleanup must not wait for any clock advance")
        self.assertFalse(self.plugin.replies.is_valid(SCOPE_A, token))
        self.assertNotIn(token, self.plugin._reply_tasks)
        self._assert_unacknowledged([message_id])
        self.assertEqual(self._queue_state()["failures"], 1)
        self.assertEqual(self._queue_state()["retry_at"], started + 10.0)
        self.assertEqual(event.send_attempts, 0)
        self.assertEqual(event.get_extra("whale_send_count", 0), 0)
        self.assertEqual(self.context.model_calls, [])
        self.clock[0] = started + 9.999
        self.assertEqual(await self.plugin.replies.pump(), 0)
        self.clock[0] = started + 10.0
        self.assertEqual(await self.plugin.replies.pump(), 1)
        retry = self.context.event_queue.get_nowait()
        self.assertEqual(str(retry.message_obj.message_id), message_id)
        self.assertNotEqual(retry.get_extra("whale_deferred"), token)

    async def test_native_empty_output_done_callback_retries_now_instead_of_waiting_lease(self):
        await self._assert_pipeline_done_without_send("empty")

    async def test_native_suppression_done_callback_retries_now_instead_of_waiting_lease(self):
        await self._assert_pipeline_done_without_send("suppressed")

    async def _assert_live_config_revocation(self, kind):
        event = self.event("普通输入需要逐段检查当前配置", "live-revoke-" + kind)
        await self.prepare_reply(event)
        token = event.get_extra("whale_reply_token")
        self.assertTrue(event.get_extra("whale_proactive"))
        text = "第一段仍在允许发送的配置下。后续段落必须重新检查是否已关闭功能、撤销群或停止主动回复。最后这一句不能越过新的配置。"
        event.set_result(MessageEventResult().message(text))
        sent = []

        async def collect_then_revoke(message, *args, **kwargs):
            sent.append(message.get_plain_text())
            if len(sent) == 1:
                if kind == "enabled":
                    self.config["enabled"] = False
                elif kind == "group":
                    self.config["group_ids"] = [GROUP_B]
                else:
                    self.config["proactive_enabled"] = False

        await self.native_segmented_delivery(event, collect_then_revoke)
        self.assertEqual(len(sent), 1)
        self.assertTrue(event.is_stopped())
        self.assertTrue(event.get_extra("whale_reply_blocked"))
        self.assertEqual(event.get_extra("whale_send_count", 0), 1)
        self.assertFalse(self.plugin.replies.is_valid(SCOPE_A, token))
        self._assert_unacknowledged(["live-revoke-" + kind])

    async def test_live_enabled_revocation_blocks_remaining_native_segments(self):
        await self._assert_live_config_revocation("enabled")

    async def test_live_group_revocation_blocks_remaining_native_segments(self):
        await self._assert_live_config_revocation("group")

    async def test_live_proactive_disable_blocks_remaining_ordinary_native_segments(self):
        await self._assert_live_config_revocation("proactive")

    async def test_live_proactive_disable_does_not_block_direct_native_segments(self):
        event = self.event("鲸鲸，明确称呼仍可完成回复", "live-direct-proactive-disable")
        await self.prepare_reply(event)
        self.assertFalse(event.get_extra("whale_proactive"))
        text = "这是明确称呼触发的完整合成回答。中途停止普通主动接话，仍不应误拦正在回应朋友称呼的后续小段。最后的信息也要保留。"
        event.set_result(MessageEventResult().message(text))
        sent = []

        async def collect_then_disable_proactive(message, *args, **kwargs):
            sent.append(message.get_plain_text())
            self.config["proactive_enabled"] = False

        await self.native_segmented_delivery(event, collect_then_disable_proactive)
        self.assertGreater(len(sent), 2)
        self.assertTrue(all(0 < len(piece) <= 20 for piece in sent))
        self.assertEqual("".join(sent), text)
        self.assertFalse(event.is_stopped())
        self.assertFalse(event.get_extra("whale_reply_blocked", False))
        self.assertEqual(self._pending_ids(), [])
        replies = self._assistant_rows()
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["text"], text)

    async def test_early_native_provider_error_send_before_model_hook_is_blocked(self):
        event = self.event("鲸鲸，模型错误不能提前发送", "early-provider-error", direct=True)
        await self.prepare_reply(event)
        self.assertFalse(event.get_extra("whale_model_response_seen", False))
        error_text = "DO_NOT_SEND_EARLY_PROVIDER_ERROR"
        event.set_result(MessageEventResult().message(error_text))
        # Native provider failure paths can call send before OnLLMResponse and
        # OnDecoratingResult. The guard installed by _bind_reply must already work.
        await event.send(MessageEventResult().message(error_text))
        self.assertTrue(event.is_stopped())
        self.assertTrue(event.get_extra("whale_reply_blocked"))
        await self.plugin.remember_reply(event, LLMResponse(role="err", completion_text=error_text))
        await self.plugin.remember_complete_reply(event)  # Still no protocol-send success.
        self.assertEqual(event.send_attempts, 0)
        self.assertEqual(event.get_extra("whale_send_count", 0), 0)
        self._assert_unacknowledged(["early-provider-error"])
        self.assertEqual(self._queue_state()["failures"], 1)
        self.assertNotIn(error_text, json.dumps(self.rows(), ensure_ascii=False))

    async def test_native_midnight_timeout_keeps_sender_in_owner_task_and_prevents_api_call(self):
        # Injected wall clock has only about 0.05s left. The timeout uses the REAL
        # event-loop monotonic clock; both deadlines cross in under one second.
        self.clock[0] = runtime.instant(23, 59, 59) + 0.95
        event = self.event("午夜前API等待不能越界", "native-midnight-await", direct=True)
        api_ready, wall_crossed, sender_released = (asyncio.Event() for _ in range(3))
        owner, observed_tasks, api_calls, timers = {}, [], [], []
        loop = asyncio.get_running_loop()

        def cross_midnight():
            self.clock[0] = runtime.instant(0, day=14)
            wall_crossed.set()

        async def delayed_protocol_sender(message, *args, **kwargs):
            observed_tasks.append(asyncio.current_task())
            seconds = self.plugin._reply_seconds_left()
            self.assertGreater(seconds, 0.0)
            self.assertLess(seconds, 0.2)
            owner["real_deadline"] = loop.time() + seconds
            timers.append(loop.call_later(seconds, cross_midnight))
            timers.append(loop.call_later(seconds + 0.04, api_ready.set))
            try:
                await api_ready.wait()
                # This stands immediately before the real protocol API boundary.
                # The awaited operation must have been cancelled before this line.
                api_calls.append(message.get_plain_text())
            finally:
                sender_released.set()

        async def native_pipeline():
            owner["task"] = asyncio.current_task()
            await self.prepare_reply(event)
            event.set_result(MessageEventResult().message("不能越过午夜调用API。"))
            await self.native_segmented_delivery(event, delayed_protocol_sender)

        task = self._spawn(native_pipeline(), "safety-native-real-midnight-timeout")
        try:
            await asyncio.wait_for(task, timeout=5.0)
            await asyncio.wait_for(wall_crossed.wait(), timeout=0.5)
            # Observe the would-have-been API gate after BOTH deadlines, not just
            # before its timer fires. No actual 10-second sleep is used.
            await asyncio.wait_for(api_ready.wait(), timeout=0.5)
            await self._settle_callbacks()
            self.assertIs(owner["task"], task)
            self.assertEqual(observed_tasks, [task],
                             "asyncio.timeout must preserve the real native Task; wait_for would create a child")
            self.assertGreaterEqual(loop.time(), owner["real_deadline"])
            self.assertEqual(self.clock[0], runtime.instant(0, day=14))
            self.assertFalse(self.plugin._awake())
            self.assertTrue(sender_released.is_set())
            self.assertEqual(api_calls, [])
            self.assertTrue(event.is_stopped())
            self.assertTrue(event.get_extra("whale_reply_blocked"))
            self.assertTrue(event.get_extra("whale_send_failed"))
            self.assertEqual(event.get_extra("whale_send_count", 0), 0)
            self._assert_unacknowledged(["native-midnight-await"])
        finally:
            for timer in timers:
                timer.cancel()

    async def test_queue_close_save_failure_drains_real_worker_and_keeps_committed_pending(self):
        queue = self.plugin.replies
        self.assertFalse(queue.running)
        queue.external_reply(SCOPE_A)
        event = self.event("关闭保存失败仍要保留输入", "queue-close-save-failure")
        self.assertIsNone(queue.offer(SCOPE_A, self.plugin._source_packet(event)))
        await queue.start()
        self.assertTrue(queue.running)
        worker = queue._worker
        self.assertIsNotNone(worker)
        self.assertFalse(worker.done())
        before_disk = queue.path.read_text(encoding="utf-8")
        with patch.object(queue, "_save", side_effect=OSError("queue close atomic save failed")) as save:
            await queue.close()
        save.assert_called()
        self.assertFalse(queue.running)
        self.assertTrue(worker.done(), "a failed close save must still cancel AND await the actual worker")
        self.assertTrue(queue._closed)
        self.assertIsNone(queue._worker)
        self.assertIsNone(queue._wake)
        self.assertEqual(queue._dispatch_tasks, {})
        self.assertEqual(queue.path.read_text(encoding="utf-8"), before_disk)
        self.assertEqual(self._pending_ids(), ["queue-close-save-failure"])

    async def test_terminate_save_failure_gathers_native_lease_scheduler_and_worker(self):
        queue = self.plugin.replies
        await queue.start()  # Real worker/running state enables the production lease ticker.
        worker = queue._worker
        native_entered, native_released, scheduler_entered, scheduler_released = (
            asyncio.Event() for _ in range(4))
        park = asyncio.Event()
        event = self.event("热重载不得留下旧原生任务", "terminate-save-failure")

        async def native_owner():
            try:
                await self.prepare_reply(event)
                native_entered.set()
                await park.wait()
            finally:
                native_released.set()

        async def parked_scheduler():
            # Use a real parked Task rather than running memory review/provider work.
            scheduler_entered.set()
            try:
                await park.wait()
            finally:
                scheduler_released.set()

        native_task = self._spawn(native_owner(), "safety-terminate-native-owner")
        scheduler_task = self._spawn(parked_scheduler(), "safety-terminate-real-scheduler-task")
        self.plugin.scheduler_task = scheduler_task
        await asyncio.wait_for(native_entered.wait(), timeout=5.0)
        await asyncio.wait_for(scheduler_entered.wait(), timeout=5.0)
        token = event.get_extra("whale_reply_token")
        self.assertIs(self.plugin._reply_tasks.get(token), native_task)
        lease_tasks = list(self.plugin._lease_tasks.values())
        self.assertEqual(len(lease_tasks), 1)
        self.assertTrue(all(not task.done() for task in lease_tasks))
        before_disk = queue.path.read_text(encoding="utf-8")
        with patch.object(queue, "_save", side_effect=OSError("terminate atomic save failed")) as save:
            await self.plugin.terminate()
        save.assert_called()
        await self._settle_callbacks()
        all_tasks = [native_task, scheduler_task, worker] + lease_tasks
        self.assertTrue(all(task.done() for task in all_tasks),
                        "terminate must gather native, heartbeat, scheduler, and queue worker despite storage failure")
        self.assertTrue(native_released.is_set())
        self.assertTrue(scheduler_released.is_set())
        self.assertEqual(self.plugin._reply_tasks, {})
        self.assertEqual(self.plugin._lease_tasks, {})
        self.assertIsNone(self.plugin.scheduler_task)
        self.assertFalse(queue.running)
        self.assertIsNone(queue._worker)
        self.assertEqual(queue.path.read_text(encoding="utf-8"), before_disk)
        self._assert_unacknowledged(["terminate-save-failure"])

    async def test_running_heartbeat_keeps_native_task_live_after_three_seventy_second_steps(self):
        queue = self.plugin.replies
        self.assertFalse(queue.running)
        heartbeat, native_entered, native_released, park = (asyncio.Event() for _ in range(4))
        heartbeat_times = []
        event = self.event("原生生成任务超过旧租约仍要保活", "runtime-heartbeat-210s")
        real_keepalive = queue.keepalive

        def observe_heartbeat(scope, token):
            result = real_keepalive(scope, token)
            if result:
                heartbeat_times.append(self.clock[0])
                heartbeat.set()
            return result

        async def native_owner():
            try:
                await self.prepare_reply(event)
                native_entered.set()
                await park.wait()  # Simulates the real task awaiting generation, not a dispatch Task.
            finally:
                native_released.set()

        with patch.object(type(self.plugin), "HEARTBEAT_SECONDS", 0.001), \
                patch.object(queue, "keepalive", side_effect=observe_heartbeat):
            await queue.start()
            worker = queue._worker
            task = self._spawn(native_owner(), "safety-runtime-heartbeat-native-owner")
            await asyncio.wait_for(native_entered.wait(), timeout=5.0)
            token = event.get_extra("whale_reply_token")
            self.assertIs(self.plugin._reply_tasks.get(token), task)
            lease_tasks = list(self.plugin._lease_tasks.values())
            self.assertEqual(len(lease_tasks), 1)
            started = self.clock[0]
            for step in range(1, 4):
                heartbeat.clear()
                self.clock[0] = started + 70.0 * step
                # Wait for an actual production heartbeat at THIS injected time.
                # Its interval is 0.001s, never a real 30/70/180-second sleep.
                await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
                self.assertIn(self.clock[0], heartbeat_times)
                self.assertTrue(queue.is_valid(SCOPE_A, token))
                self.assertEqual(set(queue._jobs), {token})
                self.assertFalse(task.done())
                self.assertTrue(all(not lease.done() for lease in lease_tasks))
                self.assertEqual(await queue.pump(), 0)
                self.assertTrue(self.context.event_queue.empty())
            self.assertEqual(self.clock[0] - started, 210.0)
            self._assert_unacknowledged(["runtime-heartbeat-210s"])
            self.assertEqual(self._queue_state()["failures"], 0)
            await self.plugin.terminate()
            await self._settle_callbacks()
            self.assertTrue(all(item.done() for item in [task, worker] + lease_tasks))
            self.assertTrue(native_released.is_set())
            self.assertEqual(self.plugin._reply_tasks, {})
            self.assertEqual(self.plugin._lease_tasks, {})
            self.assertFalse(queue.running)
            self.assertIsNone(queue._worker)
            self.assertEqual(self._pending_ids(), ["runtime-heartbeat-210s"])

    async def test_offer_oserror_observe_clears_and_stops_before_native_error_output(self):
        event = self.event("鲸鲸，保存异常也不能回垃圾内容", "incoming-offer-oserror", direct=True)
        garbage = MessageEventResult().message("DO_NOT_SEND_NATIVE_PLUGIN_ERROR_GARBAGE")
        event.set_result(garbage)
        with patch.object(self.plugin.replies, "offer", side_effect=OSError("incoming queue admission failed")) as offer:
            await self.plugin.observe(event)  # The observer must swallow/fence its own storage error.
        offer.assert_called_once()
        self.assertTrue(event.is_stopped())
        self.assertFalse(event.get_extra("whale_reply_token"))
        result = event.get_result()
        self.assertTrue(result is None or not result.chain)
        await RespondStage().process(event)
        self.assertEqual(event.send_attempts, 0)
        self.assertEqual(self._pending_ids(), [])
        self.assertEqual(self.plugin.replies._jobs, {})
        self.assertEqual(self.plugin._reply_tasks, {})
        self.assertEqual(self._assistant_rows(), [])
        self.assertIsNone(self._actual_reply_timestamp())
        self.assertEqual(self.context.model_calls, [])
        self.assertNotIn("DO_NOT_SEND", json.dumps(self.rows(), ensure_ascii=False))

    async def test_native_stripped_command_filters_are_observed_but_not_queued_or_stopped(self):
        self.config['ignore_commands'] = False  # Explicit legacy opt-out; new policy is covered separately.
        # Real waking already stripped '/help' or '/group'. A leading-slash
        # heuristic cannot distinguish these from ordinary text at this point.
        cases = (("help", CommandFilter("help")), ("group", CommandGroupFilter("group")))
        for text, native_filter in cases:
            with self.subTest(native_filter=type(native_filter).__name__):
                event = self.event(text, "stripped-native-" + text, direct=True)
                event.set_extra("activated_handlers", [SimpleNamespace(
                    handler_module_path="fixture.builtin",
                    event_filters=[native_filter],
                )])
                native_reply = MessageEventResult().message("native command reply: " + text)
                event.set_result(native_reply)
                with patch.object(self.plugin.replies, "offer", wraps=self.plugin.replies.offer) as offer:
                    await self.plugin.observe(event)
                offer.assert_not_called()
                self.assertTrue(event.is_at_or_wake_command)
                self.assertFalse(event.is_stopped(), "registered commands must return to their real native handler")
                self.assertFalse(event.get_extra("whale_reply_token"))
                self.assertIs(event.get_result(), native_reply)
                self.assertEqual(event.get_result().get_plain_text(), "native command reply: " + text)
                self.assertEqual(self._pending_ids(), [])
                self.assertEqual(self.plugin.replies._jobs, {})
        rows = self.rows()
        self.assertEqual([row["role"] for row in rows], ["user", "user"])
        self.assertEqual([row["text"] for row in rows], ["help", "group"])
        self.assertEqual(self.plugin._reply_tasks, {})
        self.assertIsNone(self._actual_reply_timestamp(), "observing a command is not a successful command reply")
        self.assertEqual(self.context.model_calls, [])

    async def test_actual_provider_empty_wake_prefix_admits_ordinary_queued_text(self):
        text = "朋友们聊起今天的天气"
        event = self.event(text, "actual-empty-provider-prefix")
        self.assertFalse(event.is_at_or_wake_command)
        self.assertFalse(any(isinstance(component, At) for component in event.get_messages()))
        self.assertTrue(_matches_provider_wake_prefix(event, ""))
        self.assertFalse(_matches_provider_wake_prefix(event, "/"))
        request = await self.prepare_reply(event)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertTrue(event.get_extra("whale_proactive"))
        self.assertTrue(event.get_extra("whale_reply_token"))
        self.assertIn(text, request.prompt)
        # The real agent does NOT exempt is_at_or_wake_command from an extra
        # provider prefix. The installer must use '' while bot commands keep '/'.
        self.assertTrue(_matches_provider_wake_prefix(event, ""))
        self.assertFalse(_matches_provider_wake_prefix(event, "/"))
        self.assertEqual(event.message_str, text)
        self.assertEqual(self._pending_ids(), ["actual-empty-provider-prefix"])
        self.assertEqual(self._assistant_rows(), [])
        self.assertIsNone(self._actual_reply_timestamp())
        self.assertEqual(self.context.model_calls, [])


if __name__ == "__main__":
    unittest.main(defaultTest="QueueSafetyRehearsal", verbosity=2)
