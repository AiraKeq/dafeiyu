"""RecallEngine 召回引擎单元测试。"""

import pytest

from memory.models import MemoryItem
from memory.recall import RecallEngine


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
    """默认配置的召回引擎。"""
    return RecallEngine(DEFAULT_CONFIG)


def test_tokenize_chinese(engine):
    """中文分词生成 2-gram 和 3-gram。"""
    tokens = engine.tokenize("我喜欢吃苹果")
    # 应包含 2-gram 和 3-gram
    assert "我喜" in tokens  # 2-gram
    assert "喜欢" in tokens  # 2-gram
    assert "苹果" in tokens  # 2-gram
    assert "我喜欢" in tokens  # 3-gram
    assert "吃苹果" in tokens  # 3-gram


def test_tokenize_english(engine):
    """英文分词。"""
    tokens = engine.tokenize("hello world")
    assert "hello" in tokens
    assert "world" in tokens


def test_tokenize_lowercase(engine):
    """分词统一转小写。"""
    tokens = engine.tokenize("Hello WORLD")
    assert "hello" in tokens
    assert "world" in tokens
    # 不应出现大写
    assert "Hello" not in tokens
    assert "WORLD" not in tokens


def test_tokenize_empty(engine):
    """空文本分词返回空列表。"""
    assert engine.tokenize("") == []
    assert engine.tokenize(None) == []


def test_tokenize_dedup(engine):
    """重复 token 去重。"""
    tokens = engine.tokenize("苹果 苹果")
    # 苹果只出现一次
    assert tokens.count("苹果") == 1


def test_score_relevance_tag_match(engine):
    """标签匹配得分高。"""
    mem = MemoryItem(content="某内容", tags=["苹果"], strength=0.5)
    query_tokens = ["苹果"]
    score = engine.score_relevance(mem, query_tokens)
    # 标签匹配 +2.0，加权 (0.5 + 0.5*0.5)=0.75 -> 2.0*0.75=1.5
    assert score > 0
    assert score == pytest.approx(1.5)


def test_score_relevance_content_match(engine):
    """内容匹配得分。"""
    mem = MemoryItem(content="我喜欢吃苹果", tags=[], strength=0.5)
    query_tokens = ["苹果"]
    score = engine.score_relevance(mem, query_tokens)
    # 内容匹配 +1.0，加权 0.75 -> 0.75
    assert score > 0
    assert score == pytest.approx(0.75)


def test_score_relevance_no_match(engine):
    """无匹配得分为 0。"""
    mem = MemoryItem(content="今天天气不错", tags=[], strength=0.5)
    query_tokens = ["苹果"]
    score = engine.score_relevance(mem, query_tokens)
    assert score == 0.0


def test_score_relevance_empty_tokens(engine):
    """空查询 token 得分为 0。"""
    mem = MemoryItem(content="内容", tags=["标签"], strength=0.5)
    assert engine.score_relevance(mem, []) == 0.0


def test_score_relevance_strength_weight(engine):
    """强度越高得分权重越大。"""
    low_strength = MemoryItem(content="苹果", tags=[], strength=0.1)
    high_strength = MemoryItem(content="苹果", tags=[], strength=1.0)
    query_tokens = ["苹果"]
    low_score = engine.score_relevance(low_strength, query_tokens)
    high_score = engine.score_relevance(high_strength, query_tokens)
    assert high_score > low_score


def test_recall_full_mode(engine):
    """完整模式搜索短期+长期。"""
    short_mem = MemoryItem(content="苹果很好吃", strength=0.5)
    long_mem = MemoryItem(content="香蕉很不错", strength=0.5)
    result = engine.recall("苹果", [short_mem], [long_mem], mode="full")
    # 应只召回匹配"苹果"的短期记忆
    assert len(result) == 1
    assert result[0].content == "苹果很好吃"


def test_recall_short_mode(engine):
    """短期模式只搜短期。"""
    short_mem = MemoryItem(content="苹果很好吃", strength=0.5)
    long_mem = MemoryItem(content="苹果香蕉", strength=0.5)
    result = engine.recall("苹果", [short_mem], [long_mem], mode="short")
    # 只搜短期，返回短期匹配
    assert len(result) == 1
    assert result[0].content == "苹果很好吃"


def test_recall_top_n(engine):
    """返回不超过 top_n 条。"""
    # 创建 10 条都匹配"苹果"的记忆
    short_term = [
        MemoryItem(content=f"苹果_{i}", strength=0.5) for i in range(10)
    ]
    result = engine.recall("苹果", short_term, [], mode="short")
    # top_n 默认为 5
    assert len(result) <= 5


def test_recall_top_n_custom(tmp_path):
    """自定义 top_n 限制返回数量。"""
    config = dict(DEFAULT_CONFIG)
    config["recall_top_n"] = 3
    engine = RecallEngine(config)
    short_term = [
        MemoryItem(content=f"苹果_{i}", strength=0.5) for i in range(10)
    ]
    result = engine.recall("苹果", short_term, [], mode="short")
    assert len(result) <= 3


def test_recall_boost(engine):
    """召回后记忆被增强（recall_count 增加）。"""
    mem = MemoryItem(content="苹果很好吃", strength=0.5)
    mem.recall_count = 0
    result = engine.recall("苹果", [mem], [], mode="short")
    assert len(result) == 1
    assert result[0].recall_count == 1  # 被增强


def test_recall_boost_strength(engine):
    """召回后记忆强度提升。"""
    mem = MemoryItem(content="苹果很好吃", strength=0.5)
    engine.recall("苹果", [mem], [], mode="short")
    assert mem.strength == pytest.approx(0.65)  # 0.5 + 0.15


def test_recall_empty(engine):
    """无匹配返回空列表。"""
    short_mem = MemoryItem(content="今天天气不错", strength=0.5)
    result = engine.recall("苹果", [short_mem], [], mode="full")
    assert result == []


def test_recall_empty_inputs(engine):
    """空记忆列表返回空。"""
    result = engine.recall("苹果", [], [], mode="full")
    assert result == []


def test_recall_sorted_by_score(engine):
    """召回结果按相关性降序排列。"""
    # 标签匹配得分高于纯内容匹配
    tag_mem = MemoryItem(content="其他内容", tags=["苹果"], strength=0.5)
    content_mem = MemoryItem(content="苹果好吃", tags=[], strength=0.5)
    result = engine.recall("苹果", [content_mem, tag_mem], [], mode="full")
    assert len(result) == 2
    # 标签匹配应排第一
    assert result[0] is tag_mem


def test_format_memories_empty(engine):
    """空列表返回空串。"""
    assert engine.format_memories_for_prompt([]) == ""


def test_format_memories_non_empty(engine):
    """非空返回格式化文本。"""
    memories = [
        MemoryItem(content="测试内容", strength=0.75),
        MemoryItem(content="第二条", strength=0.5),
    ]
    text = engine.format_memories_for_prompt(memories)
    assert "[神经网络记忆回顾]" in text
    assert "测试内容" in text
    assert "第二条" in text
    assert "75%" in text  # 0.75 -> 75%
    assert "50%" in text  # 0.5 -> 50%
    assert "1." in text
    assert "2." in text


def test_format_memories_single(engine):
    """单条记忆格式化。"""
    mem = MemoryItem(content="单条", strength=1.0)
    text = engine.format_memories_for_prompt([mem])
    assert "单条" in text
    assert "100%" in text
