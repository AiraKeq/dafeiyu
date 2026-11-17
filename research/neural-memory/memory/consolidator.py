"""记忆增强与巩固引擎。

模仿人类神经网络的记忆巩固机制，提供召回增强、弱增强、
短期记忆升级为长期记忆等能力，供记忆调度主流程调用。
"""

import logging
import time
from typing import List, Tuple

from .models import MemoryItem

logger = logging.getLogger(__name__)


class Consolidator:
    """记忆增强与巩固器。

    通过召回增强、弱增强、巩固升级等操作，模拟人类神经网络的
    记忆强化与短期→长期转化过程。所有可调阈值均通过 config 注入。
    """

    def __init__(self, config: dict):
        """初始化巩固器，从配置字典读取各项阈值与增强幅度。

        Args:
            config: 配置字典，缺失项使用默认值。
        """
        self.config = config or {}
        # 召回增强幅度
        self.recall_boost = float(config.get("recall_boost", 0.15))
        # 弱增强幅度
        self.weak_boost = float(config.get("weak_boost", 0.05))
        # 巩固所需召回次数阈值
        self.consolidation_recall_threshold = int(config.get("consolidation_recall_threshold", 3))
        # 巩固所需强度阈值
        self.consolidation_strength_threshold = float(config.get("consolidation_strength_threshold", 0.8))
        # 升级为长期记忆时的初始强度
        self.long_term_initial_strength = float(config.get("long_term_initial_strength", 0.7))

    def boost_recall(self, memory: MemoryItem, now: float = None) -> None:
        """召回增强：提升记忆强度、增加召回计数、刷新召回时间。

        Args:
            memory: 被召回的记忆对象，原地修改。
            now: 当前时间戳，未指定则使用 time.time()。
        """
        # 强度增加但不超过上限 1.0
        memory.strength = min(1.0, memory.strength + self.recall_boost)
        # 召回次数自增
        memory.recall_count += 1
        # 更新最近召回时间
        memory.last_recalled_at = now if now is not None else time.time()

    def weak_boost_memory(self, memory: MemoryItem) -> None:
        """弱增强：仅小幅提升记忆强度，不更新召回次数与时间。

        用于用户发言触发标签匹配时的被动强化，避免污染召回统计。

        Args:
            memory: 待弱增强的记忆对象，原地修改。
        """
        # 仅提升强度，不超过上限 1.0
        memory.strength = min(1.0, memory.strength + self.weak_boost)

    def should_consolidate(self, memory: MemoryItem) -> bool:
        """判断短期记忆是否满足巩固为长期记忆的条件。

        条件：类型为 "short"，且召回次数达到阈值或强度达到阈值。

        Args:
            memory: 待判断的记忆对象。

        Returns:
            是否应当巩固。
        """
        if memory.type != "short":
            return False
        # 召回次数达标或强度达标均可触发巩固
        if memory.recall_count >= self.consolidation_recall_threshold:
            return True
        if memory.strength >= self.consolidation_strength_threshold:
            return True
        return False

    def consolidate(self, memory: MemoryItem) -> MemoryItem:
        """将短期记忆升级为长期记忆。

        原地修改 memory 对象：类型置为 "long"，强度重置为长期初始值。

        Args:
            memory: 待巩固的短期记忆对象。

        Returns:
            修改后的同一 memory 对象引用。
        """
        # 类型升级为长期
        memory.type = "long"
        # 强度重置为长期记忆初始值
        memory.strength = self.long_term_initial_strength
        return memory

    def process_consolidation(
        self, short_term: List[MemoryItem]
    ) -> Tuple[List[MemoryItem], List[MemoryItem]]:
        """遍历短期记忆列表，将满足条件的记忆巩固为长期记忆。

        满足巩固条件的记忆从短期列表移除，调用 consolidate 修改后
        放入长期列表。返回两个新列表，但其中元素仍为原 MemoryItem 引用。

        Args:
            short_term: 短期记忆列表（不会被原地修改）。

        Returns:
            二元组 (保留的短期记忆列表, 需巩固为长期的记忆列表)。
        """
        kept_short: List[MemoryItem] = []
        consolidated_long: List[MemoryItem] = []
        for memory in short_term:
            if self.should_consolidate(memory):
                # 巩固并放入长期列表
                consolidated_long.append(self.consolidate(memory))
            else:
                # 保留在短期列表
                kept_short.append(memory)
        return kept_short, consolidated_long

    def find_tag_matches(
        self, user_text: str, memories: List[MemoryItem]
    ) -> List[MemoryItem]:
        """在记忆列表中查找标签与用户文本匹配的记忆。

        遍历每条记忆的 tags，若任一标签出现在 user_text 中
        （大小写不敏感），则视为匹配。

        Args:
            user_text: 用户发言文本。
            memories: 待检索的记忆列表。

        Returns:
            匹配到的记忆列表。
        """
        if not user_text or not memories:
            return []
        user_text_lower = user_text.lower()
        matched: List[MemoryItem] = []
        for memory in memories:
            for tag in memory.tags:
                if not tag:
                    continue
                # 大小写不敏感匹配
                if tag.lower() in user_text_lower:
                    matched.append(memory)
                    break
        return matched

    def process_weak_boost(
        self,
        user_text: str,
        short_term: List[MemoryItem],
        long_term: List[MemoryItem],
    ) -> None:
        """对用户发言执行弱增强。

        在短期与长期记忆中查找标签与发言匹配的记忆，对匹配项执行
        弱增强。原地修改，不返回。

        Args:
            user_text: 用户发言文本。
            short_term: 短期记忆列表。
            long_term: 长期记忆列表。
        """
        # 对短期记忆中匹配项执行弱增强
        for memory in self.find_tag_matches(user_text, short_term):
            self.weak_boost_memory(memory)
        # 对长期记忆中匹配项执行弱增强
        for memory in self.find_tag_matches(user_text, long_term):
            self.weak_boost_memory(memory)
