"""DecayEngine 衰退引擎单元测试。"""

import time

import pytest

from memory.models import MemoryItem
from memory.store import MemoryStore
from memory.decay import DecayEngine


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
    """默认配置的衰退引擎。"""
    return DecayEngine(DEFAULT_CONFIG)


def test_should_decay_true(engine):
    """超过间隔返回 True。"""
    # 2 小时前，超过 1 小时间隔
    last_check = time.time() - 7200
    assert engine.should_decay(last_check) is True


def test_should_decay_false(engine):
    """未到间隔返回 False。"""
    # 100 秒前，未到 3600 秒间隔
    last_check = time.time() - 100
    assert engine.should_decay(last_check) is False


def test_should_decay_exactly_interval(engine):
    """恰好达到间隔时返回 True。"""
    # 恰好 3600 秒前
    last_check = time.time() - 3600
    assert engine.should_decay(last_check) is True


def test_apply_decay_short(engine):
    """短期记忆衰减后强度降低。"""
    mem = MemoryItem.create_short("短期记忆", strength=0.5)
    survived, forgotten, downgraded = engine.apply_decay([mem])
    # 0.5 - 0.15 = 0.35，高于阈值 0.1，应保留
    assert len(survived) == 1
    assert len(forgotten) == 0
    assert len(downgraded) == 0
    assert mem.strength == pytest.approx(0.35)


def test_apply_decay_long(engine):
    """长期记忆衰减率低于短期。"""
    short_mem = MemoryItem.create_short("短期", strength=0.7)
    long_mem = MemoryItem.create_long("长期", strength=0.7)
    # 分别衰减
    engine.apply_decay([short_mem])
    engine.apply_decay([long_mem])
    # 短期衰减 0.15，长期衰减 0.03，长期剩余强度应更高
    assert short_mem.strength == pytest.approx(0.55)
    assert long_mem.strength == pytest.approx(0.67)
    assert long_mem.strength > short_mem.strength


def test_apply_decay_forget(engine):
    """强度低于阈值的短期记忆被遗忘。"""
    # 0.2 - 0.15 = 0.05 < 0.1，被遗忘
    mem = MemoryItem.create_short("将被遗忘", strength=0.2)
    survived, forgotten, downgraded = engine.apply_decay([mem])
    assert len(survived) == 0
    assert len(forgotten) == 1
    assert len(downgraded) == 0
    assert forgotten[0] is mem


def test_apply_decay_downgrade(engine):
    """强度低于阈值的长期记忆被降级为短期。"""
    # 0.1 - 0.03 = 0.07 < 0.1，被降级
    mem = MemoryItem.create_long("将被降级", strength=0.1)
    survived, forgotten, downgraded = engine.apply_decay([mem])
    assert len(survived) == 0
    assert len(forgotten) == 0
    assert len(downgraded) == 1
    assert downgraded[0] is mem
    assert mem.type == "short"


def test_apply_decay_mixed(engine):
    """混合列表正确分类。"""
    memories = [
        MemoryItem.create_short("保留短期", strength=0.5),      # 0.35 保留
        MemoryItem.create_short("遗忘短期", strength=0.2),      # 0.05 遗忘
        MemoryItem.create_long("保留长期", strength=0.7),       # 0.67 保留
        MemoryItem.create_long("降级长期", strength=0.1),       # 0.07 降级
    ]
    survived, forgotten, downgraded = engine.apply_decay(memories)
    assert len(survived) == 2
    assert len(forgotten) == 1
    assert len(downgraded) == 1


def test_apply_decay_empty(engine):
    """空列表不崩溃。"""
    survived, forgotten, downgraded = engine.apply_decay([])
    assert survived == []
    assert forgotten == []
    assert downgraded == []


def test_apply_decay_strength_not_negative(engine):
    """衰减后强度不为负。"""
    mem = MemoryItem.create_short("极低强度", strength=0.05)
    engine.apply_decay([mem])
    # 0.05 - 0.15 = -0.1 -> max(0, -0.1) = 0
    assert mem.strength == 0.0


def test_process_user_disabled(tmp_path):
    """enabled=False 时不做任何操作。"""
    config = dict(DEFAULT_CONFIG)
    config["decay_enabled"] = False
    engine = DecayEngine(config)
    store = MemoryStore(str(tmp_path), DEFAULT_CONFIG)
    # 添加一条记忆
    mem = MemoryItem.create_short("测试", strength=0.2)
    store.add_memory("u1", mem)
    # 手动设置很久以前的衰减检查时间
    data = store.get_user_data("u1")
    data["last_decay_check"] = time.time() - 99999
    store.save_user_data("u1", data)
    # 执行衰减
    result = engine.process_user(store, "u1")
    # 衰减被禁用，记忆应保持不变
    short_term = result["short_term"]
    assert len(short_term) == 1
    assert short_term[0].strength == pytest.approx(0.2)


def test_process_user_normal(tmp_path):
    """正常衰减流程。"""
    engine = DecayEngine(DEFAULT_CONFIG)
    store = MemoryStore(str(tmp_path), DEFAULT_CONFIG)
    # 短期低强度（会被遗忘），长期正常（会保留）
    short_mem = MemoryItem.create_short("将被遗忘", strength=0.2)
    long_mem = MemoryItem.create_long("将保留", strength=0.7)
    store.add_memory("u1", short_mem)
    store.add_memory("u1", long_mem)
    # 设置很久以前的衰减检查时间，确保触发衰减
    data = store.get_user_data("u1")
    data["last_decay_check"] = time.time() - 7200
    store.save_user_data("u1", data)
    # 执行衰减
    result = engine.process_user(store, "u1")
    # 短期记忆 0.2-0.15=0.05 < 0.1 被遗忘
    assert len(result["short_term"]) == 0
    # 长期记忆 0.7-0.03=0.67 >= 0.1 被保留
    assert len(result["long_term"]) == 1
    assert result["long_term"][0].strength == pytest.approx(0.67)
    # 衰减检查时间应被更新
    assert result["last_decay_check"] > time.time() - 7200


def test_process_user_not_time_yet(tmp_path):
    """未到衰减检查时间时不衰减。"""
    engine = DecayEngine(DEFAULT_CONFIG)
    store = MemoryStore(str(tmp_path), DEFAULT_CONFIG)
    mem = MemoryItem.create_short("测试", strength=0.2)
    store.add_memory("u1", mem)
    # last_decay_check 默认为当前时间，未到间隔
    result = engine.process_user(store, "u1")
    # 不衰减，记忆保持原样
    assert len(result["short_term"]) == 1
    assert result["short_term"][0].strength == pytest.approx(0.2)


def test_process_user_downgrade_moves_to_short(tmp_path):
    """长期记忆降级后转移到短期列表。"""
    engine = DecayEngine(DEFAULT_CONFIG)
    store = MemoryStore(str(tmp_path), DEFAULT_CONFIG)
    # 长期低强度，会被降级
    long_mem = MemoryItem.create_long("将被降级", strength=0.1)
    store.add_memory("u1", long_mem)
    # 设置很久以前的检查时间
    data = store.get_user_data("u1")
    data["last_decay_check"] = time.time() - 7200
    store.save_user_data("u1", data)
    result = engine.process_user(store, "u1")
    # 长期记忆 0.1-0.03=0.07 < 0.1 被降级，转入短期列表
    assert len(result["long_term"]) == 0
    assert len(result["short_term"]) == 1
    assert result["short_term"][0].type == "short"
