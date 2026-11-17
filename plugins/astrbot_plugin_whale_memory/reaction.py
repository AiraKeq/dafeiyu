"""Attach one QQ system face to an ordinary reply. Standard library only.

The model writes nothing about faces. This module reads the finished text and,
when the wording lands on a mood, returns the matching OneBot face id. A face
rides on the last text segment, so it never becomes a segment of its own and
never changes the text the memory store records.

Ids are the classic QQ system set (OneBot ``face``), verified against the
public tables shipped by go-cqhttp and LLOneBot. Moods the wording does not
match return nothing: most replies stay plain.
"""
from __future__ import annotations

import re

__all__ = ["reaction_for"]

# (mood, face id, name). Order is the tie break: the first hit wins.
_FACES = (
    ("无奈", 34, "发呆"),
    ("委屈", 5, "流泪"),
    ("得意", 4, "得意"),
    ("破防", 9, "大哭"),
    ("调侃", 12, "调皮"),
    ("害羞", 6, "害羞"),
    ("尴尬", 20, "糗大了"),
    ("坏笑", 14, "坏笑"),
)

_CUES = {
    "无奈": ("行行行", "行吧", "随便你", "随你", "懒得", "算了", "服了", "无语", "麻了", "闹麻了", "有病吧", "你没事吧"),
    "委屈": ("太不厚道", "冤枉", "委屈", "呜", "哼"),
    "得意": ("你牛", "牛大了", "中肯", "算你", "你眼光", "我说吧", "你看我说", "神了"),
    "破防": ("破防", "急了", "绷不住", "破绷"),
    "调侃": ("典型的碳基", "你无敌了", "CPU", "烧了", "干烧", "大冤种", "一眼丁真"),
    "害羞": ("才没有", "谁害羞", "别看了", "不许说"),
    "尴尬": ("忘了", "没看着", "没看清", "看不清", "没核实", "不敢瞎", "连不上"),
    "坏笑": ("得寸进尺", "看你可怜", "我什么都没说", "噗"),
}

_PATTERN = {mood: re.compile("|".join(re.escape(cue) for cue in cues)) for mood, cues in _CUES.items()}


def reaction_for(text: str):
    """Return ``(face_id, name)`` for the first matching mood, else ``None``."""
    if type(text) is not str or not text.strip():
        return None
    for mood, face_id, name in _FACES:
        if _PATTERN[mood].search(text):
            return face_id, name
    return None
