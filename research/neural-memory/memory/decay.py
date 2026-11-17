"""记忆衰退引擎。

模拟人类神经网络中记忆随时间衰退的机制：定期对短期与长期记忆
应用强度衰减，强度过低时短期记忆被遗忘、长期记忆被降级为短期，
从而保持记忆系统的活跃度与有限容量。
"""

import logging
import time
from typing import List, Tuple

from .models import MemoryItem

logger = logging.getLogger(__name__)


class DecayEngine:
    """记忆衰退引擎。

    依据配置的衰减速率与遗忘阈值，定期衰减用户记忆强度，
    并根据强度决定记忆是被遗忘、降级还是保留。

    Attributes:
        enabled: 是否启用衰退机制。
        short_decay_rate: 短期记忆每次衰减的强度减少量。
        long_decay_rate: 长期记忆每次衰减的强度减少量。
        forget_threshold: 遗忘阈值，强度低于该值的记忆将被遗忘或降级。
        decay_check_interval: 两次衰减检查之间的最小间隔（秒）。
    """

    def __init__(self, config: dict):
        """初始化衰退引擎。

        Args:
            config: 插件配置字典，用于读取衰减参数。
        """
        self.config = config or {}
        self.enabled = bool(config.get("decay_enabled", True))
        self.short_decay_rate = float(config.get("short_decay_rate", 0.15))
        self.long_decay_rate = float(config.get("long_decay_rate", 0.03))
        self.forget_threshold = float(config.get("forget_threshold", 0.1))
        self.decay_check_interval = int(config.get("decay_check_interval", 3600))

    def should_decay(self, last_decay_check: float) -> bool:
        """判断是否到了该执行衰减检查的时间。

        Args:
            last_decay_check: 上次执行衰减检查的时间戳（秒）。

        Returns:
            若距上次检查的时间间隔大于等于 decay_check_interval，返回 True。
        """
        # 当前时间距上次检查的间隔，缺失时间戳时按 0 处理
        elapsed = time.time() - float(last_decay_check or 0)
        return elapsed >= self.decay_check_interval

    def apply_decay(
        self, memories: List[MemoryItem], now: float = None
    ) -> Tuple[List[MemoryItem], List[MemoryItem], List[MemoryItem]]:
        """对记忆列表应用强度衰减。

        按记忆类型选择衰减率：短期记忆使用 short_decay_rate，
        长期记忆使用 long_decay_rate。衰减后强度低于 forget_threshold 时：
        - 短期记忆直接遗忘（放入 forgotten 列表，不保留）
        - 长期记忆降级为短期（type 改为 "short"，放入 downgraded 列表）

        Args:
            memories: 待衰减的记忆列表。
            now: 当前时间戳，默认 time.time()。预留参数，供未来基于时间差的
                衰减策略扩展使用。

        Returns:
            三元组 (survived, forgotten, downgraded)：
            - survived: 强度仍达标的记忆列表（不含降级的长期记忆）。
            - forgotten: 被遗忘的短期记忆列表。
            - downgraded: 被降级为短期的长期记忆列表（type 已改为 "short"）。
        """
        if now is None:
            now = time.time()

        survived: List[MemoryItem] = []
        forgotten: List[MemoryItem] = []
        downgraded: List[MemoryItem] = []

        for memory in memories:
            # 根据记忆类型选择对应的衰减率
            if memory.type == "long":
                decay_rate = self.long_decay_rate
            else:
                decay_rate = self.short_decay_rate

            # 应用衰减，确保强度不低于 0
            memory.strength = max(0.0, memory.strength - decay_rate)

            # 根据衰减后的强度判断记忆去留
            if memory.strength < self.forget_threshold:
                if memory.type == "long":
                    # 长期记忆强度过低，降级为短期记忆（保留，不删除）
                    memory.type = "short"
                    downgraded.append(memory)
                else:
                    # 短期记忆强度过低，直接遗忘
                    forgotten.append(memory)
            else:
                # 强度达标，保留
                survived.append(memory)

        return survived, forgotten, downgraded

    def process_user(self, store, user_id: str) -> dict:
        """完整处理一个用户的衰减流程。

        读取用户数据，判断是否到衰减检查时间；若到则对短期与长期记忆
        分别应用衰减，将被遗忘的短期记忆移除，将被降级的长期记忆
        从长期列表转移到短期列表，最后更新衰减检查时间并保存。

        Args:
            store: MemoryStore 实例，用于读写用户数据。
            user_id: 用户 ID。

        Returns:
            更新后的用户数据字典。若衰减未启用或未到检查时间，返回原数据。
            异常时返回重新读取的用户数据，不抛出。
        """
        # 衰减未启用，直接返回原数据，不做任何操作
        if not self.enabled:
            return store.get_user_data(user_id)

        try:
            # 读取用户当前数据
            data = store.get_user_data(user_id)
            last_decay_check = data.get("last_decay_check", 0)

            # 未到衰减检查时间，直接返回原数据
            if not self.should_decay(last_decay_check):
                return data

            now = time.time()

            # 对短期记忆应用衰减：被遗忘的不保留
            short_survived, short_forgotten, _ = self.apply_decay(
                data.get("short_term", []), now
            )
            data["short_term"] = short_survived

            # 对长期记忆应用衰减：被降级的从长期列表移除
            long_survived, _, long_downgraded = self.apply_decay(
                data.get("long_term", []), now
            )
            data["long_term"] = long_survived
            # 降级的长期记忆（已改为 short）转入短期列表
            data["short_term"].extend(long_downgraded)

            # 更新最后衰减检查时间
            data["last_decay_check"] = now

            # 保存到存储
            store.save_user_data(user_id, data)

            # 记录衰减结果日志
            if short_forgotten:
                logger.info(
                    f"用户 {user_id} 衰减遗忘 {len(short_forgotten)} 条短期记忆"
                )
            if long_downgraded:
                logger.info(
                    f"用户 {user_id} 衰减降级 {len(long_downgraded)} 条长期记忆"
                )

            return data
        except Exception as e:
            logger.error(f"用户 {user_id} 记忆衰减处理失败: {e}")
            return store.get_user_data(user_id)
