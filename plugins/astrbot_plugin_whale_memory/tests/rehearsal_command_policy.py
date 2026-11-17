"""Offline native commandguard rehearsal, against the actual main.py hooks.

Run only in the parent's disposable network=none, fake-config AstrBot container:
    WHALE_REHEARSAL=1 python data/plugins/astrbot_plugin_whale_memory/tests/rehearsal_command_policy.py

Borrow the temporary storage/network-denial fixtures from rehearsal_runtime. Real
ProcessStage, StarRequestSubStage, native filter classes, priority registry and
RespondStage are used. The agent substage is a local fail-on-entry sentinel, not
an actual provider. No commandguard is injected by the test: missing main wiring
must fail. Additional real WakingCheckStage tests call plugin.initialize() to
install the parent's ingress wrapper before native missing-argument error sends.
"""
from __future__ import annotations

from contextlib import ExitStack
import copy
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


sys.dont_write_bytecode = True
if os.environ.get("WHALE_REHEARSAL") != "1":
    raise RuntimeError("Run only in the disposable network-disabled rehearsal container with WHALE_REHEARSAL=1.")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rehearsal_runtime as runtime

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import StarHandlerMetadata, StarHandlerRegistry


class CommandPolicyRehearsal(unittest.IsolatedAsyncioTestCase):
    # Borrow helpers, not the original TestCase's legacy command/gap tests.
    _patch = runtime.RuntimeRehearsal._patch
    _deny_network = runtime.RuntimeRehearsal._deny_network
    event = runtime.RuntimeRehearsal.event
    rows = runtime.RuntimeRehearsal.rows

    async def asyncSetUp(self):
        await runtime.RuntimeRehearsal.asyncSetUp(self)
        self.config.update(ignore_commands=True, proactive_gap_seconds=25,
                           web_lookup_enabled=False, vision_enabled=True,
                           delivery_guard_enabled=True, reply_polish_enabled=False)
        self.native_commands = []
        self.agent_entries = []
        self.native_priorities = []

    async def asyncTearDown(self):
        await runtime.RuntimeRehearsal.asyncTearDown(self)

    def _queue_bytes(self):
        path = self.plugin.replies.path
        self.assertTrue(path.is_relative_to(self.temporary.name))
        return path.read_bytes() if path.exists() else None

    async def _native_process(self, event, native_filter=None, *, allow_agent=False):
        """Use native dispatch order on activated, post-waking handlers."""
        configuration = copy.deepcopy(DEFAULT_CONFIG)
        configuration["provider_settings"]["enable"] = True
        context = SimpleNamespace(astrbot_config=configuration,
                                  plugin_manager=SimpleNamespace(context=self.context))
        # The existing FakeEvent lacks this read-only framework accessor.
        event.get_message_str = lambda: event.message_str
        registry = StarHandlerRegistry()
        for original in runtime.star_handlers_registry:
            if (original.handler_module_path == runtime.plugin_module.__name__
                    and original.event_type == runtime.EventType.AdapterMessageEvent
                    and all(item.filter(event, configuration) for item in original.event_filters)):
                handler = copy.copy(original)
                handler.handler = getattr(self.plugin, handler.handler_name)
                registry.append(handler)

        async def native_command(incoming):
            self.native_commands.append(incoming.message_str)
            incoming.should_call_llm(True)
            yield incoming.plain_result("DO_NOT_SEND_COMMAND_LONG_RESULT" * 20)

        fixture_module = "fixture.offline_native_command"
        if native_filter is not None:
            registry.append(StarHandlerMetadata(
                event_type=runtime.EventType.AdapterMessageEvent,
                handler_full_name=fixture_module + "_command",
                handler_name="command", handler_module_path=fixture_module,
                handler=native_command, event_filters=[native_filter],
                extras_configs={"priority": 0},
            ))
        handlers = list(registry)
        self.assertTrue(handlers, "The actual main.py incoming hooks are not registered.")
        self.native_priorities = [item.extras_configs.get("priority", 0) for item in handlers]
        self.assertEqual(self.native_priorities, sorted(self.native_priorities, reverse=True))
        event.set_extra("activated_handlers", handlers)
        event.set_extra("handlers_parsed_params", {})

        async def agent_sentinel(incoming):
            self.agent_entries.append(incoming.message_str)
            if not allow_agent:
                raise AssertionError("A command entered the native model path.")
            yield

        stage = ProcessStage()
        stage.ctx = context
        stage.star_request_sub_stage = StarRequestSubStage()
        await stage.star_request_sub_stage.initialize(context)
        stage.agent_sub_stage = SimpleNamespace(process=agent_sentinel)
        with ExitStack() as bindings:
            bindings.enter_context(patch.dict(runtime.star_map, {
                fixture_module: SimpleNamespace(name="offline-command-fixture",
                                                display_name="offline-command-fixture")}))
            bindings.enter_context(patch.object(runtime.star_map[runtime.plugin_module.__name__],
                                               "activated", True))
            bindings.enter_context(patch("astrbot.core.pipeline.process_stage.stage.FILTERS", []))
            bindings.enter_context(patch("astrbot.core.pipeline.process_stage.stage.USER_SESSIONS", {}))
            async for _ in stage.process(event):
                pass

    async def _assert_blocked(self, event, native_filter=None):
        before_rows = self.rows()
        before_queue = self._queue_bytes()
        before_tasks = dict(self.plugin._reply_tasks)
        before_jobs = dict(self.plugin.replies._jobs)
        before_saves = self.config.saves
        event.set_result(runtime.MessageEventResult().message("DO_NOT_SEND_PREEXISTING_HELP" * 20))
        with patch.object(self.plugin.store, "add_message", wraps=self.plugin.store.add_message) as remember, \
                patch.object(self.plugin, "_capture_images", new_callable=AsyncMock) as images, \
                patch.object(self.plugin.replies, "offer", wraps=self.plugin.replies.offer) as offer:
            await self._native_process(event, native_filter)
        remember.assert_not_called()
        images.assert_not_called()
        offer.assert_not_called()
        self.assertTrue(event.is_stopped())
        result = event.get_result()
        self.assertTrue(result is None or not result.chain, "A stopped command retained sendable output.")
        await RespondStage().process(event)
        self.assertEqual(event.send_attempts, 0)
        self.assertFalse(event.get_extra("whale_reply_token"))
        self.assertEqual(self.rows(), before_rows)
        self.assertEqual(self._queue_bytes(), before_queue)
        self.assertEqual(self.plugin._reply_tasks, before_tasks)
        self.assertEqual(self.plugin.replies._jobs, before_jobs)
        self.assertEqual(self.config.saves, before_saves)
        self.assertTrue(self.context.event_queue.empty())
        self.assertEqual(self.context.model_calls, [])
        self.assertEqual(self.native_commands, [])
        self.assertEqual(self.agent_entries, [])

    async def _waking_stage(self):
        configuration = copy.deepcopy(DEFAULT_CONFIG)
        configuration["wake_prefix"] = ["/"]
        configuration["admins_id"] = [runtime.ADMIN]
        configuration["disable_builtin_commands"] = False
        configuration["platform_settings"]["no_permission_reply"] = False
        context = SimpleNamespace(astrbot_config=configuration, db_helper=None,
                                  astrbot_config_id="fixture-waking-command")
        stage = WakingCheckStage()
        await stage.initialize(context)
        return stage

    def _waking_event(self, text, message_id):
        event = self.event(text, message_id)
        event.is_wake = False
        event.role = "member"
        event.get_message_str = lambda: event.message_str
        event.get_button_interaction = lambda: None
        event.is_button_interaction = lambda: False
        event.is_private_chat = lambda: not bool(event.get_group_id())
        return event

    async def _assert_waking_blocked(self, stage, event):
        before_rows, before_queue = self.rows(), self._queue_bytes()
        event.set_result(runtime.MessageEventResult().message("DO_NOT_SEND_WAKING_RESULT" * 20))
        with patch.object(self.plugin.store, "add_message", wraps=self.plugin.store.add_message) as remember, \
                patch.object(self.plugin, "_capture_images", new_callable=AsyncMock) as images, \
                patch.object(self.plugin.replies, "offer", wraps=self.plugin.replies.offer) as offer:
            # Actual class method, with only the wrapper installed by initialize.
            await stage.process(event)
        remember.assert_not_called()
        images.assert_not_called()
        offer.assert_not_called()
        self.assertTrue(event.is_stopped())
        result = event.get_result()
        self.assertTrue(result is None or not result.chain)
        await RespondStage().process(event)
        self.assertEqual(event.send_attempts, 0)
        self.assertEqual(self.rows(), before_rows)
        self.assertEqual(self._queue_bytes(), before_queue)
        self.assertEqual(self.context.model_calls, [])
        self.assertTrue(self.context.event_queue.empty())
        self.assertEqual(self.config.saves, 0)

    async def test_real_waking_missing_group_argument_is_blocked_by_actual_initialize(self):
        stage = await self._waking_stage()
        original_process = WakingCheckStage.process
        fixture_module = "fixture.offline_missing_group_argument"
        registry = StarHandlerRegistry()

        async def never_command(incoming):
            raise AssertionError("The missing-argument fixture handler must never execute.")

        registry.append(StarHandlerMetadata(
            event_type=runtime.EventType.AdapterMessageEvent,
            handler_full_name=fixture_module + "_command", handler_name="command",
            handler_module_path=fixture_module, handler=never_command,
            event_filters=[CommandGroupFilter("fixturegroup")],
            extras_configs={"priority": 0},
        ))
        with ExitStack() as bindings:
            bindings.enter_context(patch.dict(runtime.star_map, {
                fixture_module: SimpleNamespace(name="offline-missing-argument",
                                                activated=True, reserved=True)}))
            bindings.enter_context(patch("astrbot.core.pipeline.waking_check.stage.star_handlers_registry",
                                        registry))
            bindings.enter_context(patch("astrbot.core.star.session_plugin_manager.sp.get_async",
                                        new_callable=AsyncMock, return_value={}))
            # Positive hazard control: real native CommandGroupFilter does try an
            # early send without ingress. Collect locally; never use event.send.
            baseline = self._waking_event("/fixturegroup", "unguarded-native-baseline")
            baseline.send = AsyncMock(return_value=None)
            await original_process(stage, baseline)
            self.assertEqual(baseline.send.await_count, 1)
            self.assertTrue(baseline.is_stopped())
            self.assertIn("fixturegroup", baseline.send.await_args.args[0].get_plain_text())
            self.assertEqual(self.rows(), [])

            await self.plugin.initialize()
            self.assertIsNot(WakingCheckStage.process, original_process,
                             "initialize() did not install the actual ingress wrapper.")
            guarded = self._waking_event("/fixturegroup", "guarded-native-missing-argument")
            await self._assert_waking_blocked(stage, guarded)
            await self.plugin.terminate()
            self.assertIs(WakingCheckStage.process, original_process,
                          "terminate() did not restore this plugin's ingress wrapper.")

    async def test_real_waking_ingress_blocks_raw_alias_at_and_control_commands(self):
        stage = await self._waking_stage()
        await self.plugin.initialize()
        for index, text in enumerate(("/help", "／unknown", "鲸鲸，/help", "@fixture-bot /help",
                                      "[CQ:at,qq=fixture-bot] /help", "鲸鱼记忆", "鲸鱼风格 新风格")):
            with self.subTest(text=text):
                event = self._waking_event(text, "native-ingress-" + str(index))
                await self._assert_waking_blocked(stage, event)

    async def test_raw_stripped_unknown_and_fullwidth_commands_are_silent(self):
        for index, (raw, current) in enumerate((
            ("/help", "help"), ("/unknown_command", "unknown_command"),
            ("／help", "／help"), ("/help 大肥鱼", "help 大肥鱼"),
            ("/help", ""),
        )):
            with self.subTest(raw=raw, current=current):
                event = self.event(current, "raw-command-" + str(index), direct=True)
                event.message_obj.message_str = raw
                await self._assert_blocked(event)

    async def test_alias_text_at_and_real_at_cannot_rescue_slash_commands(self):
        for index, text in enumerate(("大肥鱼 /help", "鲸鲸，／unknown", "@fixture-bot /help",
                                      "[CQ:at,qq=fixture-bot] /help")):
            with self.subTest(text=text):
                await self._assert_blocked(self.event(text, "addressed-" + str(index), direct=True))
        event = self.event("help", "real-at-stripped", direct=True,
                           components=[runtime.At(qq=runtime.BOT), runtime.Plain("/help")])
        event.message_obj.message_str = "/help"
        await self._assert_blocked(event, CommandFilter("help"))

    async def test_activated_native_command_and_group_filters_stop_before_their_handlers(self):
        for text, item in (("help", CommandFilter("help")),
                           ("group sub", CommandGroupFilter("group"))):
            with self.subTest(filter=type(item).__name__):
                event = self.event(text, "activated-" + text, direct=True)
                # Deliberately no original slash: native filter metadata must suffice.
                self.assertEqual(event.message_obj.message_str, text)
                await self._assert_blocked(event, item)

    async def test_whale_memory_and_style_controls_do_not_send_or_change_settings(self):
        before_style = self.config.get("base_style")
        for index, text in enumerate(("鲸鱼记忆", "鲸鱼记忆 查询 合成", "鲸鱼记忆 整理",
                                      "鲸鱼风格", "鲸鱼风格 不允许写入的合成风格",
                                      "鲸鲸，鲸鱼风格 新风格")):
            with self.subTest(text=text):
                await self._assert_blocked(self.event(text, "management-" + str(index),
                                                       sender=runtime.ADMIN, direct=True))
        self.assertEqual(self.config.get("base_style"), before_style)
        self.assertEqual(self.config.saves, 0)

    async def test_command_at_night_is_not_remembered_or_sent(self):
        self.clock[0] = runtime.instant(0)
        await self._assert_blocked(self.event("/help", "night-command", direct=True))

    async def test_managed_admin_private_commands_are_also_silent(self):
        await self._assert_blocked(self.event("/help", "admin-private-command", group="",
                                               sender=runtime.ADMIN, direct=True))

    async def test_command_does_not_touch_existing_reply_or_pending_queue(self):
        first = self.event("鲸鲸，已有聊天不能被命令抢占", "existing-chat", direct=True)
        await self.plugin.observe(first)
        token = first.get_extra("whale_reply_token")
        self.assertTrue(token)
        await self._assert_blocked(self.event("/help 大肥鱼", "no-force-command", direct=True),
                                   CommandFilter("help"))
        self.assertTrue(self.plugin.replies.is_valid(runtime.SCOPE_A, token))

    async def test_sentence_quotes_urls_and_path_discussion_remain_normal_chat(self):
        for index, text in enumerate(("鲸鲸，我刚才用了/help", "大肥鱼，解释一下 /help",
                                      "@fixture-bot 我引用了/help", "“/help”", "`/help`",
                                      "https://example.org/help", "//example.org/help",
                                      "路径 /usr/local/help 在哪里", "鲸鱼记忆很好用")):
            with self.subTest(text=text):
                event = self.event(text, "normal-" + str(index), direct=True)
                before = len(self.rows())
                await self._native_process(event, allow_agent=True)
                self.assertFalse(event.is_stopped())
                self.assertTrue(event.get_extra("whale_reply_token"))
                self.assertEqual(len(self.rows()), before + 1)
                self.assertEqual(self.rows()[-1]["text"], text)
        self.assertEqual(len(self.agent_entries), 9)
        self.assertEqual(self.native_commands, [])
        self.assertEqual(self.context.model_calls, [])

    async def test_disabled_command_policy_keeps_native_command_behavior(self):
        self.config["ignore_commands"] = False
        event = self.event("help", "policy-disabled", direct=True)
        event.message_obj.message_str = "/help"
        await self._native_process(event, CommandFilter("help"))
        self.assertFalse(event.is_stopped())
        self.assertEqual(self.native_commands, ["help"])
        self.assertEqual([row["text"] for row in self.rows()], ["help"])
        self.assertFalse(event.get_extra("whale_reply_token"))
        self.assertEqual(self.agent_entries, [])
        self.assertEqual(self.context.model_calls, [])

    async def test_original_scope_self_echo_and_ordinary_night_rules_are_retained(self):
        for event in (self.event("/help", "outside-group", group="outside"),
                      self.event("/help", "outside-private", group="", sender="outsider"),
                      self.event("/help", "outside-platform", platform="other-adapter")):
            prior = runtime.MessageEventResult().message("OUT_OF_SCOPE_RESULT")
            event.set_result(prior)
            await self.plugin.observe(event)
            self.assertFalse(event.is_stopped())
            self.assertIs(event.get_result(), prior)
        echo = self.event("鲸鲸，我自己的回声", "self-echo", sender=runtime.BOT, direct=True)
        await self.plugin.observe(echo)
        self.assertTrue(echo.is_stopped())
        self.assertEqual(self.rows(), [])
        self.clock[0] = runtime.instant(0)
        night = self.event("鲸鲸，夜间普通聊天仍观察", "night-normal", direct=True)
        await self.plugin.observe(night)
        self.assertTrue(night.is_stopped())
        self.assertEqual([row["text"] for row in self.rows()], [night.message_str])
        self.assertFalse(night.get_extra("whale_reply_token"))
        self.assertEqual(self.context.model_calls, [])


if __name__ == "__main__":
    unittest.main(defaultTest="CommandPolicyRehearsal", verbosity=2)
