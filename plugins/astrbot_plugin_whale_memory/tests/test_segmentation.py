"""Offline stdlib contract tests for the standalone segmentation module.

Run from qq-deepseek-deploy with PYTHONDONTWRITEBYTECODE=1:
    python -m unittest discover -s plugins/astrbot_plugin_whale_memory/tests \
        -p test_segmentation.py -v

Load the file directly: importing the plugin package would initialize AstrBot.
Boundaries are internal character offsets, excluding 0 and len(text).  Regular
plans retain 1/2/3 candidate metadata; infeasible candidates must have zero
completeness and score, and must never be selected.  Scores/scene coefficients
are intentionally not pinned to implementation-specific tuning constants.
No fixture files, network connections, third-party packages, or timing limits.
"""

import sys

# Also set the environment flag when invoking discovery, so this test file's
# own import cannot create a cache before executing this assignment.
sys.dont_write_bytecode = True

import ast
import builtins
from contextlib import ExitStack
import importlib.util
import math
from pathlib import Path
import random
import socket
import unittest
from unittest import mock


_MODULE_PATH = Path(__file__).resolve().parents[1] / "segmentation.py"
_SCENES = frozenset({"casual", "playful", "comfort", "explanation"})
_DEFAULT_WEIGHTS = (0.6, 0.3, 0.1)
_REQUIRED_RESULT_KEYS = {
    "segments", "scene", "chosen_count", "source_chars", "mode", "weights",
    "candidates", "exception",
}
_REQUIRED_CANDIDATE_KEYS = {
    "count", "prior", "scene_fit", "completeness", "naturalness", "score",
    "boundaries",
}
_IMPORTED_ROOTS = set()


def _offline_guard(stack):
    """Fail locally before any accidental socket or DNS operation can occur."""
    failure = AssertionError("The segmentation module must not access a network")
    for attribute in ("connect", "connect_ex", "sendto"):
        stack.enter_context(mock.patch.object(socket.socket, attribute, side_effect=failure))
    for attribute in ("create_connection", "getaddrinfo"):
        stack.enter_context(mock.patch.object(socket, attribute, side_effect=failure))


