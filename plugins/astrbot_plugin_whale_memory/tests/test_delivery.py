"""Offline stdlib tests for dynamic QQ budgets, lossless pages and legacy API.

Run from qq-deepseek-deploy with PYTHONDONTWRITEBYTECODE=1:
    python -m unittest discover -s plugins/astrbot_plugin_whale_memory/tests \
        -p test_delivery.py -v

Both modules load directly by importlib file path, never through AstrBot's
plugin __init__.  Imports and execution are socket/DNS guarded.  The fixed-seed
fuzz corpus contains exactly 1000 cases, in addition to explicit 5000-character,
UTF, navigation, strict-parameter and old 1..20 compatibility fixtures.
"""

import sys

sys.dont_write_bytecode = True

import ast
import builtins
from contextlib import ExitStack
from fractions import Fraction
import importlib.util
import inspect
import math
from pathlib import Path
import random
import socket
import unittest
from unittest import mock


_ROOT = Path(__file__).resolve().parents[1]
_SEGMENTATION_PATH = _ROOT / "segmentation.py"
_DELIVERY_PATH = _ROOT / "delivery.py"
_DEFAULT_HINT = "说“继续”看下一页。"
_DEFAULT_LIMITS = (20, 40, 60)
_SCENES = {"casual", "playful", "comfort", "explanation"}
_REQUIRED_RESULT = {"full_text", "pages", "scene", "weights", "source_chars"}
_REQUIRED_PAGE = {
    "content", "display_text", "segments", "char_limit", "continuation", "index",
    "source_start", "source_end", "score_plan",
}
_IMPORTED_ROOTS = set()


def _offline_guard(stack):
    failure = AssertionError("Delivery tests must not access the network or cloud")
    for attribute in ("connect", "connect_ex", "sendto"):
        stack.enter_context(mock.patch.object(socket.socket, attribute, side_effect=failure))
    for attribute in ("create_connection", "getaddrinfo"):
        stack.enter_context(mock.patch.object(socket, attribute, side_effect=failure))


