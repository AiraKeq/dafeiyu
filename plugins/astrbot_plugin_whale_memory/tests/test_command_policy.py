"""Standard-library-only command classification tests; no AstrBot is imported."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "whale_command_policy", Path(__file__).resolve().parents[1] / "command_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
ALIASES = ("大肥鱼", "鲸鲸", "鲸鱼娘", "大鲸鱼", "小鲸鱼")

# Class names mirror real native metadata, without requiring its dependency tree.
CommandFilter = type("CommandFilter", (), {})
CommandGroupFilter = type("CommandGroupFilter", (), {})
RegexFilter = type("RegexFilter", (), {})
EventMessageTypeFilter = type("EventMessageTypeFilter", (), {})
GroupFilter = type("GroupFilter", (), {})


class CustomCommandFilter(CommandFilter):
    pass


class FakeEvent:
    def __init__(self, text="", original=None, filters=(), module="fixture.builtin"):
        self.message_str = text
        self.message_obj = SimpleNamespace(message_str=text if original is None else original)
        self.is_at_or_wake_command = True
        self.extras = {"activated_handlers": [SimpleNamespace(
            handler_module_path=module, event_filters=list(filters))]}
        self.clear_result = Mock()
        self.stop_event = Mock()
        self.send = Mock()

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


class CommandTextTests(unittest.TestCase):
    def test_initial_known_unknown_and_fullwidth_commands(self):
        for text in ("/help", " /help 大肥鱼 ", "／help", "\t／未知 参数\n",
                     "/totally-unknown", "/鲸鱼记忆", "/", "／"):
            with self.subTest(text=text):
                self.assertIs(policy.is_command_text(text, ALIASES), True)

    def test_alias_or_at_before_commands(self):
        for text in ("大肥鱼/help", "鲸鲸， /help", "鲸鱼娘：／unknown arg",
                     "@123 /help", "@合成昵称/help", "＠合成昵称／help",
                     "[CQ:at,qq=123] /help", "[CQ:at,qq=123,name=合成]／help",
                     "@123 大肥鱼，/help", "大肥鱼 @123 /unknown"):
            with self.subTest(text=text):
                self.assertTrue(policy.is_command_text(text, ALIASES))

    def test_management_controls_are_commands_without_slash(self):
        for text in ("鲸鱼记忆", "鲸鱼记忆 查询 今天", "鲸鱼风格", "鲸鱼风格 新风格",
                     "鲸鲸，鲸鱼记忆", "@123 鲸鱼风格 新风格", "／鲸鱼风格"):
            with self.subTest(text=text):
                self.assertTrue(policy.is_command_text(text, ALIASES))

    def test_mentions_quotes_paths_and_urls_in_conversation_are_not_commands(self):
        for text in ("我刚才用了/help", "解释一下 /help", "大肥鱼，我刚才用了/help",
                     "鲸鲸，你知道/help吗",
                     "@123 我引用了 /help", '"/help"', "“/help”", "`/help`",
                     "> /help", "https://example.org/help", "http://example.org/a/help?q=/help",
                     "//example.org/help", "大肥鱼 https://example.org/help",
                     "鲸鲸 //example.org/help", "路径 /usr/local/help 在哪里",
                     "F:/fixture/help", "鲸鱼记忆很好用", "鲸鱼风格是什么",
                     "关于鲸鱼记忆的讨论", "help", "@123 help", "鲸鲸", "@123", ""):
            with self.subTest(text=text):
                self.assertIs(policy.is_command_text(text, ALIASES), False)

    def test_aliases_are_configurable_and_longest_match_first(self):
        self.assertFalse(policy.is_command_text("合成鱼 /help", ALIASES))
        self.assertTrue(policy.is_command_text("合成鱼 /help", ("合成", "合成鱼")))
        self.assertTrue(policy.is_command_text("合成鱼／help", "合成鱼"))
        self.assertTrue(policy.is_command_text("合成鱼 /help", ("", None, "合成鱼", "合成鱼")))
        self.assertFalse(policy.is_command_text("大肥鱼 /help"))

    def test_invalid_text_is_not_a_command(self):
        for text in (None, 123, False, [], {}, b"/help"):
            self.assertIs(policy.is_command_text(text), False)


class CommandEventTests(unittest.TestCase):
    def test_original_message_survives_native_slash_stripping(self):
        for original in ("/help", "／unknown", "鲸鲸， /help", "@123 /help"):
            event = FakeEvent("help", original=original)
            self.assertTrue(policy.should_ignore_command(event, ALIASES))

    def test_current_message_is_also_checked(self):
        event = FakeEvent("/unknown", original="")
        self.assertTrue(policy.should_ignore_command(event, ALIASES))

    def test_activated_native_filters_identify_stripped_commands(self):
        for item in (CommandFilter(), CommandGroupFilter(), CustomCommandFilter()):
            for module in ("fixture.builtin", "astrbot_plugin_whale_memory.main"):
                with self.subTest(filter=type(item).__name__, module=module):
                    event = FakeEvent("help", filters=(RegexFilter(), item), module=module)
                    self.assertIs(policy.should_ignore_command(event), True)

    def test_only_activated_command_filters_are_authoritative(self):
        event = FakeEvent("我用了/help", filters=(RegexFilter(), EventMessageTypeFilter(), GroupFilter()))
        event.extras["unactivated_handlers"] = [SimpleNamespace(event_filters=[CommandFilter()])]
        self.assertFalse(policy.should_ignore_command(event))
        event.extras["activated_handlers"].append(SimpleNamespace(event_filters=[CommandFilter()]))
        self.assertTrue(policy.should_ignore_command(event))

    def test_wake_flags_do_not_turn_help_discussion_into_command(self):
        for text in ("help", "@123 help", "鲸鲸，我刚才用了/help", "https://example.org/help"):
            event = FakeEvent(text)
            self.assertFalse(policy.should_ignore_command(event, ALIASES))

    def test_missing_optional_event_metadata_is_safe(self):
        self.assertFalse(policy.should_ignore_command(None))
        self.assertFalse(policy.should_ignore_command(SimpleNamespace(message_str="hello")))
        self.assertTrue(policy.should_ignore_command(SimpleNamespace(message_str="/help")))
        event = FakeEvent("hello")
        event.extras["activated_handlers"] = [None, SimpleNamespace(), SimpleNamespace(event_filters=None)]
        self.assertFalse(policy.should_ignore_command(event))
        event.extras["activated_handlers"] = None
        self.assertFalse(policy.should_ignore_command(event))

    def test_alias_generator_applies_to_both_text_sources(self):
        event = FakeEvent("合成鱼 /help", original="ordinary")
        self.assertTrue(policy.should_ignore_command(event, (name for name in ("合成魚", "合成鱼"))))

    def test_policy_does_not_mutate_send_stop_or_clear(self):
        event = FakeEvent("help", original="/help", filters=(CommandFilter(),))
        before = copy.deepcopy((event.message_str, event.message_obj, event.extras))
        self.assertTrue(policy.should_ignore_command(event, ALIASES))
        self.assertEqual(event.message_str, before[0])
        self.assertEqual(event.message_obj, before[1])
        self.assertEqual(event.extras["activated_handlers"][0].handler_module_path,
                         before[2]["activated_handlers"][0].handler_module_path)
        self.assertEqual(len(event.extras["activated_handlers"][0].event_filters), 1)
        event.clear_result.assert_not_called()
        event.stop_event.assert_not_called()
        event.send.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
