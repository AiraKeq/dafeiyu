"""AutoLearner 自主学习引擎单元测试。"""

import pytest

from memory.models import MemoryItem
from memory.learner import AutoLearner


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
def learner():
    """默认配置的自动学习器。"""
    return AutoLearner(DEFAULT_CONFIG)


def test_generate_tags(learner):
    """生成标签非空。"""
    tags = learner.generate_tags("我喜欢吃苹果")
    assert len(tags) > 0
    # 应包含中文 2-gram
    assert any(len(t) == 2 for t in tags)


def test_generate_tags_max_five(learner):
    """标签最多 5 个。"""
    # 长文本生成很多候选，但应被截断到 5
    tags = learner.generate_tags("我喜欢吃苹果香蕉橘子葡萄西瓜")
    assert len(tags) <= 5


def test_generate_tags_empty(learner):
    """空内容返回空列表。"""
    assert learner.generate_tags("") == []


def test_generate_tags_english(learner):
    """英文标签长度大于 2。"""
    tags = learner.generate_tags("I love programming")
    # programming 长度 > 2 应被提取
    assert "programming" in tags
    # love 长度 > 2 应被提取
    assert "love" in tags


def test_classify_content_preference(learner):
    """我喜欢编程 -> preference。"""
    assert learner.classify_content("我喜欢编程") == "preference"


def test_classify_content_fact(learner):
    """我叫张三 -> fact。"""
    assert learner.classify_content("我叫张三") == "fact"


def test_classify_content_general(learner):
    """普通文本 -> general。"""
    assert learner.classify_content("这个想法很有意思") == "general"


def test_classify_content_event(learner):
    """事件类描述 -> event。"""
    assert learner.classify_content("昨天去了公园") == "event"


def test_classify_content_empty(learner):
    """空内容 -> general。"""
    assert learner.classify_content("") == "general"


def test_score_importance_range(learner):
    """评分在 [0.3, 0.9] 范围内。"""
    test_cases = [
        ("我喜欢编程", "preference"),
        ("我叫张三", "fact"),
        ("昨天去了公园", "event"),
        ("这个想法很有意思", "general"),
        ("今天天气怎么样？", "general"),
    ]
    for content, category in test_cases:
        score = learner.score_importance(content, category)
        assert 0.3 <= score <= 0.9, f"内容 '{content}' 评分 {score} 超出范围"


def test_extract_memories_self_statement(learner):
    """我喜欢吃苹果 能抽取到记忆。"""
    memories = learner.extract_memories("我喜欢吃苹果")
    assert len(memories) >= 1
    assert memories[0].content == "我喜欢吃苹果"
    assert memories[0].type == "short"
    assert memories[0].source == "auto_learn"


def test_extract_memories_question(learner):
    """纯疑问句不抽取。"""
    memories = learner.extract_memories("今天天气怎么样？")
    assert len(memories) == 0


def test_extract_memories_fact_with_number(learner):
    """含数字+单位的事实陈述能抽取。"""
    memories = learner.extract_memories("我今年25岁")
    assert len(memories) >= 1


def test_extract_memories_empty(learner):
    """空文本不抽取。"""
    assert learner.extract_memories("") == []


def test_extract_memories_multiple_sentences(learner):
    """多句文本抽取多条记忆。"""
    text = "我喜欢吃苹果。我叫小明。我今年20岁。"
    memories = learner.extract_memories(text)
    assert len(memories) >= 2


def test_extract_memories_short_sentence_skipped(learner):
    """过短句子被跳过。"""
    memories = learner.extract_memories("好的")
    assert len(memories) == 0


def test_is_duplicate_similar(learner):
    """相似内容返回已有记忆。"""
    existing = [MemoryItem(content="我喜欢吃苹果")]
    # 新内容包含已有内容（或高相似）
    result = learner.is_duplicate("我喜欢吃苹果呀", existing)
    assert result is not None
    assert result.content == "我喜欢吃苹果"


def test_is_duplicate_exact(learner):
    """完全相同内容判为重复。"""
    existing = [MemoryItem(content="我喜欢吃苹果")]
    result = learner.is_duplicate("我喜欢吃苹果", existing)
    assert result is not None


def test_is_duplicate_different(learner):
    """不同内容返回 None。"""
    existing = [MemoryItem(content="我喜欢吃苹果")]
    result = learner.is_duplicate("今天去爬山了", existing)
    assert result is None


def test_is_duplicate_empty(learner):
    """空内容返回 None。"""
    existing = [MemoryItem(content="测试")]
    assert learner.is_duplicate("", existing) is None


def test_is_duplicate_empty_existing(learner):
    """已有记忆为空返回 None。"""
    assert learner.is_duplicate("测试", []) is None


def test_process_new_memories(learner):
    """新内容产生新记忆。"""
    new_memories, boosted = learner.process("我喜欢吃苹果", "", [])
    assert len(new_memories) == 1
    assert new_memories[0].content == "我喜欢吃苹果"
    assert len(boosted) == 0


def test_process_duplicate(learner):
    """重复内容弱增强已有记忆。"""
    existing = MemoryItem(content="我喜欢吃苹果", strength=0.5)
    new_memories, boosted = learner.process("我喜欢吃苹果", "", [existing])
    # 重复内容不产生新记忆
    assert len(new_memories) == 0
    # 已有记忆被弱增强
    assert len(boosted) == 1
    assert boosted[0] is existing
    assert existing.strength == pytest.approx(0.55)  # 0.5 + 0.05


def test_process_mixed(learner):
    """混合新内容与重复内容。"""
    existing = MemoryItem(content="我喜欢吃苹果", strength=0.5)
    # 一句重复、一句新内容
    new_memories, boosted = learner.process(
        "我喜欢吃苹果。我喜欢看书。", "", [existing]
    )
    # "我喜欢看书" 是新内容
    assert len(new_memories) >= 1
    # "我喜欢吃苹果" 重复，弱增强已有
    assert len(boosted) == 1
    assert boosted[0] is existing


def test_process_disabled():
    """enabled=False 时返回空列表。"""
    config = dict(DEFAULT_CONFIG)
    config["auto_learn_enabled"] = False
    learner = AutoLearner(config)
    new_memories, boosted = learner.process("我喜欢吃苹果", "", [])
    assert new_memories == []
    assert boosted == []


def test_process_duplicate_boosted_once(learner):
    """同一已有记忆最多弱增强一次。"""
    existing = MemoryItem(content="我喜欢吃苹果", strength=0.5)
    # 两句都重复同一已有记忆
    new_memories, boosted = learner.process(
        "我喜欢吃苹果。我喜欢吃苹果。", "", [existing]
    )
    # 不产生新记忆
    assert len(new_memories) == 0
    # 只弱增强一次
    assert len(boosted) == 1
    assert existing.strength == pytest.approx(0.55)  # 只加一次 0.05
