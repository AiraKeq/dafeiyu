"""记忆回顾（召回）引擎。

模仿人类神经网络的记忆召回过程，在用户发言时根据关键词与标签
相关性从短期/长期记忆中召回 Top-N 条记忆，对被选中的记忆执行
召回增强，并将结果格式化为可注入 system_prompt 的文本。
"""

import logging
import re
import time
from typing import List, Optional

from .models import MemoryItem
from .consolidator import Consolidator

logger = logging.getLogger(__name__)


class RecallEngine:
    """记忆回顾（召回）引擎。

    根据用户当前发言对候选记忆进行相关性评分，按得分降序选取
    Top-N 条记忆并触发召回增强，支持完整模式（短期+长期）与
    短期模式（仅短期）两种召回范围。
    """

    def __init__(self, config: dict):
        """初始化召回引擎，从配置字典读取 Top-N 等参数。

        Args:
            config: 配置字典，缺失项使用默认值。
        """
        self.config = config or {}
        # 召回返回的最大记忆条数
        self.top_n = int(config.get("recall_top_n", 5))
        # 巩固器实例，用于对选中记忆执行召回增强
        self.consolidator = Consolidator(config)

    def tokenize(self, text: str) -> List[str]:
        """将文本分词为关键词列表。

        中文按 2-3 字符滑窗切分（生成 2-gram 与 3-gram），
        英文按空格与标点分割为单词；统一转小写，过滤空串与纯标点，
        返回去重后（保持首次出现顺序）的关键词列表。

        Args:
            text: 待分词的原始文本。

        Returns:
            去重后的关键词列表，可能为空。
        """
        if not text:
            return []
        # 统一转小写，保证后续匹配大小写不敏感
        text = text.lower()
        tokens: List[str] = []
        # 提取连续中文片段，生成 2-gram 与 3-gram
        for seg in re.findall(r"[\u4e00-\u9fff]+", text):
            # 2-gram 滑窗
            for i in range(len(seg) - 1):
                tokens.append(seg[i : i + 2])
            # 3-gram 滑窗
            for i in range(len(seg) - 2):
                tokens.append(seg[i : i + 3])
        # 提取英文单词（字母数字序列）
        tokens.extend(re.findall(r"[a-z0-9]+", text))
        # 去重并保持首次出现顺序，过滤空串与纯标点
        seen = set()
        result: List[str] = []
        for tok in tokens:
            if not tok or tok in seen:
                continue
            # 兜底过滤纯标点/空白（中文滑窗与英文提取后理论上不会出现）
            if re.fullmatch(r"[\s\W]+", tok):
                continue
            seen.add(tok)
            result.append(tok)
        return result

    def score_relevance(self, memory: MemoryItem, query_tokens: List[str]) -> float:
        """计算记忆与查询关键词的相关性得分。

        评分规则：
          - 标签匹配：memory.tags 中每个 tag 若作为子串出现在任意
            query_token 中，+2.0 分（每个 tag 至多计一次）。
          - 内容匹配：每个 query_token 作为子串出现在 memory.content
            中，+1.0 分。
          - 强度加权：最终得分乘以 (0.5 + 0.5 * memory.strength)，
            强度越高权重越大。

        Args:
            memory: 待评分的记忆对象。
            query_tokens: 由 tokenize 生成的查询关键词列表。

        Returns:
            相关性得分，无匹配时为 0.0。
        """
        if not query_tokens:
            return 0.0
        score = 0.0
        content_lower = memory.content.lower()
        tags_lower = [t.lower() for t in memory.tags]
        # 标签匹配：每个 tag 命中任意 query_token 即 +2.0，单 tag 只计一次
        for tag in tags_lower:
            if not tag:
                continue
            for qt in query_tokens:
                if tag in qt:
                    score += 2.0
                    break
        # 内容匹配：每个 query_token 出现在 content 中即 +1.0
        for qt in query_tokens:
            if qt in content_lower:
                score += 1.0
        # 强度加权：强度越高权重越大（范围 0.5 ~ 1.0）
        score *= 0.5 + 0.5 * memory.strength
        return score

    def recall(
        self,
        user_text: str,
        short_term: List[MemoryItem],
        long_term: List[MemoryItem],
        mode: str = "full",
        now: Optional[float] = None,
    ) -> List[MemoryItem]:
        """主召回方法：根据用户发言召回相关记忆。

        根据 mode 决定搜索范围：mode == "short" 仅在短期记忆中搜索，
        mode == "full"（默认）在短期与长期记忆中搜索。对每条候选记忆
        计算相关性得分，过滤得分为 0 者，按得分降序取前 self.top_n 条，
        并对选中的记忆执行召回增强。

        Args:
            user_text: 用户当前发言文本。
            short_term: 短期记忆列表。
            long_term: 长期记忆列表。
            mode: 召回模式，"short" 仅短期，"full" 短期+长期。
            now: 当前时间戳，用于召回增强；为 None 时由巩固器自取。

        Returns:
            被选中并已执行召回增强的记忆列表，按相关性降序排列，
            可能为空。
        """
        # 根据模式确定候选搜索范围
        if mode == "short":
            candidates = list(short_term)
        else:
            # 默认完整模式：短期 + 长期
            candidates = list(short_term) + list(long_term)
        # 对用户发言分词
        query_tokens = self.tokenize(user_text)
        # 计算每条候选记忆的得分，过滤得分为 0 者
        scored = []
        for memory in candidates:
            score = self.score_relevance(memory, query_tokens)
            if score > 0:
                scored.append((score, memory))
        # 按得分降序排序，取前 top_n 条
        scored.sort(key=lambda item: item[0], reverse=True)
        selected = scored[: self.top_n]
        # 对选中的记忆执行召回增强并收集结果
        result: List[MemoryItem] = []
        for _score, memory in selected:
            try:
                self.consolidator.boost_recall(memory, now)
            except Exception as e:
                logger.error(f"召回增强记忆失败: {e}")
            result.append(memory)
        return result

    def format_memories_for_prompt(self, memories: List[MemoryItem]) -> str:
        """将召回的记忆格式化为可注入 system_prompt 的文本。

        格式：
            [神经网络记忆回顾] 以下是与当前对话相关的记忆：
            1. {content1}（强度: {strength百分比}%）
            2. {content2}（强度: {strength百分比}%）
            ...

        strength 显示为百分比整数（如 0.75 -> 75%）。
        记忆列表为空时返回空字符串 ""。

        Args:
            memories: 召回得到的记忆列表。

        Returns:
            格式化后的提示文本，空列表返回空串。
        """
        if not memories:
            return ""
        lines = ["[神经网络记忆回顾] 以下是与当前对话相关的记忆："]
        for idx, memory in enumerate(memories, start=1):
            # 强度转为百分比整数
            strength_pct = int(round(memory.strength * 100))
            lines.append(f"{idx}. {memory.content}（强度: {strength_pct}%）")
        return "\n".join(lines)
