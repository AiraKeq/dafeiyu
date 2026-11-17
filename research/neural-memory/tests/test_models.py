"""MemoryItem 数据模型单元测试。"""

from memory.models import MemoryItem


def test_create_short():
    """创建短期记忆，验证 type 为 short，strength 默认 0.5。"""
    item = MemoryItem.create_short("测试短期记忆")
    assert item.content == "测试短期记忆"
    assert item.type == "short"
    assert item.strength == 0.5
    assert item.tags == []
    assert item.source == "manual"


def test_create_short_with_params():
    """创建短期记忆时传入自定义参数。"""
    item = MemoryItem.create_short(
        "测试内容", tags=["标签1", "标签2"], source="auto_learn", strength=0.8
    )
    assert item.tags == ["标签1", "标签2"]
    assert item.source == "auto_learn"
    assert item.strength == 0.8


def test_create_long():
    """创建长期记忆，验证 type 为 long，strength 默认 0.7。"""
    item = MemoryItem.create_long("测试长期记忆")
    assert item.content == "测试长期记忆"
    assert item.type == "long"
    assert item.strength == 0.7
    assert item.tags == []
    assert item.source == "manual"


def test_create_long_with_params():
    """创建长期记忆时传入自定义参数。"""
    item = MemoryItem.create_long(
        "测试内容", tags=["偏好"], source="llm_tool", strength=0.9
    )
    assert item.tags == ["偏好"]
    assert item.source == "llm_tool"
    assert item.strength == 0.9


def test_to_dict():
    """序列化包含所有字段。"""
    item = MemoryItem(
        content="内容",
        type="short",
        strength=0.6,
        tags=["a", "b"],
        source="manual",
    )
    data = item.to_dict()
    # 验证所有 9 个字段均存在且值正确
    assert data["id"] == item.id
    assert data["content"] == "内容"
    assert data["type"] == "short"
    assert data["strength"] == 0.6
    assert data["created_at"] == item.created_at
    assert data["last_recalled_at"] == item.last_recalled_at
    assert data["recall_count"] == item.recall_count
    assert data["tags"] == ["a", "b"]
    assert data["source"] == "manual"


def test_from_dict():
    """反序列化正确还原。"""
    data = {
        "id": "test-id-123",
        "content": "反序列化内容",
        "type": "long",
        "strength": 0.85,
        "created_at": 1000.0,
        "last_recalled_at": 2000.0,
        "recall_count": 5,
        "tags": ["标签"],
        "source": "auto_learn",
    }
    item = MemoryItem.from_dict(data)
    assert item.id == "test-id-123"
    assert item.content == "反序列化内容"
    assert item.type == "long"
    assert item.strength == 0.85
    assert item.created_at == 1000.0
    assert item.last_recalled_at == 2000.0
    assert item.recall_count == 5
    assert item.tags == ["标签"]
    assert item.source == "auto_learn"


def test_from_dict_missing_fields():
    """缺失字段时用默认值，不崩溃。"""
    # 只提供 content，其余字段全部缺失
    item = MemoryItem.from_dict({"content": "部分数据"})
    assert item.content == "部分数据"
    assert item.type == "short"
    assert item.strength == 0.5
    assert item.recall_count == 0
    assert item.tags == []
    assert item.source == "manual"
    # id 应被自动生成为非空字符串
    assert isinstance(item.id, str) and len(item.id) > 0
    # 时间戳应为浮点数
    assert isinstance(item.created_at, float)
    assert isinstance(item.last_recalled_at, float)


def test_from_dict_empty():
    """空字典反序列化不崩溃。"""
    item = MemoryItem.from_dict({})
    assert item.content == ""
    assert item.type == "short"
    assert item.strength == 0.5
    assert item.tags == []


def test_from_dict_tags_none():
    """tags 为 None 时回退为空列表。"""
    item = MemoryItem.from_dict({"content": "x", "tags": None})
    assert item.tags == []


def test_tags_default_empty():
    """默认 tags 为空列表。"""
    item = MemoryItem(content="测试")
    assert item.tags == []
    # 验证两条记忆的默认 tags 互不影响（不是同一引用）
    item2 = MemoryItem(content="测试2")
    item.tags.append("shared")
    assert item2.tags == []


def test_default_values():
    """直接构造时各字段默认值正确。"""
    item = MemoryItem()
    assert item.content == ""
    assert item.type == "short"
    assert item.strength == 0.5
    assert item.recall_count == 0
    assert item.tags == []
    assert item.source == "manual"
    assert isinstance(item.id, str)
    assert isinstance(item.created_at, float)


def test_to_dict_tags_is_copy():
    """to_dict 返回的 tags 是副本，修改不影响原对象。"""
    item = MemoryItem(content="x", tags=["a"])
    data = item.to_dict()
    data["tags"].append("b")
    assert item.tags == ["a"]
