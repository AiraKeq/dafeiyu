"""记忆数据模型。

定义统一的记忆数据结构 MemoryItem，支持短期与长期两类记忆，
提供序列化/反序列化与工厂方法，供存储层与各引擎使用。
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MemoryItem:
    """单条记忆的数据模型。

    模仿人类神经网络的记忆单元，每条记忆带有强度、时间戳、
    召回信息与标签，支持短期与长期两种生命周期类型。

    Attributes:
        id: 唯一标识，由 uuid4 生成。
        content: 记忆内容文本。
        type: 记忆类型，"short" 表示短期记忆，"long" 表示长期记忆。
        strength: 记忆强度，取值范围 0-1，随时间衰减、随召回增强。
        created_at: 创建时间戳（秒）。
        last_recalled_at: 上次被召回的时间戳（秒）。
        recall_count: 累计被召回的次数。
        tags: 标签列表，用于相关性召回匹配。
        source: 记忆来源，如 "auto_learn"、"llm_tool"、"manual"。
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""
    type: str = "short"
    strength: float = 0.5
    created_at: float = field(default_factory=time.time)
    last_recalled_at: float = field(default_factory=time.time)
    recall_count: int = 0
    tags: List[str] = field(default_factory=list)
    source: str = "manual"

    def to_dict(self) -> dict:
        """将记忆序列化为字典，便于持久化为 JSON。"""
        return {
            "id": self.id,
            "content": self.content,
            "type": self.type,
            "strength": self.strength,
            "created_at": self.created_at,
            "last_recalled_at": self.last_recalled_at,
            "recall_count": self.recall_count,
            "tags": list(self.tags),
            "source": self.source,
        }

    @staticmethod
    def from_dict(data: dict) -> "MemoryItem":
        """从字典反序列化为 MemoryItem 对象。

        对缺失或异常字段做容错处理，保证旧数据加载不崩溃。
        """
        return MemoryItem(
            id=data.get("id") or str(uuid.uuid4()),
            content=data.get("content", ""),
            type=data.get("type", "short"),
            strength=float(data.get("strength", 0.5)),
            created_at=float(data.get("created_at", time.time())),
            last_recalled_at=float(data.get("last_recalled_at", time.time())),
            recall_count=int(data.get("recall_count", 0)),
            tags=list(data.get("tags", []) or []),
            source=data.get("source", "manual"),
        )

    @staticmethod
    def create_short(
        content: str,
        tags: Optional[List[str]] = None,
        source: str = "manual",
        strength: float = 0.5,
    ) -> "MemoryItem":
        """创建短期记忆的工厂方法。

        Args:
            content: 记忆内容文本。
            tags: 标签列表，默认为空。
            source: 记忆来源。
            strength: 初始强度，默认 0.5。
        """
        return MemoryItem(
            content=content,
            type="short",
            strength=strength,
            tags=list(tags) if tags else [],
            source=source,
        )

    @staticmethod
    def create_long(
        content: str,
        tags: Optional[List[str]] = None,
        source: str = "manual",
        strength: float = 0.7,
    ) -> "MemoryItem":
        """创建长期记忆的工厂方法。

        Args:
            content: 记忆内容文本。
            tags: 标签列表，默认为空。
            source: 记忆来源。
            strength: 初始强度，默认 0.7，应高于短期记忆。
        """
        return MemoryItem(
            content=content,
            type="long",
            strength=strength,
            tags=list(tags) if tags else [],
            source=source,
        )
