"""好感度引擎。

根据用户发言的情感倾向动态调整对用户的好感度，
并将好感度映射为关系阶段（陌生/熟悉/亲近/挚友），
为人格扮演与回复语气提供基础。
"""

import logging
from typing import Tuple

logger = logging.getLogger(__name__)


class AffinityEngine:
    """好感度引擎：管理用户好感度数值与关系阶段的映射。"""

    def __init__(self, config: dict):
        self.config = config or {}
        self.enabled = bool(config.get("affinity_enabled", True))
        self.default_affinity = int(config.get("affinity_default", 50))
        self.positive_gain = int(config.get("affinity_positive_gain", 2))
        self.negative_loss = int(config.get("affinity_negative_loss", 3))

    def get_stage(self, affinity: int) -> str:
        """根据好感度数值返回关系阶段。

        0-24: stranger（陌生）
        25-49: acquaintance（熟悉）
        50-74: close（亲近）
        75-100: intimate（挚友）
        """
        try:
            if affinity < 25:
                return "stranger"
            elif affinity < 50:
                return "acquaintance"
            elif affinity < 75:
                return "close"
            else:
                return "intimate"
        except Exception:
            return "stranger"

    def get_stage_label(self, stage: str) -> str:
        """返回关系阶段的中文标签。"""
        labels = {
            "stranger": "陌生",
            "acquaintance": "熟悉",
            "close": "亲近",
            "intimate": "挚友",
        }
        return labels.get(stage, "陌生")

    def analyze_sentiment(self, text: str) -> int:
        """分析文本情感倾向，返回分值变化。

        正数表示正向情感，负数表示负向情感，0 表示中性。
        基于关键词命中数计算，大小写不敏感。
        """
        try:
            if not text:
                return 0
            text_lower = text.lower()
            # 正向关键词：每个命中 +1
            positive_keywords = [
                "谢谢", "感谢", "喜欢", "爱你", "棒", "好厉害",
                "开心", "高兴", "可爱", "棒棒",
                "thanks", "thank", "love", "great", "awesome", "nice", "good",
            ]
            # 负向关键词：每个命中 -1
            negative_keywords = [
                "讨厌", "烦", "滚", "笨", "蠢", "垃圾", "恶心", "闭嘴",
                "hate", "stupid", "shut up", "ugly", "bad", "terrible",
            ]
            score = 0
            for kw in positive_keywords:
                score += text_lower.count(kw)
            for kw in negative_keywords:
                score -= text_lower.count(kw)
            return score
        except Exception:
            return 0

    def update_affinity(self, current: int, user_text: str) -> int:
        """根据用户发言更新好感度。

        正向情感增加好感度，负向情感降低好感度，
        结果 clamp 到 [0, 100]。引擎禁用时直接返回原值。
        """
        try:
            if not self.enabled:
                return current
            sentiment = self.analyze_sentiment(user_text)
            new_affinity = current
            if sentiment > 0:
                # 最多加 3 倍 gain
                new_affinity += self.positive_gain * min(sentiment, 3)
            elif sentiment < 0:
                # 最多减 3 倍 loss
                new_affinity -= self.negative_loss * min(abs(sentiment), 3)
            # clamp 到 [0, 100]
            new_affinity = max(0, min(100, new_affinity))
            return new_affinity
        except Exception:
            return current

    def get_stage_prompt(self, stage: str) -> str:
        """返回该关系阶段对应的语气提示文本，用于注入 system_prompt。"""
        prompts = {
            "stranger": "当前与用户关系较陌生，请保持礼貌、克制，用语正式，避免过度亲昵。",
            "acquaintance": "当前与用户关系渐熟，可以适当放松语气，友好交流，但仍保持分寸。",
            "close": "当前与用户关系亲近，语气可更自然温暖，适当使用亲切称呼，展现关心。",
            "intimate": "当前与用户关系亲密（挚友），语气可非常放松热情，使用专属昵称，主动表达情感与关心，展现深层信任。",
        }
        return prompts.get(stage, "")

    def format_affinity_info(self, affinity: int, interaction_count: int = 0) -> str:
        """格式化好感度信息，用于指令回复。"""
        try:
            stage = self.get_stage(affinity)
            label = self.get_stage_label(stage)
            return (
                f"好感度: {affinity}/100\n"
                f"关系阶段: {label}\n"
                f"互动次数: {interaction_count}"
            )
        except Exception:
            return (
                f"好感度: {affinity}/100\n"
                f"关系阶段: 陌生\n"
                f"互动次数: {interaction_count}"
            )
