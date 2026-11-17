"""Offline tests for the QQ face picker. Loads the file directly, no AstrBot."""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True


def load():
    path = Path(__file__).resolve().parents[1] / "reaction.py"
    spec = importlib.util.spec_from_file_location("whale_reaction", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reaction = load()


class ReactionTests(unittest.TestCase):
    def test_each_mood_maps_to_its_face(self):
        samples = {
            "行行行，算你厉害": (34, "发呆"),
            "主人翻我日记本还笑出声 太不厚道了": (5, "流泪"),
            "算你有眼光": (4, "得意"),
            "急了？破防了？": (9, "大哭"),
            "这算盘打得我CPU干烧了": (12, "调皮"),
            "才没有想你": (6, "害羞"),
            "这梗没核实清楚，先不瞎编": (20, "糗大了"),
            "主人别得寸进尺啊": (14, "坏笑"),
        }
        for text, expected in samples.items():
            with self.subTest(text=text):
                self.assertEqual(reaction.reaction_for(text), expected)

    def test_plain_chat_stays_plain(self):
        for text in ("", "   ", "两万六买台带水冷的本", "这变色小人转一圈", "不会就对了"):
            with self.subTest(text=text):
                self.assertIsNone(reaction.reaction_for(text))

    def test_non_text_is_ignored(self):
        for value in (None, 5, ["行吧"], {"text": "行吧"}):
            self.assertIsNone(reaction.reaction_for(value))

    def test_first_mood_wins_when_several_match(self):
        # 无奈排在委屈前面，所以「行吧」压过「呜」。
        self.assertEqual(reaction.reaction_for("行吧……呜"), (34, "发呆"))

    def test_ids_are_the_classic_system_set(self):
        ids = {reaction.reaction_for(cue)[0] for cue in ("行吧", "呜", "神了", "破防", "CPU", "才没有", "忘了", "噗")}
        self.assertTrue(ids <= {4, 5, 6, 9, 12, 14, 20, 34})


if __name__ == "__main__":
    unittest.main()
