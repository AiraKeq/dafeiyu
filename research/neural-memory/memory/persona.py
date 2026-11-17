"""人格扮演增强引擎。

结合好感度阶段、长期记忆摘要与用户偏好，
生成人格扮演增强提示，注入 system_prompt 以提升角色扮演一致性。
"""

import logging
from typing import List

from .models import MemoryItem
from .affinity import AffinityEngine

logger = logging.getLogger(__name__)


class PersonaEnhancer:
    """人格扮演增强器：综合好感度与记忆生成扮演提示。"""

    # 用户偏好关键词，用于识别长期记忆中的偏好信息
    PREFERENCE_KEYWORDS = ["喜欢", "偏好", "爱好", "习惯", "最爱", "偏爱", "倾向"]

    def __init__(self, config: dict):
        self.config = config or {}
        self.enabled = bool(config.get("persona_enhance_enabled", True))
        self.affinity_engine = AffinityEngine(config)

    def summarize_long_term_memories(self, long_term: List[MemoryItem], max_count: int = 5) -> str:
        """摘要长期记忆，按强度降序取前 max_count 条。

        每条记忆格式为一行 "- {content}"，无记忆时返回空串。
        """
        try:
            if not long_term:
                return ""
            # 按 strength 降序排序
            sorted_items = sorted(long_term, key=lambda m: m.strength, reverse=True)
            lines = []
            for item in sorted_items[:max_count]:
                lines.append(f"- {item.content}")
            return "\n".join(lines)
        except Exception:
            return ""

    def extract_user_preferences(self, long_term: List[MemoryItem]) -> str:
        """从长期记忆中提取用户偏好信息，最多 5 条。

        筛选条件：tags 含 "preference"，或 source 为 "auto_learn" 且 content 含偏好关键词。
        """
        try:
            if not long_term:
                return ""
            lines = []
            for item in long_term:
                tags = item.tags or []
                has_tag = "preference" in tags
                is_auto = item.source == "auto_learn"
                content_has_kw = any(kw in item.content for kw in self.PREFERENCE_KEYWORDS)
                if has_tag or (is_auto and content_has_kw):
                    lines.append(f"- {item.content}")
                if len(lines) >= 5:
                    break
            return "\n".join(lines)
        except Exception:
            return ""

    def generate_persona_hint(self, affinity: int, long_term: List[MemoryItem], recent_style_hint: str = "") -> str:
        """生成人格扮演增强提示文本。

        组装好感度阶段语气、长期记忆摘要、用户偏好与近期风格提示，
        引擎禁用时返回空串。
        """
        try:
            if not self.enabled:
                return ""
            # 获取好感度阶段与语气提示
            stage = self.affinity_engine.get_stage(affinity)
            stage_prompt = self.affinity_engine.get_stage_prompt(stage)
            # 获取长期记忆摘要与用户偏好
            memory_summary = self.summarize_long_term_memories(long_term)
            preferences = self.extract_user_preferences(long_term)

            parts = ["[人格扮演增强]", stage_prompt]

            if memory_summary:
                parts.append("你与该用户的历史记忆摘要：")
                parts.append(memory_summary)

            if preferences:
                parts.append("该用户的已知偏好：")
                parts.append(preferences)

            if recent_style_hint:
                parts.append(recent_style_hint)

            parts.append("请在保持原有人设基础上，自然地融入以上信息，使角色扮演更加一致和深入。")
            return "\n".join(parts)
        except Exception:
            return ""