def _load_standalone_module():
    original_import = builtins.__import__

    def stdlib_only_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            package = (globals or {}).get("__package__", "")
            root = package.split(".", 1)[0]
        else:
            root = name.split(".", 1)[0]
        _IMPORTED_ROOTS.add(root)
        if root not in sys.stdlib_module_names and root != "builtins":
            raise AssertionError(
                "Standalone segmentation may import only stdlib, not {!r}".format(name)
            )
        return original_import(name, globals, locals, fromlist, level)

    spec = importlib.util.spec_from_file_location(
        "_whale_segmentation_unittest_target", _MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load standalone module: {}".format(_MODULE_PATH))
    module = importlib.util.module_from_spec(spec)
    # dataclasses and similar stdlib helpers look up their module by name.
    sys.modules[spec.name] = module
    try:
        with ExitStack() as stack:
            _offline_guard(stack)
            stack.enter_context(mock.patch.object(builtins, "__import__", stdlib_only_import))
            spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


SEGMENTATION = _load_standalone_module()


def _normalized(weights):
    """Independent overflow-safe reference normalization for valid fixtures."""
    largest = max(weights)
    scaled = tuple(value / largest for value in weights)
    total = math.fsum(scaled)
    return tuple(value / total for value in scaled)


class SegmentationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        stack = ExitStack()
        cls.addClassCleanup(stack.close)
        _offline_guard(stack)

    def assert_finite_nonnegative(self, value, label):
        self.assertIsInstance(value, (int, float), label)
        self.assertNotIsInstance(value, bool, label)
        self.assertTrue(math.isfinite(value), label)
        self.assertGreaterEqual(value, 0, label)

    def assert_candidate(self, candidate, text, max_chars):
        self.assertIsInstance(candidate, dict)
        self.assertTrue(_REQUIRED_CANDIDATE_KEYS.issubset(candidate), candidate)
        count = candidate["count"]
        self.assertIsInstance(count, int)
        self.assertNotIsInstance(count, bool)
        self.assertGreaterEqual(count, 1)
        for key in ("prior", "scene_fit", "completeness", "naturalness", "score"):
            self.assert_finite_nonnegative(candidate[key], "candidate." + key)
        product = (
            candidate["prior"] * candidate["scene_fit"]
            * candidate["completeness"] * candidate["naturalness"]
        )
        self.assertTrue(
            math.isclose(candidate["score"], product, rel_tol=1e-12, abs_tol=1e-14),
            "score must be prior * scene_fit * completeness * naturalness",
        )
        if "feasible" in candidate:
            self.assertIsInstance(candidate["feasible"], bool)
            feasible = candidate["feasible"]
        else:
            feasible = not (
                candidate["completeness"] == 0 and candidate["score"] == 0
            )

        cuts = candidate["boundaries"]
        self.assertIsInstance(cuts, (list, tuple))
        previous = 0
        for cut in cuts:
            self.assertIsInstance(cut, int)
            self.assertNotIsInstance(cut, bool)
            self.assertGreater(cut, previous)
            self.assertLess(cut, len(text))
            previous = cut

        if not feasible:
            # These are diagnostic 1/2/3 metadata, not emitted illegal chunks.
            self.assertEqual(candidate["completeness"], 0)
            self.assertEqual(candidate["score"], 0)
            return False, None

        self.assertLessEqual(count, len(text))
        self.assertGreaterEqual(count * max_chars, len(text))
        self.assertEqual(len(cuts), count - 1)
        ends = [0, *cuts, len(text)]
        parts = [text[left:right] for left, right in zip(ends, ends[1:])]
        self.assertEqual(len(parts), count)
        self.assertEqual("".join(parts), text)
        for part in parts:
            self.assertGreaterEqual(len(part), 1)
            self.assertLessEqual(len(part), max_chars)
        return True, parts

    def assert_plan(self, result, text, *, incoming="", scene=None,
                    max_chars=20, weights=_DEFAULT_WEIGHTS, draw=None):
        self.assertIsInstance(result, dict)
        self.assertTrue(_REQUIRED_RESULT_KEYS.issubset(result), result)
        self.assertIsInstance(result["segments"], list)
        self.assertIsInstance(result["source_chars"], int)
        self.assertNotIsInstance(result["source_chars"], bool)
        self.assertEqual(result["source_chars"], len(text))
        self.assertIsInstance(result["chosen_count"], int)
        self.assertNotIsInstance(result["chosen_count"], bool)
        self.assertEqual(result["chosen_count"], len(result["segments"]))
        self.assertIn(result["scene"], _SCENES)
        expected_scene = (
            scene if scene is not None else SEGMENTATION.classify_scene(incoming, text)
        )
        self.assertEqual(result["scene"], expected_scene)
        self.assertIn(result["mode"], {"scored", "long", "empty"})

        actual_weights = result["weights"]
        self.assertIsInstance(actual_weights, (list, tuple))
        self.assertEqual(len(actual_weights), 3)
        for actual, expected in zip(actual_weights, _normalized(weights)):
            self.assert_finite_nonnegative(actual, "normalized weight")
            self.assertAlmostEqual(actual, expected, places=12)
        self.assertAlmostEqual(math.fsum(actual_weights), 1.0, places=12)

        segments = result["segments"]
        if not segments:
            self.assertTrue(text == "" or text.isspace())
            self.assertEqual(result["chosen_count"], 0)
            self.assertEqual(result["mode"], "empty")
        else:
            self.assertNotEqual(result["mode"], "empty")
            self.assertEqual("".join(segments), text, "No trimming, rewriting, or lost tail")
            self.assertGreaterEqual(len(segments), (len(text) + max_chars - 1) // max_chars)
            for part in segments:
                self.assertIsInstance(part, str)
                self.assertGreaterEqual(len(part), 1)
                self.assertLessEqual(len(part), max_chars)

        self.assertIsInstance(result["candidates"], (list, tuple))
        by_count = {}
        candidate_parts = {}
        for candidate in result["candidates"]:
            feasible, parts = self.assert_candidate(candidate, text, max_chars)
            count = candidate["count"]
            self.assertNotIn(count, by_count, "Candidate counts must be unique")
            by_count[count] = candidate
            if count in (1, 2, 3):
                self.assertAlmostEqual(candidate["prior"], actual_weights[count - 1], places=12)
            if feasible:
                candidate_parts[count] = parts

        if result["mode"] == "scored":
            self.assertLessEqual(len(text), 3 * max_chars)
            self.assertEqual(set(by_count), {1, 2, 3})
            self.assertIn(result["chosen_count"], candidate_parts,
                          "An infeasible candidate cannot be selected")
            self.assertEqual(candidate_parts[result["chosen_count"]], segments)
            if draw is None:
                winner = by_count[result["chosen_count"]]["score"]
                maximum = max(candidate["score"] for candidate in by_count.values())
                self.assertTrue(math.isclose(winner, maximum, rel_tol=1e-12, abs_tol=1e-14),
                                "Default selection must maximize score, not sample priors")
        elif segments and result["chosen_count"] in candidate_parts:
            self.assertEqual(candidate_parts[result["chosen_count"]], segments)
        return result

    def check_plan(self, text, **kwargs):
        return self.assert_plan(SEGMENTATION.plan_segments(text, **kwargs), text, **kwargs)

    def test_public_functions_and_stdlib_only_standalone_import(self):
        self.assertTrue(callable(SEGMENTATION.classify_scene))
        self.assertTrue(callable(SEGMENTATION.plan_segments))
        self.assertNotIn("astrbot", _IMPORTED_ROOTS)
        self.assertTrue(_IMPORTED_ROOTS.issubset(sys.stdlib_module_names | {"builtins"}))
        # Include imports inside function bodies, not only imports exercised
        # during module initialization; a lazy AstrBot import is still forbidden.
        tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "Standalone module cannot need its plugin package")
                roots = [(node.module or "").split(".", 1)[0]]
            else:
                continue
            for root in roots:
                self.assertIn(root, sys.stdlib_module_names | {"builtins"})

    def test_classify_obvious_scenes_without_a_model(self):
        examples = (
            ("早上好呀，今天过得怎么样？", "早上好，我刚喝了杯茶。", "casual"),
            ("哈哈哈，逗你玩呢！", "哼，逗你的啦，哈哈哈！", "playful"),
            ("我好难过，刚失恋了，想哭。", "别难过，我会陪着你，慢慢来。", "comfort"),
            ("请解释二分查找的原理和步骤。", "二分查找每次将有序区间折半。", "explanation"),
        )
        for incoming, reply, expected in examples:
            with self.subTest(scene=expected):
                self.assertEqual(SEGMENTATION.classify_scene(incoming, reply), expected)
                self.check_plan(reply, incoming=incoming)
        self.assertEqual(SEGMENTATION.classify_scene("", ""), "casual")

    def test_explicit_scene_is_respected_without_forcing_a_count(self):
        for scene in sorted(_SCENES):
            with self.subTest(scene=scene):
                result = self.check_plan("我在。", incoming="请详细解释一下。", scene=scene)
                self.assertEqual(result["scene"], scene)
                self.assertEqual(result["chosen_count"], 1)

    def test_comfort_can_be_one_natural_segment(self):
        reply = "我在，慢慢说。"
        result = self.check_plan(reply, incoming="我今天很难过，想哭。")
        self.assertEqual(result["scene"], "comfort")
        self.assertEqual(result["segments"], [reply])
        self.assertEqual(result["chosen_count"], 1)

    def test_default_candidate_priors_are_60_30_10_and_scores_are_products(self):
        text = "风来了呀。花也开了。一起散步吧。"
        self.assertLessEqual(len(text), 20)
        result = self.check_plan(text, scene="casual")
        by_count = {candidate["count"]: candidate for candidate in result["candidates"]}
        self.assertEqual(set(by_count), {1, 2, 3})
        for count, expected in enumerate(_DEFAULT_WEIGHTS, 1):
            self.assertAlmostEqual(by_count[count]["prior"], expected, places=12)
            self.assertGreater(by_count[count]["completeness"], 0,
                               "Three natural clauses offer feasible 1/2/3 alternatives")

    def test_non_default_weights_are_normalized_without_overflow(self):
        fixtures = (
            (60, 30, 10),
            [6.0, 3.0, 1.0],
            (1, 0, 0),
            (0, 1, 0),
            (0, 0, 1),
            (0, 4, 1),
            (1e308, 1e308, 1e308),
            (1e308, 5e307, 2.5e307),
            (5e-324, 5e-324, 5e-324),
            (5e-324, 0.0, 0.0),
        )
        for weights in fixtures:
            with self.subTest(weights=weights):
                self.check_plan("风来了呀。花也开了。一起散步吧。", scene="casual", weights=weights)

    def test_short_daily_replies_prefer_one_segment(self):
        replies = (
            "嗯", "好呀", "我在。", "晚安，做个好梦。", "刚喝完水，等你呀。",
            "吃过啦，今天很开心。", "今天我们一起去河边慢慢散步吧",
            "今天我们一起去河边慢慢散步看花等风吹来呀",
        )
        for text in replies:
            with self.subTest(text=text):
                self.assertLessEqual(len(text), 20)
                result = self.check_plan(text, incoming="在干嘛呀？", scene="casual")
                self.assertEqual(result["segments"], [text])

    def test_small_replies_do_not_become_meaningless_fragments(self):
        for text in ("好", "好呀", "谢谢你", "明天见呀", "OK!", "🍀抱抱你呀", "（晚点再说）"):
            with self.subTest(text=text):
                self.assertLessEqual(len(text), 20)
                self.assertEqual(self.check_plan(text, scene="casual")["segments"], [text])

    def test_21_40_41_60_61_character_capacity_boundaries(self):
        for length in (20, 21, 40, 41, 60, 61):
            with self.subTest(length=length):
                text = "春" * length
                result = self.check_plan(text, scene="casual")
                minimum = (length + 19) // 20
                self.assertGreaterEqual(result["chosen_count"], minimum)
                if length <= 60:
                    self.assertEqual(result["mode"], "scored")
                    self.assertLessEqual(result["chosen_count"], 3)
                    for candidate in result["candidates"]:
                        if candidate["count"] < minimum:
                            self.assertEqual(candidate["completeness"], 0)
                            self.assertEqual(candidate["score"], 0)
                else:
                    self.assertEqual(result["mode"], "long")
                    self.assertGreater(result["chosen_count"], 3)
                if length in (41, 60):
                    self.assertEqual(result["chosen_count"], 3)
                if length == 60:
                    self.assertEqual([len(part) for part in result["segments"]], [20, 20, 20])

    def test_5000_chinese_characters_keep_the_entire_tail(self):
        text = ("春风拂过花海" * 1000)[:4996] + "末尾保全"
        self.assertEqual(len(text), 5000)
        result = self.check_plan(text, incoming="请解释这段内容。", scene="explanation")
        self.assertEqual(result["mode"], "long")
        self.assertGreaterEqual(result["chosen_count"], 250)
        self.assertEqual("".join(result["segments"])[-4:], "末尾保全")

    def test_punctuation_newlines_indentation_and_spaces_are_exact(self):
        fixtures = (
            "  春风来了，花开了！  ",
            "第一句。\n第二句？\n第三句！\n",
            "  一、准备材料。\n    二、慢慢搅拌。\n\t三、等待完成。\n",
            "\r\n    第一步：保留 CRLF。\r\n\t第二步：保留 tab。\r\n",
            "甲  乙   丙。\n\n  丁\t戊；己……庚——辛。  ",
            "\u3000全角空格\u00a0不换行空格\t制表符\n换行。",
            "  " + "春风。" * 30 + "\n    末尾还在。  ",
        )
        for text in fixtures:
            with self.subTest(text=repr(text)):
                self.assertEqual("".join(self.check_plan(text)["segments"]), text)

    def test_empty_and_whitespace_only_inputs_have_consistent_metadata(self):
        for text in ("", " ", "\t\n\r", "\u3000\u00a0", " " * 81):
            with self.subTest(text=repr(text)):
                result = self.check_plan(text)
                if text == "":
                    self.assertEqual(result["segments"], [])
                elif result["segments"]:
                    self.assertEqual("".join(result["segments"]), text)

    def test_long_whitespace_runs_do_not_destroy_nonblank_text(self):
        fixtures = (
            "开头" + " " * 80 + "末尾",
            " " * 65 + "末尾",
            "开头" + "\n\t " * 25,
        )
        for text in fixtures:
            with self.subTest(text=repr(text)):
                result = self.check_plan(text)
                self.assertEqual("".join(result["segments"]), text)
                # Empty strings are never allowed. Whitespace chunks can be
                # unavoidable under the hard 20-character cap and stay intact.
                self.assertTrue(all(result["segments"]))
                if any(not part.strip() for part in result["segments"]):
                    self.assertTrue(result["exception"])
                    self.assertIn("whitespace_segments", result)
                    self.assertTrue(result["whitespace_segments"])

    def test_ascii_emoji_brackets_and_long_url_preserve_every_character(self):
        fixtures = (
            "ASCII words,  two spaces; quotes='x' and \"y\".",
            "😀🧠👩\u200d🚀👨\u200d👩\u200d👧\u200d👦❤️你好！🙂" * 5,
            "（说明：[保留这些括号]{以及内容}）\n(second line [x] {y})",
            "访问 https://example.invalid/" + "very-long-path/" * 12 + "?q=a%20b&x=1#tail",
            "A" * 79 + "Z",
            "编号 0123456789，符号 <> [] {} () / \\ % & + = 都要留下。",
        )
        for text in fixtures:
            with self.subTest(text=repr(text)):
                result = self.check_plan(text)
                self.assertEqual("".join(result["segments"]), text)
                self.assertEqual(result["segments"][-1][-1], text[-1])

    def test_three_segments_are_naturally_feasible_and_not_empty(self):
        fixtures = (
            (("先把清水慢慢倒进杯中。", "接着加入茶叶等待片刻。", "最后轻轻搅拌再尝一口。"),
             "explanation"),
            (("风来了呀。", "花也开了。", "一起散步吧。"), "playful"),
        )
        for clauses, scene in fixtures:
            with self.subTest(scene=scene, clauses=clauses):
                text = "".join(clauses)
                self.assertTrue(all(1 <= len(clause) <= 20 for clause in clauses))
                result = self.check_plan(text, scene=scene, weights=(0, 0, 1))
                self.assertEqual(result["chosen_count"], 3)
                self.assertEqual(len(result["segments"]), 3)
                self.assertTrue(all(part.strip() for part in result["segments"]))
                # There is an explicit punctuation boundary after each complete clause.
                self.assertEqual(result["segments"], list(clauses))

    def test_zero_prior_for_feasible_counts_cannot_select_infeasible_metadata(self):
        for text, weights in (
            ("春" * 21, (1, 0, 0)),
            ("春" * 41, (1, 0, 0)),
            ("春" * 60, (0, 1, 0)),
            ("好", (0, 0, 1)),
        ):
            for draw in (None, 0, 0.5, math.nextafter(1.0, 0.0)):
                with self.subTest(length=len(text), weights=weights, draw=draw):
                    result = self.check_plan(text, weights=weights, draw=draw)
                    self.assertEqual("".join(result["segments"]), text)
                    if len(text) > 40:
                        self.assertEqual(result["chosen_count"], 3)

    def test_requested_smaller_character_caps_are_honored(self):
        text = "  春风来了。\n    花也开了。末尾保全。"
        for cap in (1, 2, 7, 12, 19, 20):
            with self.subTest(max_chars=cap):
                self.check_plan(text, max_chars=cap)

    def test_default_is_repeatable_and_always_chooses_a_maximum_score(self):
        fixtures = (
            ("你好呀。", "casual"),
            ("花开了呀。风也很温柔。", "playful"),
            ("别着急。我们先深呼吸。你可以慢慢说。", "comfort"),
            ("先准备材料。再分次加入。最后检查结果。", "explanation"),
            ("甲" * 21, "casual"),
            ("甲" * 41, "comfort"),
        )
        for text, scene in fixtures:
            with self.subTest(text=text, scene=scene):
                baseline = self.check_plan(text, scene=scene)
                self.assertEqual(baseline, self.check_plan(text, scene=scene, draw=None))
                for _ in range(8):
                    self.assertEqual(baseline, self.check_plan(text, scene=scene))

    def _natural_two_candidate_qualities(self):
        text = "花开了呀。风也很温柔。"
        result = self.check_plan(text, scene="casual")
        by_count = {candidate["count"]: candidate for candidate in result["candidates"]}
        qualities = {}
        for count in (1, 2):
            candidate = by_count[count]
            qualities[count] = (
                candidate["scene_fit"] * candidate["completeness"] * candidate["naturalness"]
            )
            self.assertGreater(qualities[count], 0,
                               "A natural two-clause text supports both candidates")
        return text, qualities

    def test_fixed_draw_is_repeatable_and_only_changes_near_tied_candidates(self):
        text, qualities = self._natural_two_candidate_qualities()
        # Opposite qualities equalize scores without assuming any fixed tuning.
        weights = (qualities[2], qualities[1], 0)
        baseline = self.check_plan(text, scene="casual", weights=weights)
        by_count = {candidate["count"]: candidate for candidate in baseline["candidates"]}
        self.assertAlmostEqual(by_count[1]["score"], by_count[2]["score"], places=12)
        self.assertGreater(by_count[1]["score"], by_count[3]["score"])
        chosen = set()
        for draw in (0, 0.1, 0.25, 0.5, 0.75, 0.9, math.nextafter(1.0, 0.0)):
            with self.subTest(draw=draw):
                result = self.check_plan(text, scene="casual", weights=weights, draw=draw)
                chosen.add(result["chosen_count"])
                self.assertIn(result["chosen_count"], {1, 2})
                self.assertEqual(result, self.check_plan(text, scene="casual", weights=weights,
                                                        draw=draw))
        self.assertEqual(chosen, {1, 2}, "draw must provide small variability at a score tie")

    def test_draw_does_not_turn_a_large_score_gap_into_weighted_sampling(self):
        text, qualities = self._natural_two_candidate_qualities()
        weights = (32 * qualities[2], qualities[1], 0)
        baseline = self.check_plan(text, scene="casual", weights=weights)
        by_count = {candidate["count"]: candidate for candidate in baseline["candidates"]}
        self.assertGreater(by_count[1]["score"], 10 * by_count[2]["score"])
        self.assertEqual(baseline["chosen_count"], 1)
        for draw in (0, 0.01, 0.25, 0.5, 0.75, 0.99, math.nextafter(1.0, 0.0)):
            with self.subTest(draw=draw):
                self.assertEqual(self.check_plan(text, scene="casual", weights=weights,
                                                draw=draw)["chosen_count"], 1)
        for weights in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            baseline = self.check_plan("风来了呀。花也开了。一起散步吧。", weights=weights)
            for draw in (0, 0.5, math.nextafter(1.0, 0.0)):
                result = self.check_plan("风来了呀。花也开了。一起散步吧。",
                                         weights=weights, draw=draw)
                self.assertEqual(result["chosen_count"], baseline["chosen_count"])
                self.assertEqual(result["segments"], baseline["segments"])

    def test_invalid_text_and_incoming_types_raise(self):
        for invalid in (None, True, False, 0, 1.5, b"text", [], {}, object()):
            with self.subTest(argument="text", value=repr(invalid)):
                with self.assertRaises((ValueError, TypeError)):
                    SEGMENTATION.plan_segments(invalid)
            with self.subTest(argument="incoming", value=repr(invalid)):
                with self.assertRaises((ValueError, TypeError)):
                    SEGMENTATION.plan_segments("你好", incoming=invalid)
            with self.subTest(argument="classify.incoming", value=repr(invalid)):
                with self.assertRaises((ValueError, TypeError)):
                    SEGMENTATION.classify_scene(invalid, "你好")
            with self.subTest(argument="classify.reply", value=repr(invalid)):
                with self.assertRaises((ValueError, TypeError)):
                    SEGMENTATION.classify_scene("你好", invalid)

    def test_invalid_scene_values_raise(self):
        for invalid in ("", "unknown", "CASUAL", "sad", True, False, 0, 1.5, [], {}, b"casual"):
            with self.subTest(scene=repr(invalid)):
                with self.assertRaises((ValueError, TypeError)):
                    SEGMENTATION.plan_segments("你好", scene=invalid)

    def test_invalid_character_caps_raise_including_bools_floats_and_over_20(self):
        invalid_caps = (None, True, False, 0, -1, 21, 1000, 20.0, 2.5, "20",
                        float("nan"), float("inf"), -float("inf"), [], {})
        for invalid in invalid_caps:
            for text in ("你好", ""):
                with self.subTest(max_chars=repr(invalid), text=text):
                    with self.assertRaises((ValueError, TypeError)):
                        SEGMENTATION.plan_segments(text, max_chars=invalid)

    def test_invalid_weights_raise_instead_of_silently_clamping(self):
        invalid_weights = [None, True, False, (), (1,), (1, 2), (1, 2, 3, 4),
                           "abc", (0, 0, 0), (-0.0, 0.0, 0.0)]
        for position in range(3):
            for invalid in (True, False, -0.1, float("nan"), float("inf"),
                            -float("inf"), "0.6", None, complex(0.6, 0), [], object()):
                values = [0.6, 0.3, 0.1]
                values[position] = invalid
                invalid_weights.append(tuple(values))
        for weights in invalid_weights:
            for text in ("你好", ""):
                with self.subTest(weights=repr(weights), text=text):
                    with self.assertRaises((ValueError, TypeError)):
                        SEGMENTATION.plan_segments(text, weights=weights)

    def test_invalid_draw_raises_for_all_modes_and_zero_is_valid(self):
        invalid_draws = (True, False, -0.001, 1, 1.001, float("nan"), float("inf"),
                         -float("inf"), "0.5", [], {}, lambda: 0.5, complex(0.5, 0))
        for draw in invalid_draws:
            for text in ("", "你好", "甲" * 61):
                with self.subTest(draw=repr(draw), length=len(text)):
                    with self.assertRaises((ValueError, TypeError)):
                        SEGMENTATION.plan_segments(text, draw=draw)
        for text in ("", "你好", "甲" * 61):
            self.check_plan(text, draw=0)
            self.check_plan(text, draw=math.nextafter(1.0, 0.0))

    def test_bounded_stdlib_random_fuzz_preserves_text_without_timing_assumptions(self):
        rng = random.Random(0x5E6_2026)
        alphabet = list("春风花海你好甲乙丙丁abcdefXYZ0123456789，。！？；：()[]{}<>/%&=+-_")
        alphabet += [" ", "\t", "\n", "\r", "\u3000", "\u00a0", "😀", "🧠", "👩", "🚀",
                     "\u200d", "\ufe0f"]
        incoming_choices = ("", "今天好吗？", "哈哈哈，逗你玩。", "我很难过。", "请解释原理。")
        scene_choices = (None, "casual", "playful", "comfort", "explanation")
        # Fixed bounds and a large deterministic corpus exercise work scaling;
        # no scheduler-dependent wall-clock threshold can make this flaky.
        for case in range(1250):
            length = rng.randrange(0, 181)
            text = "".join(rng.choice(alphabet) for _ in range(length))
            kwargs = {
                "incoming": rng.choice(incoming_choices),
                "scene": rng.choice(scene_choices),
                "max_chars": rng.choice((7, 12, 19, 20)),
            }
            if case % 5 == 0:
                kwargs["weights"] = (rng.randrange(1, 101), rng.randrange(101), rng.randrange(101))
            with self.subTest(case=case, length=length, cap=kwargs["max_chars"]):
                result = self.check_plan(text, **kwargs)
                if case % 32 == 0:
                    self.assertEqual(result, self.check_plan(text, **kwargs))
                if result["mode"] == "scored":
                    self.assertEqual(len(result["candidates"]), 3)
                self.assertLessEqual(result["chosen_count"], len(text))


if __name__ == "__main__":
    unittest.main()
