"""PersonaEnhancer 人格增强引擎单元测试。"""

import pytest

from memory.models import MemoryItem
from memory.persona import PersonaEnhancer


# 默认配置
DEFAULT_CONFIG = {
    "decay_enabled": True,
    "short_decay_rate": 0.15,
    "long_decay_rate": 0.03,
    "forget_threshold": 0.1,
    "decay_check_interval": 3600,
    "recall_top_n": 5,
    "consolidation_recall_threshold": 3,
    "consolidation_strength_threshold": 0.8,
    "short_term_initial_strength": 0.5,
    "long_term_initial_strength": 0.7,
    "recall_boost": 0.15,
    "weak_boost": 0.05,
    "max_short_term": 100,
    "max_long_term": 200,
    "auto_learn_enabled": True,
    "affinity_enabled": True,
    "affinity_default": 50,
    "affinity_positive_gain": 2,
    "affinity_negative_loss": 3,
    "persona_enhance_enabled": True,
}


@pytest.fixture
def enhancer():
    """默认配置的人格增强器。"""
    return PersonaEnhancer(DEFAULT_CONFIG)


def test_summarize_long_term(enhancer):
    """有长期记忆时返回摘要文本。"""
    long_term = [
        MemoryItem.create_long("我喜欢吃苹果", strength=0.7),
        MemoryItem.create_long("我会编程", strength=0.9),
    ]
    summary = enhancer.summarize_long_term_memories(long_term)
    assert summary != ""
    assert "我喜欢吃苹果" in summary
    assert "我会编程" in summary
    # 每条以 "- " 开头
    assert "- 我喜欢吃苹果" in summary


def test_summarize_long_term_sorted_by_strength(enhancer):
    """摘要按强度降序排列。"""
    long_term = [
        MemoryItem.create_long("低强度", strength=0.5),
        MemoryItem.create_long("高强度", strength=0.9),
    ]
    summary = enhancer.summarize_long_term_memories(long_term)
    lines = summary.split("\n")
    # 高强度应排前面
    assert "高强度" in lines[0]
    assert "低强度" in lines[1]


def test_summarize_empty(enhancer):
    """空列表返回空串。"""
    assert enhancer.summarize_long_term_memories([]) == ""


def test_summarize_max_count(enhancer):
    """摘要最多 max_count 条。"""
    long_term = [
        MemoryItem.create_long(f"记忆_{i}", strength=0.5 + i * 0.01)
        for i in range(10)
    ]
    summary = enhancer.summarize_long_term_memories(long_term, max_count=3)
    lines = summary.split("\n")
    assert len(lines) == 3


def test_extract_user_preferences(enhancer):
    """含偏好的记忆能提取。"""
    long_term = [
        MemoryItem.create_long("喜欢吃苹果", source="auto_learn", strength=0.7),
    ]
    prefs = enhancer.extract_user_preferences(long_term)
    assert prefs != ""
    assert "喜欢吃苹果" in prefs


def test_extract_user_preferences_by_tag(enhancer):
    """通过 preference 标签识别偏好。"""
    long_term = [
        MemoryItem.create_long("某内容", tags=["preference"], strength=0.7),
    ]
    prefs = enhancer.extract_user_preferences(long_term)
    assert prefs != ""
    assert "某内容" in prefs


def test_extract_user_preferences_empty(enhancer):
    """无偏好记忆时返回空串。"""
    long_term = [
        MemoryItem.create_long("今天天气不错", source="manual", strength=0.7),
    ]
    assert enhancer.extract_user_preferences(long_term) == ""


def test_extract_user_preferences_empty_list(enhancer):
    """空长期记忆列表返回空串。"""
    assert enhancer.extract_user_preferences([]) == ""


def test_generate_persona_hint(enhancer):
    """生成非空提示。"""
    hint = enhancer.generate_persona_hint(50, [])
    assert hint != ""
    assert "[人格扮演增强]" in hint


def test_generate_persona_hint_with_memories(enhancer):
    """有记忆时提示包含记忆摘要。"""
    long_term = [
        MemoryItem.create_long("我喜欢吃苹果", source="auto_learn", strength=0.7),
    ]
    hint = enhancer.generate_persona_hint(60, long_term)
    assert "我喜欢吃苹果" in hint
    assert "历史记忆摘要" in hint


def test_generate_persona_hint_with_style_hint(enhancer):
    """提示中包含近期风格提示。"""
    hint = enhancer.generate_persona_hint(50, [], recent_style_hint="近期风格：活泼")
    assert "近期风格：活泼" in hint


def test_generate_persona_hint_disabled():
    """enabled=False 时返回空串。"""
    config = dict(DEFAULT_CONFIG)
    config["persona_enhance_enabled"] = False
    enhancer = PersonaEnhancer(config)
    assert enhancer.generate_persona_hint(50, []) == ""


def test_generate_persona_hint_includes_stage(enhancer):
    """提示中包含关系阶段信息。"""
    # 亲密阶段
    hint = enhancer.generate_persona_hint(80, [])
    # intimate 阶段提示包含"挚友"或"亲密"
    assert "挚友" in hint or "亲密" in hint


def test_generate_persona_hint_includes_stage_stranger(enhancer):
    """陌生阶段提示包含阶段信息。"""
    hint = enhancer.generate_persona_hint(10, [])
    assert "陌生" in hint


def test_generate_persona_hint_includes_preferences(enhancer):
    """提示中包含用户偏好部分。"""
    long_term = [
        MemoryItem.create_long("喜欢吃苹果", source="auto_learn", strength=0.7),
    ]
    hint = enhancer.generate_persona_hint(50, long_term)
    assert "已知偏好" in hint
