"""Consolidator 巩固引擎单元测试。"""

import pytest

from memory.models import MemoryItem
from memory.consolidator import Consolidator


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
def consolidator():
    """默认配置的巩固器。"""
    return Consolidator(DEFAULT_CONFIG)


def test_boost_recall(consolidator):
    """召回增强后 strength 提升、recall_count+1、last_recalled_at 更新。"""
    old_time = 1000.0
    new_time = 2000.0
    mem = MemoryItem.create_short("测试", strength=0.5)
    mem.recall_count = 0
    mem.last_recalled_at = old_time
    consolidator.boost_recall(mem, now=new_time)
    assert mem.strength == pytest.approx(0.65)  # 0.5 + 0.15
    assert mem.recall_count == 1
    assert mem.last_recalled_at == new_time


def test_boost_recall_default_now(consolidator):
    """boost_recall 未传 now 时使用当前时间。"""
    mem = MemoryItem.create_short("测试", strength=0.5)
    before = 1000.0
    mem.last_recalled_at = before
    consolidator.boost_recall(mem)
    # last_recalled_at 应被更新为当前时间
    assert mem.last_recalled_at > before


def test_boost_recall_max(consolidator):
    """strength 不超过 1.0。"""
    mem = MemoryItem.create_short("测试", strength=0.9)
    consolidator.boost_recall(mem)
    # 0.9 + 0.15 = 1.05，应被 clamp 到 1.0
    assert mem.strength == 1.0


def test_boost_recall_multiple(consolidator):
    """多次召回增强累计生效。"""
    mem = MemoryItem.create_short("测试", strength=0.3)
    consolidator.boost_recall(mem, now=1000.0)
    consolidator.boost_recall(mem, now=2000.0)
    assert mem.strength == pytest.approx(0.6)  # 0.3 + 0.15 + 0.15
    assert mem.recall_count == 2


def test_weak_boost(consolidator):
    """弱增强后 strength 小幅提升，recall_count 不变。"""
    mem = MemoryItem.create_short("测试", strength=0.5)
    mem.recall_count = 3
    old_recalled_at = mem.last_recalled_at
    consolidator.weak_boost_memory(mem)
    assert mem.strength == pytest.approx(0.55)  # 0.5 + 0.05
    assert mem.recall_count == 3  # 召回次数不变
    assert mem.last_recalled_at == old_recalled_at  # 召回时间不变


def test_weak_boost_max(consolidator):
    """弱增强强度不超过 1.0。"""
    mem = MemoryItem.create_short("测试", strength=0.98)
    consolidator.weak_boost_memory(mem)
    assert mem.strength == 1.0


def test_should_consolidate_by_recall(consolidator):
    """recall_count 达阈值时返回 True。"""
    mem = MemoryItem.create_short("测试", strength=0.5)
    mem.recall_count = 3  # 等于阈值 3
    assert consolidator.should_consolidate(mem) is True


def test_should_consolidate_by_strength(consolidator):
    """strength 达阈值时返回 True。"""
    mem = MemoryItem.create_short("测试", strength=0.8)
    mem.recall_count = 0
    assert consolidator.should_consolidate(mem) is True


def test_should_consolidate_below_thresholds(consolidator):
    """未达任何阈值时返回 False。"""
    mem = MemoryItem.create_short("测试", strength=0.5)
    mem.recall_count = 1
    assert consolidator.should_consolidate(mem) is False


def test_should_consolidate_long_term(consolidator):
    """长期记忆返回 False。"""
    mem = MemoryItem.create_long("测试", strength=0.9)
    mem.recall_count = 10
    assert consolidator.should_consolidate(mem) is False


def test_consolidate(consolidator):
    """巩固后 type="long"，strength 重置。"""
    mem = MemoryItem.create_short("测试", strength=0.9)
    mem.recall_count = 5
    result = consolidator.consolidate(mem)
    assert result is mem  # 返回同一对象
    assert mem.type == "long"
    assert mem.strength == pytest.approx(0.7)  # long_term_initial_strength


def test_process_consolidation(consolidator):
    """批量巩固正确分离。"""
    mem_keep = MemoryItem.create_short("保留", strength=0.5)
    mem_keep.recall_count = 0
    mem_consolidate = MemoryItem.create_short("巩固", strength=0.5)
    mem_consolidate.recall_count = 3  # 达到召回阈值
    short_term = [mem_keep, mem_consolidate]
    kept, consolidated = consolidator.process_consolidation(short_term)
    assert len(kept) == 1
    assert len(consolidated) == 1
    assert kept[0] is mem_keep
    assert consolidated[0] is mem_consolidate
    assert consolidated[0].type == "long"


def test_process_consolidation_empty(consolidator):
    """空列表不崩溃。"""
    kept, consolidated = consolidator.process_consolidation([])
    assert kept == []
    assert consolidated == []


def test_process_consolidation_all_consolidate(consolidator):
    """全部满足巩固条件时全部转移。"""
    memories = [
        MemoryItem.create_short("m1", strength=0.9),
        MemoryItem.create_short("m2", strength=0.85),
    ]
    kept, consolidated = consolidator.process_consolidation(memories)
    assert len(kept) == 0
    assert len(consolidated) == 2
    for m in consolidated:
        assert m.type == "long"


def test_process_consolidation_none_consolidate(consolidator):
    """全部不满足条件时全部保留。"""
    memories = [
        MemoryItem.create_short("m1", strength=0.5),
        MemoryItem.create_short("m2", strength=0.6),
    ]
    kept, consolidated = consolidator.process_consolidation(memories)
    assert len(kept) == 2
    assert len(consolidated) == 0
