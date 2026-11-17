"""自主学习引擎。

从对话中自动抽取可记忆信息，分类、评分、生成标签并去重，
对新信息创建短期记忆，对重复信息弱增强已有记忆，
模仿人类神经网络中记忆的形成与强化过程。
"""

import logging
import re
from typing import List, Optional, Tuple

from .models import MemoryItem
from .consolidator import Consolidator

logger = logging.getLogger(__name__)


class AutoLearner:
    """自主学习引擎，负责从对话中抽取并组织新记忆。

    工作流程：抽取候选记忆 -> 去重 -> 对新记忆创建短期记忆，
    对重复记忆调用 Consolidator 做弱增强，强化已有记忆。
    """

    def __init__(self, config: dict):
        self.config = config or {}
        self.enabled = bool(config.get("auto_learn_enabled", True))
        self.short_term_initial_strength = float(config.get("short_term_initial_strength", 0.5))
        self.consolidator = Consolidator(config)

    def generate_tags(self, content: str) -> List[str]:
        """根据内容自动生成标签。

        提取内容中的关键词作为标签：
        - 中文连续片段生成 2-gram 与 3-gram
        - 英文单词（长度 > 2，统一小写）
        最多保留 5 个标签，按出现顺序去重。
        """
        if not content:
            return []
        tags: List[str] = []
        seen = set()

        # 中文连续片段：生成 2-gram 与 3-gram
        for seg in re.findall(r"[\u4e00-\u9fa5]+", content):
            # 2-gram
            for i in range(len(seg) - 1):
                gram = seg[i:i + 2]
                if gram not in seen:
                    seen.add(gram)
                    tags.append(gram)
            # 3-gram
            for i in range(len(seg) - 2):
                gram = seg[i:i + 3]
                if gram not in seen:
                    seen.add(gram)
                    tags.append(gram)

        # 英文单词：长度 > 2，统一小写以便去重
        for word in re.findall(r"[a-zA-Z]+", content):
            token = word.lower()
            if len(token) > 2 and token not in seen:
                seen.add(token)
                tags.append(token)

        # 最多保留 5 个标签
        return tags[:5]

    def classify_content(self, content: str) -> str:
        """分类记忆内容类型，返回类别字符串。

        依次检测（大小写不敏感）：
        - preference：偏好表达
        - fact：事实陈述或含数字日期
        - event：事件描述
        - general：其他
        """
        if not content:
            return "general"
        # 偏好类
        if re.search(r"喜欢|讨厌|爱|恨|偏好|想要|希望|prefer|like|hate|love", content, re.IGNORECASE):
            return "preference"
        # 事实类：关键词或含数字日期
        if re.search(r"是|叫|住在|工作|职业|我是|name|is|age", content, re.IGNORECASE) or re.search(r"\d", content):
            return "fact"
        # 事件类
        if re.search(r"昨天|今天|上周|去了|做了|发生|happened|did|went", content, re.IGNORECASE):
            return "event"
        # 其他
        return "general"

    def score_importance(self, content: str, category: str) -> float:
        """计算初始重要性分数（0-1），影响记忆初始强度。

        规则：
        - 基础分：preference=0.6, fact=0.7, event=0.5, general=0.4
        - 含疑问词（?？/怎么/为什么/what/why）减 0.1
        - 内容长度 > 50 字符加 0.1
        - 含人名/地名（大写英文或先生/小姐等称呼）加 0.05
        最终 clamp 到 [0.3, 0.9]。
        """
        base_map = {"preference": 0.6, "fact": 0.7, "event": 0.5, "general": 0.4}
        score = base_map.get(category, 0.4)

        # 含疑问词减分（疑问通常不需记忆）
        if re.search(r"[?？]|怎么|为什么|what|why", content, re.IGNORECASE):
            score -= 0.1
        # 内容长度 > 50 字符加分（信息量较大）
        if len(content) > 50:
            score += 0.1
        # 含人名/地名加分（简单检测：大写英文或常见称呼）
        if re.search(r"[A-Z]", content) or re.search(r"先生|小姐", content):
            score += 0.05

        # clamp 到 [0.3, 0.9]
        score = max(0.3, min(0.9, score))
        return score

    def extract_memories(self, user_text: str, assistant_text: str = "") -> List[MemoryItem]:
        """从一轮对话中抽取可记忆信息。

        按句号/问号/感叹号/换行分割用户发言为句子，对匹配自我陈述、
        偏好表达或数字+单位事实陈述的句子创建短期记忆。
        跳过纯疑问句与过短句子（< 4 字符），最多抽取 5 条。
        """
        if not user_text:
            return []

        # 自我陈述模式
        self_statement = r"我喜欢|我讨厌|我爱|我是|我叫|我住|我的|我有|我会"
        # 偏好表达模式
        preference_expr = r"喜欢|讨厌|爱|恨"
        # 数字 + 单位的事实陈述（如年龄、日期）
        fact_with_number = r"\d+\s*(?:岁|年|月|日|天|小时|分钟|秒|kg|cm|km|斤|公斤|个|次|块|元|米|度|磅|英里|公里)"

        # 按句末标点与换行切分，保留尾标点以便判断疑问句
        raw_sentences = re.findall(r"[^。？！.!?\n]+[。？！.!?\n]*", user_text)

        memories: List[MemoryItem] = []
        for raw in raw_sentences:
            sentence = raw.strip()
            # 跳过过短句子
            if len(sentence) < 4:
                continue
            # 跳过纯疑问句（以 ?/？ 结尾且不含自我陈述）
            if sentence.endswith(("?", "？")) and not re.search(self_statement, sentence):
                continue
            # 匹配可记忆模式
            if not (
                re.search(self_statement, sentence)
                or re.search(preference_expr, sentence)
                or re.search(fact_with_number, sentence)
            ):
                continue

            # 去除尾部标点作为记忆内容
            content = sentence.rstrip("。？！.!?")
            # 分类、评分、生成标签
            category = self.classify_content(content)
            importance = self.score_importance(content, category)
            tags = self.generate_tags(content)
            memory = MemoryItem.create_short(
                content=content,
                tags=tags,
                source="auto_learn",
                strength=importance,
            )
            memories.append(memory)
            # 最多抽取 5 条
            if len(memories) >= 5:
                break
        return memories

    @staticmethod
    def _two_grams(text: str) -> set:
        """生成文本的字符级 2-gram 集合，用于 Jaccard 相似度计算。"""
        text = text or ""
        if len(text) < 2:
            # 单字符直接作为唯一元素，便于精确匹配
            return {text} if text else set()
        return {text[i:i + 2] for i in range(len(text) - 1)}

    def is_duplicate(self, new_content: str, existing_memories: List[MemoryItem]) -> Optional[MemoryItem]:
        """检查新内容是否与已有记忆高度相似。

        判定条件（满足任一即视为重复）：
        - 新内容与已有记忆 content 的 2-gram Jaccard 相似度 > 0.6
        - 新内容完全包含已有记忆的 content，或反过来（需较短一方长度 >= 4）
        返回匹配的已有记忆，无匹配返回 None。
        """
        if not new_content or not existing_memories:
            return None

        new_grams = self._two_grams(new_content)
        for mem in existing_memories:
            existing_content = mem.content or ""
            if not existing_content:
                continue

            # 完全包含关系（较短一方长度 >= 4，避免退化匹配）
            shorter_len = min(len(new_content), len(existing_content))
            if shorter_len >= 4 and (
                new_content in existing_content or existing_content in new_content
            ):
                return mem

            # 2-gram Jaccard 相似度
            existing_grams = self._two_grams(existing_content)
            if new_grams and existing_grams:
                intersection = new_grams & existing_grams
                union = new_grams | existing_grams
                similarity = len(intersection) / len(union) if union else 0.0
                if similarity > 0.6:
                    return mem
        return None

    def process(
        self,
        user_text: str,
        assistant_text: str,
        short_term: List[MemoryItem],
    ) -> Tuple[List[MemoryItem], List[MemoryItem]]:
        """完整自主学习流程。

        - 若自主学习被禁用，返回 ([], [])
        - 抽取候选记忆，对每条做去重判断：
          - 重复：对已有记忆执行弱增强（每条最多增强一次），不添加新记忆
          - 不重复：加入新记忆列表
        返回二元组：(待添加的新记忆列表, 被弱增强的已有记忆列表)。
        被弱增强的已有记忆为 short_term 中原地修改的对象，调用方需负责持久化。
        """
        if not self.enabled:
            return ([], [])

        new_memories: List[MemoryItem] = []
        boosted_memories: List[MemoryItem] = []

        candidates = self.extract_memories(user_text, assistant_text)
        for mem in candidates:
            duplicate = self.is_duplicate(mem.content, short_term)
            if duplicate is not None:
                # 重复：弱增强已有记忆（同一记忆最多增强一次）
                already_boosted = any(duplicate is b for b in boosted_memories)
                if not already_boosted:
                    self.consolidator.weak_boost_memory(duplicate)
                    boosted_memories.append(duplicate)
            else:
                # 不重复：加入新记忆列表
                new_memories.append(mem)

        return (new_memories, boosted_memories)