def _load_file(path, name):
    original_import = builtins.__import__

    def stdlib_import(module_name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            raise AssertionError("Standalone delivery must not initialize a plugin package")
        root = module_name.split(".", 1)[0]
        _IMPORTED_ROOTS.add(root)
        if root not in sys.stdlib_module_names | {"builtins"}:
            raise AssertionError("Non-stdlib import: {!r}".format(module_name))
        return original_import(module_name, globals, locals, fromlist, level)

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load standalone file: {}".format(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        with ExitStack() as stack:
            _offline_guard(stack)
            stack.enter_context(mock.patch.object(builtins, "__import__", stdlib_import))
            spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


SEGMENTATION = _load_file(_SEGMENTATION_PATH, "_whale_delivery_test_segmentation")
DELIVERY = _load_file(_DELIVERY_PATH, "_whale_delivery_test_target")


def _normalized(weights):
    values = [Fraction(value) for value in weights]
    peak = max(values)
    scaled = [float(value / peak) for value in values]
    total = math.fsum(scaled)
    return [value / total for value in scaled]


class DeliveryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        _offline_guard(stack)

    def assert_scores(self, plan, display, limit, budget, draw):
        self.assertIsInstance(plan, dict)
        self.assertEqual(plan["source_chars"], len(display))
        self.assertEqual(plan["chosen_count"], len(plan["segments"]))
        self.assertEqual(plan["mode"], "scored")
        self.assertEqual(plan["whitespace_segments"], [])
        self.assertEqual("".join(plan["segments"]), display)
        by_count = {candidate["count"]: candidate for candidate in plan["candidates"]}
        self.assertEqual(set(by_count), {1, 2, 3})
        for count, candidate in by_count.items():
            for key in ("prior", "scene_fit", "completeness", "naturalness", "score"):
                value = candidate[key]
                self.assertIsInstance(value, (int, float))
                self.assertNotIsInstance(value, bool)
                self.assertTrue(math.isfinite(value))
                self.assertGreaterEqual(value, 0)
            self.assertAlmostEqual(candidate["prior"], plan["weights"][count - 1], places=12)
            product = (candidate["prior"] * candidate["scene_fit"]
                       * candidate["completeness"] * candidate["naturalness"])
            self.assertTrue(math.isclose(candidate["score"], product,
                                         rel_tol=1e-12, abs_tol=1e-14))
            self.assertIsInstance(candidate["feasible"], bool)
            if count > budget:
                self.assertFalse(candidate["feasible"])
                self.assertEqual(candidate["reason"], "message_budget")
            if not candidate["feasible"]:
                self.assertEqual(candidate["completeness"], 0)
                self.assertEqual(candidate["score"], 0)
                self.assertEqual(candidate["boundaries"], [])
                continue
            cuts = candidate["boundaries"]
            self.assertEqual(len(cuts), count - 1)
            self.assertEqual(cuts, sorted(set(cuts)))
            self.assertTrue(all(type(cut) is int and 0 < cut < len(display) for cut in cuts))
            offsets = [0, *cuts, len(display)]
            parts = [display[start:end] for start, end in zip(offsets, offsets[1:])]
            self.assertEqual("".join(parts), display)
            self.assertTrue(all(1 <= len(part) <= limit and not part.isspace() for part in parts))
            self.assertEqual(candidate["completeness"], 1)
        selected = by_count[plan["chosen_count"]]
        self.assertTrue(selected["feasible"])
        cuts = selected["boundaries"]
        offsets = [0, *cuts, len(display)]
        self.assertEqual([display[start:end] for start, end in zip(offsets, offsets[1:])],
                         plan["segments"])
        if draw is None:
            self.assertAlmostEqual(selected["score"],
                                   max(candidate["score"] for candidate in by_count.values()),
                                   places=12)

    def assert_delivery(self, result, text, *, incoming="", weights=(60, 30, 10),
                        draw=None, max_messages=3, short_limit=20, medium_limit=40,
                        long_limit=60, continuation_hint=_DEFAULT_HINT):
        self.assertIsInstance(result, dict)
        self.assertTrue(_REQUIRED_RESULT.issubset(result))
        self.assertEqual(result["full_text"], text)
        self.assertEqual(result["source_chars"], len(text))
        self.assertIs(type(result["source_chars"]), int)
        self.assertEqual(result["scene"], SEGMENTATION.classify_scene(incoming, text))
        self.assertIn(result["scene"], _SCENES)
        self.assertEqual(len(result["weights"]), 3)
        for actual, expected in zip(result["weights"], _normalized(weights)):
            self.assertTrue(math.isfinite(actual))
            self.assertGreaterEqual(actual, 0)
            self.assertAlmostEqual(actual, expected, places=12)
        self.assertAlmostEqual(math.fsum(result["weights"]), 1.0, places=12)
        pages = result["pages"]
        self.assertIsInstance(pages, list)
        if not text or text.isspace():
            self.assertEqual(pages, [])
            self.assertEqual(result["complete"], not text)
            self.assertEqual(result["exception"], bool(text))
            self.assertEqual(result["constraint_fallback"],
                             "whitespace_only_not_sent" if text else "none")
            return result
        self.assertTrue(pages)
        self.assertTrue(result["complete"])
        self.assertEqual("".join(page["content"] for page in pages), text,
                         "Original punctuation, whitespace and tail must all survive")
        previous = 0
        limits = (short_limit, medium_limit, long_limit)
        suffix = "\n" + continuation_hint if continuation_hint else ""
        for index, page in enumerate(pages):
            self.assertIsInstance(page, dict)
            self.assertTrue(_REQUIRED_PAGE.issubset(page))
            for key in ("index", "source_start", "source_end", "char_limit"):
                self.assertIs(type(page[key]), int)
            self.assertEqual(page["index"], index)
            self.assertEqual(page["source_start"], previous)
            self.assertGreater(page["source_end"], page["source_start"])
            self.assertLessEqual(page["source_end"], len(text))
            self.assertEqual(page["content"], text[page["source_start"]:page["source_end"]])
            self.assertFalse(page["content"].isspace())
            previous = page["source_end"]
            self.assertIs(type(page["continuation"]), bool)
            self.assertEqual(page["continuation"], index < len(pages) - 1)
            expected_display = page["content"] + suffix if page["continuation"] else page["content"]
            self.assertEqual(page["display_text"], expected_display)
            self.assertIsInstance(page["segments"], list)
            self.assertEqual("".join(page["segments"]), page["display_text"])
            self.assertIn(page["char_limit"], limits)
            self.assertLessEqual(page["char_limit"], 60)
            self.assertGreaterEqual(len(page["segments"]), 1)
            self.assertLessEqual(len(page["segments"]), max_messages)
            self.assertLessEqual(len(page["display_text"]), max_messages * page["char_limit"])
            for segment in page["segments"]:
                self.assertIsInstance(segment, str)
                self.assertGreaterEqual(len(segment), 1)
                self.assertLessEqual(len(segment), page["char_limit"])
                self.assertFalse(segment.isspace(), "QQ must not receive a blank-only bubble")
            self.assertEqual(page["score_plan"]["segments"], page["segments"])
            self.assertEqual(page["score_plan"]["scene"], result["scene"])
            self.assert_scores(page["score_plan"], page["display_text"], page["char_limit"],
                               max_messages, draw)
            for reason in page["page_boundary_reasons"]:
                self.assertEqual(reason["offset"], page["source_end"])
                self.assertTrue(page["continuation"])
        self.assertEqual(previous, len(text))
        self.assertFalse(pages[-1]["continuation"])
        self.assertEqual(pages[-1]["display_text"], pages[-1]["content"])
        return result

    def check(self, text, **kwargs):
        # Materialize weights only for an independent assertion if needed.
        result = DELIVERY.plan_delivery(text, **kwargs)
        return self.assert_delivery(result, text, **kwargs)

    def test_public_signature_and_stdlib_only_file_loading(self):
        signature = inspect.signature(DELIVERY.plan_delivery)
        self.assertEqual(list(signature.parameters), [
            "text", "incoming", "weights", "draw", "max_messages", "short_limit",
            "medium_limit", "long_limit", "continuation_hint",
        ])
        for name, parameter in signature.parameters.items():
            expected_kind = (inspect.Parameter.POSITIONAL_OR_KEYWORD if name == "text"
                             else inspect.Parameter.KEYWORD_ONLY)
            self.assertEqual(parameter.kind, expected_kind)
        self.assertEqual(signature.parameters["weights"].default, (60, 30, 10))
        self.assertEqual(signature.parameters["continuation_hint"].default, _DEFAULT_HINT)
        self.assertTrue(callable(SEGMENTATION.plan_segments_extended))
        self.assertNotIn("astrbot", _IMPORTED_ROOTS)
        self.assertTrue(_IMPORTED_ROOTS.issubset(sys.stdlib_module_names | {"builtins"}))
        for path in (_DELIVERY_PATH, _SEGMENTATION_PATH):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0)
                    roots = [(node.module or "").split(".", 1)[0]]
                else:
                    continue
                for root in roots:
                    self.assertIn(root, sys.stdlib_module_names | {"builtins"})

    def test_daily_replies_use_twenty_character_bubbles(self):
        for text in ("嗯", "好呀", "我在，慢慢说。", "今晚也要做个好梦。", "春" * 20):
            with self.subTest(text=text):
                result = self.check(text)
                self.assertEqual(len(result["pages"]), 1)
                self.assertEqual(result["pages"][0]["char_limit"], 20)
                self.assertEqual(result["pages"][0]["segments"], [text])

    def test_default_39_40_60_61_120_180_capacity_adaptation(self):
        expected_limits = {
            20: 20, 39: 20, 40: 20, 41: 20, 60: 20,
            61: 40, 79: 40, 80: 40, 120: 40, 121: 60, 179: 60, 180: 60,
        }
        for length, expected in expected_limits.items():
            with self.subTest(length=length):
                result = self.check("春" * length)
                self.assertEqual(len(result["pages"]), 1)
                self.assertEqual(result["pages"][0]["char_limit"], expected)
                self.assertFalse(result["pages"][0]["continuation"])

    def test_181_requires_pages_not_a_truncated_reply(self):
        text = "春" * 177 + "末尾保全"
        self.assertEqual(len(text), 181)
        result = self.check(text)
        self.assertGreaterEqual(len(result["pages"]), 2)
        self.assertEqual(result["pages"][0]["char_limit"], 60)
        self.assertEqual(result["pages"][-1]["content"][-4:], "末尾保全")

    def test_four_short_protected_words_under_sixty_try_forty(self):
        text = " ".join(character * 13 for character in "abcd")
        self.assertEqual(len(text), 55)
        legacy = SEGMENTATION.plan_segments(text)
        self.assertGreater(legacy["chosen_count"], 3)
        result = self.check(text)
        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(result["pages"][0]["char_limit"], 40)

    def test_four_medium_words_try_sixty_before_paging(self):
        text = " ".join(character * 26 for character in "abcd")
        self.assertEqual(len(text), 107)
        medium = SEGMENTATION.plan_segments_extended(text, max_chars=40)
        self.assertGreater(medium["chosen_count"], 3)
        result = self.check(text)
        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(result["pages"][0]["char_limit"], 60)

    def test_5000_characters_all_budgets_preserve_tail(self):
        text = ("春风拂过花海" * 1000)[:4996] + "末尾保全"
        self.assertEqual(len(text), 5000)
        for budget in (1, 2, 3):
            with self.subTest(budget=budget):
                result = self.check(text, incoming="请详细解释。", max_messages=budget)
                self.assertGreater(len(result["pages"]), 1)
                self.assertEqual(result["pages"][-1]["content"][-4:], "末尾保全")

    def test_max_messages_one_two_three_changes_whole_capacity(self):
        for budget in (1, 2, 3):
            for length in (39, 40, 60, 61, 120, 180, 181):
                with self.subTest(budget=budget, length=length):
                    result = self.check("春" * length, max_messages=budget)
                    self.assertEqual(len(result["pages"]) == 1, length <= budget * 60)

    def test_nonfinal_navigation_is_in_same_three_message_budget(self):
        result = self.check("春" * 500)
        for page in result["pages"][:-1]:
            self.assertLessEqual(len(page["segments"]), 3)
            self.assertTrue(page["display_text"].endswith("\n" + _DEFAULT_HINT))
            self.assertLessEqual(len(page["content"]), 180 - len("\n" + _DEFAULT_HINT))

    def test_one_message_budget_merges_navigation(self):
        result = self.check("春" * 181, max_messages=1)
        for page in result["pages"][:-1]:
            self.assertEqual(len(page["segments"]), 1)
            self.assertEqual(page["segments"][0], page["content"] + "\n" + _DEFAULT_HINT)
            self.assertLessEqual(len(page["content"]), 60 - len("\n" + _DEFAULT_HINT))

    def test_body_hint_literal_and_history_are_separate(self):
        text = _DEFAULT_HINT + "春" * 200 + _DEFAULT_HINT
        result = self.check(text)
        self.assertEqual("".join(page["content"] for page in result["pages"]), text)
        self.assertEqual(result["pages"][-1]["display_text"], result["pages"][-1]["content"])
        self.assertNotEqual("".join(page["display_text"] for page in result["pages"]), text)
        self.assertEqual(result["full_text"], text)

    def test_empty_and_whitespace_are_not_claimed_sent(self):
        for text in ("", " ", "\t\n\r", "\u3000\u00a0\u2003\u202f", " " * 5000):
            with self.subTest(text=repr(text[:30])):
                result = self.check(text)
                self.assertEqual(result["pages"], [])
                if text:
                    self.assertFalse(result["complete"])
                    self.assertEqual(result["reason"], "whitespace_only_not_sent")

    def test_all_utf_whitespace_crlf_indent_and_punctuation_are_lossless(self):
        fixtures = (
            "  春风来了，花开了！  ",
            "第一句。\n第二句？\n第三句！\n",
            "  一、准备材料。\n    二、慢慢搅拌。\n\t三、等待完成。\n",
            "\r\n    第一步：保留 CRLF。\r\n\t第二步：保留 tab。\r\n",
            "甲  乙   丙。\n\n  丁\t戊；己……庚——辛。  ",
            "\u3000全角空格\u00a0不换行\u2003em\u202f窄空格\x85下一行\u2028行\u2029段\v竖\f换页。",
            "  " + "春风。！？……——\r\n\t  " * 30 + "末尾还在。  ",
        )
        for text in fixtures:
            for budget in (1, 2, 3):
                with self.subTest(text=repr(text[:40]), budget=budget):
                    self.check(text, max_messages=budget)

    def test_utf_emoji_combining_and_zwj_preserve_code_points(self):
        fixtures = (
            "😀🧠👩\u200d🚀👨\u200d👩\u200d👧\u200d👦❤️你好！🙂" * 20,
            "e\u0301 café naïve，字形不归一化。\n" * 20,
            "变量𝛼=𝟙。再看𠮷与𠀀，正文完整。" * 20,
        )
        for text in fixtures:
            with self.subTest(prefix=repr(text[:30])):
                self.check(text)

    def test_unrepresentable_extreme_mixed_whitespace_fails_closed(self):
        fixtures = (
            " " * 60 + "x", "x" + " " * 60,
            "x" + " " * 119 + "y", "abc" + "\n\t " * 40 + "def",
            "x" + " " * 5000 + "y",
        )
        for text in fixtures:
            for budget in (1, 2, 3):
                with self.subTest(length=len(text), budget=budget):
                    with self.assertRaisesRegex(ValueError, "QQ|nonblank"):
                        DELIVERY.plan_delivery(text, max_messages=budget)

    def test_representable_whitespace_escalates_to_sixty(self):
        for text in (" " * 59 + "x", "x" + " " * 59, "x" + " " * 118 + "y"):
            with self.subTest(length=len(text)):
                result = self.check(text)
                self.assertEqual(len(result["pages"]), 1)
                self.assertEqual(result["pages"][0]["char_limit"], 60)

    def test_pagination_preserves_long_blank_runs_and_larger_final_tail(self):
        fixtures = (
            "开头" + " " * 80 + "中间" + " " * 80 + "末尾" + "春" * 200,
            "春" * 300 + "x" + " " * 59,
            " " * 45 + "x" + "春" * 300,
        )
        for text in fixtures:
            for budget in (1, 2, 3):
                with self.subTest(length=len(text), budget=budget):
                    self.check(text, max_messages=budget)

    def test_long_url_and_words_may_force_cut_without_losing_text(self):
        fixtures = (
            "访问 https://example.invalid/" + "very-long-path/" * 50 + "?q=a%20b&x=1#tail",
            "A" * 500 + "Z",
            "编号 " + "0123456789" * 60 + " 完整。",
        )
        for text in fixtures:
            with self.subTest(prefix=repr(text[:35])):
                result = self.check(text)
                reasons = [reason["reason"]
                           for page in result["pages"]
                           for reason in page["score_plan"]["forced_reasons"]
                           + page["page_boundary_reasons"]]
                self.assertTrue(any(reason in {"url", "ascii_word", "number"} for reason in reasons))

    def test_fitting_words_brackets_and_crlf_are_protected(self):
        unit = "abcdefghijklmnop"
        bracket = "（保留括号与内容）"
        text = ("春风。" * 35 + " " + unit + " " + bracket + "\r\n" + "花开。" * 70)
        result = self.check(text)
        intervals = []
        for value in (unit, bracket, "\r\n"):
            start = text.index(value)
            intervals.append((start, start + len(value)))
        cuts = []
        for page in result["pages"]:
            if page["continuation"]:
                cuts.append(page["source_end"])
            for boundary in page["score_plan"]["boundaries"]:
                if boundary < len(page["content"]):
                    cuts.append(page["source_start"] + boundary)
        for start, end in intervals:
            self.assertFalse(any(start < cut < end for cut in cuts), (start, end, cuts))

    def test_custom_limits_are_strict_and_honored(self):
        for short, medium, long in ((20, 30, 50), (21, 41, 59), (58, 59, 60)):
            for budget in (1, 2, 3):
                with self.subTest(limits=(short, medium, long), budget=budget):
                    self.check("春" * 301, max_messages=budget, short_limit=short,
                               medium_limit=medium, long_limit=long)
        result = self.check("春" * 28, max_messages=1, short_limit=20,
                            medium_limit=30, long_limit=50)
        self.assertEqual(result["pages"][0]["char_limit"], 30)

    def test_custom_empty_and_long_navigation_obey_budget(self):
        for hint in ("", "继续?", " NEXT ", "N" * 100, "N" * 168):
            with self.subTest(hint_length=len(hint)):
                self.check("春" * 401, continuation_hint=hint)
        result = self.check("春" * 401, continuation_hint="")
        self.assertTrue(all(page["display_text"] == page["content"] for page in result["pages"]))

    def test_unsendable_hint_fails_only_when_pagination_needs_it(self):
        for budget in (1, 2, 3):
            hint = "N" * (budget * 60 - 1)
            with self.subTest(budget=budget):
                with self.assertRaisesRegex(ValueError, "continuation_hint|budget"):
                    DELIVERY.plan_delivery("春" * 181, max_messages=budget,
                                           continuation_hint=hint)
                self.check("你好。", max_messages=budget, continuation_hint=hint)
        with self.assertRaises(ValueError):
            DELIVERY.plan_delivery("春" * 181, continuation_hint=" " * 100)

    def test_scenes_come_from_original_reply_not_navigation(self):
        fixtures = (
            ("早上好", "早安。" * 100, "casual"),
            ("我好难过", "我会一直陪着你。" * 100, "comfort"),
            ("请解释算法", "步骤：先观察，再尝试。" * 100, "explanation"),
            ("给个笑话", "哈哈，逗你的。" * 100, "playful"),
        )
        for incoming, text, scene in fixtures:
            with self.subTest(scene=scene):
                result = self.check(text, incoming=incoming, continuation_hint="详细教程解释")
                self.assertEqual(result["scene"], scene)

    def test_normalized_weights_zero_priors_and_extreme_finite_values(self):
        fixtures = (
            (60, 30, 10), (1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 4, 1),
            (1e308, 1e308, 1e308), (5e-324, 5e-324, 5e-324),
            (10 ** 1000, 10 ** 999, 1),
            (Fraction(1, 10 ** 1000), Fraction(2, 10 ** 1000), 0),
        )
        for weights in fixtures:
            for text in ("我在。", "春" * 61, "风来了呀。花也开了。" * 30):
                with self.subTest(weights=repr(weights)[:60], length=len(text)):
                    self.check(text, weights=weights)

    def test_weight_generator_consumed_once_for_many_pages(self):
        consumed = []

        def weights():
            for value in (60, 30, 10):
                consumed.append(value)
                yield value

        result = DELIVERY.plan_delivery("春" * 1000, weights=weights())
        self.assert_delivery(result, "春" * 1000, weights=(60, 30, 10))
        self.assertEqual(consumed, [60, 30, 10])

    def test_repeatable_defaults_and_explicit_draws(self):
        text = "春風。花开啦！\n" * 60
        for draw in (None, 0, 0.5, math.nextafter(1.0, 0.0)):
            with self.subTest(draw=draw):
                expected = self.check(text, draw=draw)
                self.assertEqual(expected, self.check(text, draw=draw))
        expected = self.check("花开了呀。风也很温柔。")
        self.assertEqual(expected, self.check("花开了呀。风也很温柔。"))

    def test_draw_can_only_choose_near_tied_feasible_counts(self):
        text = "花开了呀。风也很温柔。"
        core = SEGMENTATION.plan_segments(text, scene="casual")
        candidates = {candidate["count"]: candidate for candidate in core["candidates"]}
        q1 = candidates[1]["scene_fit"] * candidates[1]["naturalness"]
        q2 = candidates[2]["scene_fit"] * candidates[2]["naturalness"]
        weights = (q2, q1, 0)
        chosen = set()
        for draw in (0, 0.25, 0.75, math.nextafter(1.0, 0.0)):
            result = self.check(text, weights=weights, draw=draw)
            chosen.add(result["pages"][0]["score_plan"]["chosen_count"])
        self.assertEqual(chosen, {1, 2})

    def test_legacy_signature_and_all_one_to_twenty_limits_stay_identical(self):
        signature = inspect.signature(SEGMENTATION.plan_segments)
        self.assertEqual(list(signature.parameters),
                         ["text", "incoming", "scene", "max_chars", "weights", "draw"])
        fixtures = ("", "\t\n ", "  春风来了。\r\n    花也开了。末尾保全。",
                    "https://example.invalid/" + "a" * 40, "x" + " " * 80 + "y")
        for cap in range(1, 21):
            for text in fixtures:
                with self.subTest(cap=cap, length=len(text)):
                    legacy = SEGMENTATION.plan_segments(text, max_chars=cap)
                    extended = SEGMENTATION.plan_segments_extended(text, max_chars=cap)
                    self.assertEqual(legacy, extended)
                    if legacy["segments"]:
                        self.assertEqual("".join(legacy["segments"]), text)
                        self.assertTrue(all(1 <= len(part) <= cap for part in legacy["segments"]))
        for cap in (21, 40, 60):
            for text in ("", "你好"):
                with self.assertRaises(ValueError):
                    SEGMENTATION.plan_segments(text, max_chars=cap)

    def test_extended_limits_and_explicit_bounded_message_budget(self):
        for cap in (1, 20, 21, 40, 60):
            text = "春" * (cap * 3 + 1)
            result = SEGMENTATION.plan_segments_extended(text, max_chars=cap)
            self.assertEqual("".join(result["segments"]), text)
            self.assertTrue(all(1 <= len(part) <= cap for part in result["segments"]))
            for budget in (1, 2, 3):
                with self.assertRaises(ValueError):
                    SEGMENTATION.plan_segments_extended(text, max_chars=cap,
                                                        max_messages=budget, allow_long=False)
        for budget in (1, 2, 3):
            text = "春" * (60 * budget)
            plan = SEGMENTATION.plan_segments_extended(text, max_chars=60,
                                                       max_messages=budget, allow_long=False)
            self.assertLessEqual(len(plan["segments"]), budget)
        for invalid in (0, 61, True, False, 60.0, None, "60", math.inf, math.nan):
            with self.assertRaises((TypeError, ValueError)):
                SEGMENTATION.plan_segments_extended("你好", max_chars=invalid)
        for invalid in (0, 4, True, 2.0, None):
            with self.assertRaises((TypeError, ValueError)):
                SEGMENTATION.plan_segments_extended("你好", max_messages=invalid)
        for invalid in (None, 0, 1, "true"):
            with self.assertRaises(TypeError):
                SEGMENTATION.plan_segments_extended("你好", allow_long=invalid)

    def test_invalid_string_arguments_raise_without_coercion(self):
        for invalid in (None, True, False, 0, 1.5, b"text", [], {}, object()):
            with self.subTest(argument="text", value=repr(invalid)):
                with self.assertRaises(TypeError):
                    DELIVERY.plan_delivery(invalid)
            for name in ("incoming", "continuation_hint"):
                for text in ("", "你好"):
                    with self.subTest(argument=name, value=repr(invalid), text=text):
                        with self.assertRaises(TypeError):
                            DELIVERY.plan_delivery(text, **{name: invalid})

    def test_invalid_message_budgets_reject_bool_float_and_out_of_range(self):
        invalids = (None, True, False, 0, -1, 4, 10 ** 100, 1.0, 2.5, "3",
                    Fraction(3, 1), math.nan, math.inf, [], {})
        for invalid in invalids:
            for text in ("", "你好", "春" * 181):
                with self.subTest(value=repr(invalid), length=len(text)):
                    with self.assertRaises((TypeError, ValueError)):
                        DELIVERY.plan_delivery(text, max_messages=invalid)

    def test_invalid_limits_reject_bool_float_range_and_nonincreasing(self):
        for name in ("short_limit", "medium_limit", "long_limit"):
            for invalid in (None, True, False, 0, 19, 61, 1000, 20.0, "40",
                            math.nan, math.inf, [], {}):
                for text in ("", "你好"):
                    with self.subTest(name=name, value=repr(invalid), text=text):
                        with self.assertRaises((TypeError, ValueError)):
                            DELIVERY.plan_delivery(text, **{name: invalid})
        for short, medium, long in ((20, 20, 60), (20, 40, 40), (60, 40, 20),
                                    (30, 20, 60), (20, 60, 60)):
            with self.assertRaises(ValueError):
                DELIVERY.plan_delivery("", short_limit=short, medium_limit=medium, long_limit=long)

    def test_invalid_weights_reject_nonfinite_negative_bool_and_wrong_count(self):
        invalids = (None, True, (), (1,), (1, 2), (1, 2, 3, 4), "abc", {},
                    (0, 0, 0), (-1, 2, 3), (True, 2, 3), (1, math.nan, 1),
                    (1, math.inf, 1), (1, -math.inf, 1), (1, "2", 3), (1, None, 3),
                    (1, 2 + 0j, 3))
        for invalid in invalids:
            for text in ("", "你好", "春" * 181):
                with self.subTest(weights=repr(invalid), length=len(text)):
                    with self.assertRaises((TypeError, ValueError)):
                        DELIVERY.plan_delivery(text, weights=invalid)

    def test_invalid_draw_is_rejected_for_all_modes(self):
        invalids = (True, False, -0.01, 1, 1.01, math.nan, math.inf, -math.inf,
                    "0.5", [], {}, lambda: 0.5, 0.5 + 0j)
        for invalid in invalids:
            for text in ("", "你好", "春" * 181):
                with self.subTest(draw=repr(invalid), length=len(text)):
                    with self.assertRaises((TypeError, ValueError)):
                        DELIVERY.plan_delivery(text, draw=invalid)

    def test_fixed_seed_1000_case_fuzz_preserves_every_character_or_fails_closed(self):
        rng = random.Random(0xD311_2026)
        alphabet = list("春风花海你好甲乙丙丁abcdefXYZ0123456789，。！？；：()[]{}<>/%&=+-_“”‘’")
        alphabet += [" ", "\t", "\n", "\r", "\u3000", "\u00a0", "\u2003", "\u202f",
                     "\u2028", "\u2029", "😀", "🧠", "👩", "🚀", "\u200d", "\ufe0f", "\u0301"]
        incoming_choices = ("", "今天好吗？", "哈哈哈，逗你玩。", "我很难过。", "请解释原理。")
        limits_choices = ((20, 40, 60), (20, 30, 50), (21, 41, 59), (58, 59, 60))
        for case in range(1000):
            limits = rng.choice(limits_choices)
            length = rng.randrange(0, 501)
            text = "".join(rng.choice(alphabet) for _ in range(length))
            kwargs = {
                "incoming": rng.choice(incoming_choices),
                "max_messages": rng.choice((1, 2, 3)),
                "short_limit": limits[0], "medium_limit": limits[1], "long_limit": limits[2],
                "weights": (rng.randrange(1, 101), rng.randrange(101), rng.randrange(101)),
                "draw": rng.choice((None, 0, 0.25, 0.5, 0.99)),
                "continuation_hint": rng.choice((_DEFAULT_HINT, "", "继续?")),
            }
            should_fail = case % 25 in (0, 1)
            if case % 25 == 0:
                text = " " * 60 + "x"
            elif case % 25 == 1:
                text = "x" + " " * 119 + "y"
            elif case % 25 == 2:
                text = rng.choice((" ", "\u3000", "\r\n")) * length
            with self.subTest(case=case, length=len(text), budget=kwargs["max_messages"]):
                if should_fail:
                    with self.assertRaises(ValueError):
                        DELIVERY.plan_delivery(text, **kwargs)
                else:
                    result = self.check(text, **kwargs)
                    if case % 32 == 0:
                        self.assertEqual(result, self.check(text, **kwargs))


if __name__ == "__main__":
    unittest.main()
