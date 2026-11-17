"""AffinityEngine 好感度引擎单元测试。"""

import pytest

from memory.affinity import AffinityEngine


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
def engine():
    """默认配置的好感度引擎。"""
    return AffinityEngine(DEFAULT_CONFIG)


def test_get_stage_stranger(engine):
    """0-24 -> stranger。"""
    assert engine.get_stage(0) == "stranger"
    assert engine.get_stage(10) == "stranger"
    assert engine.get_stage(24) == "stranger"


def test_get_stage_acquaintance(engine):
    """25-49 -> acquaintance。"""
    assert engine.get_stage(25) == "acquaintance"
    assert engine.get_stage(30) == "acquaintance"
    assert engine.get_stage(49) == "acquaintance"


def test_get_stage_close(engine):
    """50-74 -> close。"""
    assert engine.get_stage(50) == "close"
    assert engine.get_stage(60) == "close"
    assert engine.get_stage(74) == "close"


def test_get_stage_intimate(engine):
    """75-100 -> intimate。"""
    assert engine.get_stage(75) == "intimate"
    assert engine.get_stage(90) == "intimate"
    assert engine.get_stage(100) == "intimate"


def test_get_stage_label(engine):
    """中文标签正确映射。"""
    assert engine.get_stage_label("stranger") == "陌生"
    assert engine.get_stage_label("acquaintance") == "熟悉"
    assert engine.get_stage_label("close") == "亲近"
    assert engine.get_stage_label("intimate") == "挚友"


def test_get_stage_label_unknown(engine):
    """未知阶段返回默认值。"""
    assert engine.get_stage_label("unknown") == "陌生"


def test_analyze_sentiment_positive(engine):
    """谢谢你，太棒了 -> 正数。"""
    score = engine.analyze_sentiment("谢谢你，太棒了")
    assert score > 0


def test_analyze_sentiment_negative(engine):
    """你真笨 -> 负数。"""
    score = engine.analyze_sentiment("你真笨")
    assert score < 0


def test_analyze_sentiment_neutral(engine):
    """今天天气如何 -> 0。"""
    score = engine.analyze_sentiment("今天天气如何")
    assert score == 0


def test_analyze_sentiment_empty(engine):
    """空文本 -> 0。"""
    assert engine.analyze_sentiment("") == 0


def test_analyze_sentiment_english_positive(engine):
    """英文正向情感。"""
    score = engine.analyze_sentiment("thank you, you are great")
    assert score > 0


def test_analyze_sentiment_english_negative(engine):
    """英文负向情感。"""
    score = engine.analyze_sentiment("you are so stupid")
    assert score < 0


def test_update_affinity_positive(engine):
    """正向交互好感度提升。"""
    new_affinity = engine.update_affinity(50, "谢谢你")
    assert new_affinity > 50


def test_update_affinity_negative(engine):
    """负向交互好感度下降。"""
    new_affinity = engine.update_affinity(50, "你真笨")
    assert new_affinity < 50


def test_update_affinity_neutral(engine):
    """中性交互好感度不变。"""
    new_affinity = engine.update_affinity(50, "今天天气如何")
    assert new_affinity == 50


def test_update_affinity_clamp_high(engine):
    """好感度不超过 100。"""
    # 从 99 开始，正向情感足够强
    new_affinity = engine.update_affinity(99, "谢谢你太棒了爱你")
    assert new_affinity == 100


def test_update_affinity_clamp_low(engine):
    """好感度不低于 0。"""
    new_affinity = engine.update_affinity(2, "你真笨太讨厌了")
    assert new_affinity == 0


def test_update_affinity_disabled():
    """enabled=False 时不变。"""
    config = dict(DEFAULT_CONFIG)
    config["affinity_enabled"] = False
    engine = AffinityEngine(config)
    new_affinity = engine.update_affinity(50, "谢谢你太棒了")
    assert new_affinity == 50


def test_get_stage_prompt(engine):
    """各阶段返回非空提示。"""
    for stage in ["stranger", "acquaintance", "close", "intimate"]:
        prompt = engine.get_stage_prompt(stage)
        assert isinstance(prompt, str)
        assert len(prompt) > 0


def test_get_stage_prompt_unknown(engine):
    """未知阶段返回空串。"""
    assert engine.get_stage_prompt("unknown") == ""


def test_format_affinity_info(engine):
    """格式化好感度信息包含关键字段。"""
    text = engine.format_affinity_info(60, 100)
    assert "60" in text
    assert "100" in text
    assert "亲近" in text  # 60 对应 close 阶段


def test_update_affinity_multiple_positive(engine):
    """多个正向关键词累计加成。"""
    # "谢谢" + "棒" = 2 个正向命中
    new_affinity = engine.update_affinity(50, "谢谢你太棒了")
    # sentiment=2, gain=2, min(2,3)=2 -> +4
    assert new_affinity == 54
