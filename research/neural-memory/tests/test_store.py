"""MemoryStore 存储层单元测试。

使用 tmp_path fixture 隔离每个测试的文件系统。
"""

import pytest

from memory.models import MemoryItem
from memory.store import MemoryStore


# 默认配置，与任务规格一致
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
def store(tmp_path):
    """用 tmp_path 创建临时存储目录的 MemoryStore。"""
    return MemoryStore(str(tmp_path), DEFAULT_CONFIG)


def test_get_user_data_new(store):
    """新用户返回默认结构。"""
    data = store.get_user_data("new_user")
    assert data["user_id"] == "new_user"
    assert data["short_term"] == []
    assert data["long_term"] == []
    assert data["affinity"] == 50
    assert data["interaction_count"] == 0
    assert data["mode"] == "full"
    assert "last_decay_check" in data
    assert isinstance(data["last_decay_check"], float)


def test_add_memory_short(store):
    """添加短期记忆后能读回。"""
    mem = MemoryItem.create_short("短期记忆内容")
    store.add_memory("u1", mem)
    short_term, long_term = store.get_all_memories("u1")
    assert len(short_term) == 1
    assert short_term[0].content == "短期记忆内容"
    assert short_term[0].type == "short"
    assert len(long_term) == 0


def test_add_memory_long(store):
    """添加长期记忆后能读回。"""
    mem = MemoryItem.create_long("长期记忆内容")
    store.add_memory("u1", mem)
    short_term, long_term = store.get_all_memories("u1")
    assert len(long_term) == 1
    assert long_term[0].content == "长期记忆内容"
    assert long_term[0].type == "long"
    assert len(short_term) == 0


def test_add_memory_multiple(store):
    """添加多条记忆到对应层级。"""
    store.add_memory("u1", MemoryItem.create_short("短期1"))
    store.add_memory("u1", MemoryItem.create_short("短期2"))
    store.add_memory("u1", MemoryItem.create_long("长期1"))
    short_term, long_term = store.get_all_memories("u1")
    assert len(short_term) == 2
    assert len(long_term) == 1


def test_remove_memory(store):
    """删除记忆后不再存在。"""
    mem = MemoryItem.create_short("待删除")
    store.add_memory("u1", mem)
    # 确认已添加
    short_term, _ = store.get_all_memories("u1")
    assert len(short_term) == 1
    # 删除
    store.remove_memory("u1", mem.id)
    short_term, _ = store.get_all_memories("u1")
    assert len(short_term) == 0


def test_remove_memory_from_long(store):
    """删除长期记忆。"""
    mem = MemoryItem.create_long("待删除长期")
    store.add_memory("u1", mem)
    store.remove_memory("u1", mem.id)
    _, long_term = store.get_all_memories("u1")
    assert len(long_term) == 0


def test_remove_memory_nonexistent(store):
    """删除不存在的记忆不崩溃。"""
    store.remove_memory("u1", "nonexistent-id")
    # 不崩溃即通过
    short_term, long_term = store.get_all_memories("u1")
    assert len(short_term) == 0
    assert len(long_term) == 0


def test_update_affinity(store):
    """更新好感度后能读回。"""
    store.update_affinity("u1", 80)
    assert store.get_affinity("u1") == 80


def test_update_affinity_multiple(store):
    """多次更新好感度以最后一次为准。"""
    store.update_affinity("u1", 30)
    store.update_affinity("u1", 75)
    assert store.get_affinity("u1") == 75


def test_get_affinity_default(store):
    """新用户好感度为配置默认值。"""
    assert store.get_affinity("new_user") == 50


def test_update_mode(store):
    """更新模式后能读回。"""
    store.update_mode("u1", "short")
    assert store.get_mode("u1") == "short"


def test_get_mode_default(store):
    """新用户模式默认为 full。"""
    assert store.get_mode("new_user") == "full"


def test_max_short_term(tmp_path):
    """超过最大数量时移除强度最低的。"""
    config = dict(DEFAULT_CONFIG)
    config["max_short_term"] = 3
    store = MemoryStore(str(tmp_path), config)
    # 添加 4 条短期记忆，强度各不相同
    store.add_memory("u1", MemoryItem.create_short("m1", strength=0.5))
    store.add_memory("u1", MemoryItem.create_short("m2", strength=0.3))
    store.add_memory("u1", MemoryItem.create_short("m3", strength=0.7))
    store.add_memory("u1", MemoryItem.create_short("m4", strength=0.6))
    short_term, _ = store.get_all_memories("u1")
    # 超过上限 3，最弱的（强度 0.3）应被移除
    assert len(short_term) == 3
    strengths = [m.strength for m in short_term]
    assert 0.3 not in strengths
    assert sorted(strengths) == [0.5, 0.6, 0.7]


def test_max_long_term(tmp_path):
    """长期记忆超过上限时移除强度最低的。"""
    config = dict(DEFAULT_CONFIG)
    config["max_long_term"] = 2
    store = MemoryStore(str(tmp_path), config)
    store.add_memory("u1", MemoryItem.create_long("l1", strength=0.5))
    store.add_memory("u1", MemoryItem.create_long("l2", strength=0.3))
    store.add_memory("u1", MemoryItem.create_long("l3", strength=0.7))
    _, long_term = store.get_all_memories("u1")
    assert len(long_term) == 2
    strengths = [m.strength for m in long_term]
    assert 0.3 not in strengths


def test_save_and_reload(tmp_path):
    """保存后重新读取数据一致。"""
    store1 = MemoryStore(str(tmp_path), DEFAULT_CONFIG)
    mem = MemoryItem.create_short("持久化测试", tags=["标签"])
    store1.add_memory("u1", mem)
    store1.update_affinity("u1", 65)
    store1.update_mode("u1", "short")
    # 新建 store 读取同一目录
    store2 = MemoryStore(str(tmp_path), DEFAULT_CONFIG)
    short_term, long_term = store2.get_all_memories("u1")
    assert len(short_term) == 1
    assert short_term[0].content == "持久化测试"
    assert short_term[0].tags == ["标签"]
    assert short_term[0].id == mem.id
    assert len(long_term) == 0
    assert store2.get_affinity("u1") == 65
    assert store2.get_mode("u1") == "short"


def test_get_all_memories_returns_tuple(store):
    """get_all_memories 返回二元组。"""
    result = store.get_all_memories("u1")
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_user_id_safety(store):
    """包含特殊字符的 user_id 不会导致路径穿越。"""
    # 包含路径分隔符等特殊字符，应被清理
    data = store.get_user_data("..%2F..%2Fevil")
    assert data is not None
    assert data["user_id"] == "..%2F..%2Fevil"
