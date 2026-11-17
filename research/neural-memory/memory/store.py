"""记忆存储与持久化层。

为每个用户维护独立的 JSON 记忆文件，管理短期/长期记忆、
好感度、记忆模式与互动统计，所有写入操作均做异常兜底，
保证对话主流程不被存储故障中断。
"""

import json
import logging
import os
import re
import time
from typing import List, Tuple

from .models import MemoryItem

logger = logging.getLogger(__name__)


class MemoryStore:
    """用户记忆持久化管理器。

    每个用户的记忆存储在 `data/neural_memory/users/{user_id}.json`，
    包含短期记忆、长期记忆、好感度、互动统计与记忆模式等字段。
    """

    def __init__(self, data_dir: str, config: dict):
        """初始化存储器。

        Args:
            data_dir: 插件所在目录，实际存储目录为其下的
                data/neural_memory/users。
            config: 插件配置字典，用于读取默认好感度与数量上限。
        """
        self.config = config or {}
        # 用户记忆文件统一存放在 data/neural_memory/users 目录
        self.store_dir = os.path.join(data_dir, "data", "neural_memory", "users")
        try:
            os.makedirs(self.store_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"创建记忆存储目录失败: {e}")

    def _user_path(self, user_id: str) -> str:
        """返回用户记忆文件路径，并对 user_id 做安全处理。

        仅保留字母、数字与 -_ 字符，避免路径穿越与非法文件名。
        """
        safe_id = re.sub(r"[^a-zA-Z0-9\-_]", "", str(user_id))
        if not safe_id:
            safe_id = "unknown"
        return os.path.join(self.store_dir, f"{safe_id}.json")

    def _default_user_data(self, user_id: str) -> dict:
        """构造新用户的默认数据结构。"""
        return {
            "user_id": user_id,
            "short_term": [],
            "long_term": [],
            "affinity": self.config.get("affinity_default", 50),
            "interaction_count": 0,
            "mode": "full",
            "last_decay_check": time.time(),
        }

    def get_user_data(self, user_id: str) -> dict:
        """读取用户数据，不存在或读取失败时返回默认结构。

        读取成功时将记忆列表从 dict 转为 MemoryItem 对象，
        并补全旧数据中可能缺失的字段。
        """
        default_data = self._default_user_data(user_id)
        path = self._user_path(user_id)
        try:
            if not os.path.exists(path):
                return default_data
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning(f"用户 {user_id} 记忆文件格式异常，使用默认数据")
                return default_data
            # 将记忆列表从字典反序列化为 MemoryItem 对象
            data["short_term"] = [
                MemoryItem.from_dict(m) if isinstance(m, dict) else m
                for m in data.get("short_term", []) or []
            ]
            data["long_term"] = [
                MemoryItem.from_dict(m) if isinstance(m, dict) else m
                for m in data.get("long_term", []) or []
            ]
            # 补全缺失字段，兼容旧版本数据
            for key, value in default_data.items():
                if key not in data:
                    data[key] = value
            return data
        except Exception as e:
            logger.error(f"读取用户 {user_id} 记忆数据失败: {e}")
            return default_data

    def save_user_data(self, user_id: str, data: dict) -> None:
        """安全写入用户数据为 JSON。

        写入前将 MemoryItem 对象转为字典。写入失败时记录日志，
        不抛出异常，避免影响对话主流程。
        """
        path = self._user_path(user_id)
        try:
            save_data = dict(data)
            # 将记忆列表中的 MemoryItem 序列化为字典
            if "short_term" in save_data:
                save_data["short_term"] = [
                    m.to_dict() if isinstance(m, MemoryItem) else m
                    for m in save_data["short_term"]
                ]
            if "long_term" in save_data:
                save_data["long_term"] = [
                    m.to_dict() if isinstance(m, MemoryItem) else m
                    for m in save_data["long_term"]
                ]
            # 先写入临时文件再替换，降低写入中断导致损坏的风险
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"保存用户 {user_id} 记忆数据失败: {e}")

    def add_memory(self, user_id: str, memory: MemoryItem) -> None:
        """添加记忆到对应层级。

        根据 memory.type 添加到短期或长期记忆列表，超出最大数量
        限制时移除强度最低的记忆，最后持久化保存。
        """
        try:
            data = self.get_user_data(user_id)
            if memory.type == "long":
                data["long_term"].append(memory)
                self._enforce_max(data["long_term"], self.config.get("max_long_term", 200))
            else:
                data["short_term"].append(memory)
                self._enforce_max(data["short_term"], self.config.get("max_short_term", 100))
            self.save_user_data(user_id, data)
        except Exception as e:
            logger.error(f"添加用户 {user_id} 记忆失败: {e}")

    def _enforce_max(self, memories: List[MemoryItem], max_count: int) -> None:
        """当记忆数量超过上限时，移除强度最低的记忆。"""
        while len(memories) > max_count:
            if not memories:
                break
            weakest = min(memories, key=lambda m: m.strength)
            memories.remove(weakest)

    def remove_memory(self, user_id: str, memory_id: str) -> None:
        """删除指定 ID 的记忆（短期与长期均检查），并保存。"""
        try:
            data = self.get_user_data(user_id)
            data["short_term"] = [m for m in data["short_term"] if m.id != memory_id]
            data["long_term"] = [m for m in data["long_term"] if m.id != memory_id]
            self.save_user_data(user_id, data)
        except Exception as e:
            logger.error(f"删除用户 {user_id} 记忆失败: {e}")

    def get_all_memories(self, user_id: str) -> Tuple[List[MemoryItem], List[MemoryItem]]:
        """返回用户的短期与长期记忆列表，元组形式 (short_term, long_term)。"""
        data = self.get_user_data(user_id)
        return data["short_term"], data["long_term"]

    def update_affinity(self, user_id: str, affinity: int) -> None:
        """更新用户好感度并保存。"""
        try:
            data = self.get_user_data(user_id)
            data["affinity"] = affinity
            self.save_user_data(user_id, data)
        except Exception as e:
            logger.error(f"更新用户 {user_id} 好感度失败: {e}")

    def get_affinity(self, user_id: str) -> int:
        """获取用户好感度，缺失时返回配置默认值。"""
        data = self.get_user_data(user_id)
        return data.get("affinity", self.config.get("affinity_default", 50))

    def update_mode(self, user_id: str, mode: str) -> None:
        """更新用户记忆模式（full/short）并保存。"""
        try:
            data = self.get_user_data(user_id)
            data["mode"] = mode
            self.save_user_data(user_id, data)
        except Exception as e:
            logger.error(f"更新用户 {user_id} 记忆模式失败: {e}")

    def get_mode(self, user_id: str) -> str:
        """获取用户记忆模式，缺失时返回默认 'full'。"""
        data = self.get_user_data(user_id)
        return data.get("mode", "full")
