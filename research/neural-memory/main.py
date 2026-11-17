"""LLM 神经网络记忆插件主入口。

模仿人类神经网络的记忆运作方式，对 AI 记忆分层管理，
支持记忆衰退、记忆增强、自主学习、好感度转变与人格扮演增强。
"""

import os
import sys

# 将插件目录加入 sys.path，确保能正确导入 memory 包
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

from memory.store import MemoryStore
from memory.decay import DecayEngine
from memory.consolidator import Consolidator
from memory.recall import RecallEngine
from memory.learner import AutoLearner
from memory.affinity import AffinityEngine
from memory.persona import PersonaEnhancer
from memory.models import MemoryItem


@register(
    "astrbot_plugin_neural_network_memory",
    "浅月tniay",
    "让AI像人类一样记忆与遗忘——短期/长期分层存储，记忆随时间衰退、随召回增强，自动学习用户偏好，好感度随互动演变，人格扮演随关系进阶，越聊越懂你",
    "1.0.0",
)
class NeuralNetworkMemoryPlugin(Star):
    """神经网络记忆插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = os.path.dirname(__file__)
        # 初始化存储与各引擎
        self.store = MemoryStore(self.data_dir, self.config)
        self.decay_engine = DecayEngine(self.config)
        self.consolidator = Consolidator(self.config)
        self.recall_engine = RecallEngine(self.config)
        self.learner = AutoLearner(self.config)
        self.affinity_engine = AffinityEngine(self.config)
        self.persona_enhancer = PersonaEnhancer(self.config)

    async def initialize(self):
        """插件初始化。"""
        if self.config.get("enabled", True):
            logger.info("LLM神经网络记忆插件初始化完成")
        else:
            logger.info("LLM神经网络记忆插件已禁用")

    def _get_user_id(self, event) -> str:
        """安全获取用户ID。"""
        try:
            return str(event.get_sender_id())
        except Exception:
            return "unknown"

    def _get_user_text(self, event) -> str:
        """获取用户消息文本。"""
        return getattr(event, "message_str", "") or ""

    @filter.on_llm_request()
    async def on_llm_request(self, event, req):
        """LLM请求前钩子：召回记忆并注入system_prompt。"""
        if not self.config.get("enabled", True):
            return
        user_id = self._get_user_id(event)
        user_text = self._get_user_text(event)
        extras = []
        try:
            # 1. 召回记忆
            short_term, long_term = self.store.get_all_memories(user_id)
            mode = self.store.get_mode(user_id)
            recalled = self.recall_engine.recall(user_text, short_term, long_term, mode=mode)
            memory_text = self.recall_engine.format_memories_for_prompt(recalled)
            if memory_text:
                extras.append(memory_text)
            # 召回后保存（因为recall内部会boost记忆强度）
            data = self.store.get_user_data(user_id)
            self.store.save_user_data(user_id, data)
            # 2. 好感度阶段提示
            if self.config.get("affinity_enabled", True):
                affinity = self.store.get_affinity(user_id)
                stage = self.affinity_engine.get_stage(affinity)
                stage_prompt = self.affinity_engine.get_stage_prompt(stage)
                if stage_prompt:
                    extras.append(f"[关系感知] {stage_prompt}")
            # 3. 人格扮演增强
            if self.config.get("persona_enhance_enabled", True):
                affinity = self.store.get_affinity(user_id)
                _, long_term = self.store.get_all_memories(user_id)
                persona_hint = self.persona_enhancer.generate_persona_hint(affinity, long_term)
                if persona_hint:
                    extras.append(persona_hint)
            # 注入到 system_prompt
            if extras:
                req.system_prompt += "\n\n" + "\n\n".join(extras)
        except Exception as e:
            logger.error(f"on_llm_request 处理失败: {e}")

    @filter.on_llm_response()
    async def on_llm_response(self, event, resp):
        """LLM响应后钩子：自主学习、弱增强、巩固、好感度、衰减。"""
        if not self.config.get("enabled", True):
            return
        user_id = self._get_user_id(event)
        user_text = self._get_user_text(event)
        assistant_text = getattr(resp, "completion", "") or ""
        try:
            # 读取用户数据
            data = self.store.get_user_data(user_id)
            short_term = data["short_term"]
            long_term = data["long_term"]
            # 1. 自主学习：抽取新记忆
            new_memories, boosted = self.learner.process(user_text, assistant_text, short_term)
            for mem in new_memories:
                self.store.add_memory(user_id, mem)
            # 重新读取（add_memory会保存）
            data = self.store.get_user_data(user_id)
            short_term = data["short_term"]
            long_term = data["long_term"]
            # 2. 弱增强：用户发言命中标签
            self.consolidator.process_weak_boost(user_text, short_term, long_term)
            # 3. 自动巩固：短期→长期
            remaining_short, to_long = self.consolidator.process_consolidation(short_term)
            data["short_term"] = remaining_short
            data["long_term"] = long_term + to_long
            # 4. 好感度更新
            if self.config.get("affinity_enabled", True):
                current_affinity = data.get("affinity", 50)
                new_affinity = self.affinity_engine.update_affinity(current_affinity, user_text)
                data["affinity"] = new_affinity
            # 5. 互动计数+1
            data["interaction_count"] = data.get("interaction_count", 0) + 1
            # 保存
            self.store.save_user_data(user_id, data)
            # 6. 衰减检查（最后执行，独立保存）
            self.decay_engine.process_user(self.store, user_id)
        except Exception as e:
            logger.error(f"on_llm_response 处理失败: {e}")

    @filter.llm_tool(name="record_memory")
    async def record_memory(self, event, content: str, tags: str = "") -> str:
        """当你认为用户表达了值得长期记忆的信息（如兴趣、偏好、重要经历、关系、习惯等），调用此工具将其记录到神经网络记忆中，以便后续对话能回忆起来。tags为逗号分隔的标签，可选。"""
        if not self.config.get("enabled", True):
            return "记忆功能已禁用"
        if not content or not content.strip():
            return "记录失败：缺少有效内容"
        try:
            user_id = self._get_user_id(event)
            tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
            memory = MemoryItem.create_short(
                content=content.strip(),
                tags=tag_list,
                source="llm_tool",
                strength=float(self.config.get("short_term_initial_strength", 0.5))
            )
            self.store.add_memory(user_id, memory)
            return f"已记录到神经网络记忆: {content[:50]}"
        except Exception as e:
            logger.error(f"record_memory 处理失败: {e}")
            return f"记录失败: {e}"

    @filter.command("nn_memory")
    async def nn_memory(self, event: AstrMessageEvent):
        """查看神经网络记忆概览"""
        try:
            user_id = self._get_user_id(event)
            short_term, long_term = self.store.get_all_memories(user_id)
            affinity = self.store.get_affinity(user_id)
            mode = self.store.get_mode(user_id)
            stage = self.affinity_engine.get_stage(affinity)
            stage_label = self.affinity_engine.get_stage_label(stage)

            result = "=== 神经网络记忆概览 ===\n"
            result += f"记忆模式: {'完整模式' if mode == 'full' else '短期模式'}\n"
            result += f"好感度: {affinity}/100 ({stage_label})\n"
            result += f"短期记忆: {len(short_term)} 条\n"
            result += f"长期记忆: {len(long_term)} 条\n\n"

            if short_term:
                result += "--- 最近短期记忆 ---\n"
                sorted_short = sorted(short_term, key=lambda m: m.created_at, reverse=True)
                for i, m in enumerate(sorted_short[:5], 1):
                    result += f"{i}. [{int(m.strength*100)}%] {m.content[:40]}\n"
            if long_term:
                result += "\n--- 高强度长期记忆 ---\n"
                sorted_long = sorted(long_term, key=lambda m: m.strength, reverse=True)
                for i, m in enumerate(sorted_long[:5], 1):
                    result += f"{i}. [{int(m.strength*100)}%] {m.content[:40]}\n"
            if not short_term and not long_term:
                result += "暂无记忆"
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"nn_memory 处理失败: {e}")
            yield event.plain_result(f"查询失败: {e}")

    @filter.command("nn_mode")
    async def nn_mode(self, event: AstrMessageEvent):
        """切换记忆模式（完整/短期）"""
        try:
            user_id = self._get_user_id(event)
            current = self.store.get_mode(user_id)
            new_mode = "short" if current == "full" else "full"
            self.store.update_mode(user_id, new_mode)
            label = "短期记忆模式（仅使用短期记忆，减少上下文负担）" if new_mode == "short" else "完整模式（同时使用短期与长期记忆）"
            yield event.plain_result(f"记忆模式已切换为: {label}")
        except Exception as e:
            logger.error(f"nn_mode 处理失败: {e}")
            yield event.plain_result(f"切换失败: {e}")

    @filter.command("nn_recall")
    async def nn_recall(self, event: AstrMessageEvent, keyword: str):
        """手动回顾与关键词相关的记忆"""
        try:
            user_id = self._get_user_id(event)
            short_term, long_term = self.store.get_all_memories(user_id)
            recalled = self.recall_engine.recall(keyword, short_term, long_term, mode="full")
            if not recalled:
                yield event.plain_result(f"未找到与「{keyword}」相关的记忆")
                return
            result = f"=== 与「{keyword}」相关的记忆 ===\n"
            for i, m in enumerate(recalled, 1):
                result += f"{i}. [{int(m.strength*100)}%] {m.content}\n"
                if m.tags:
                    result += f"   标签: {', '.join(m.tags)}\n"
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"nn_recall 处理失败: {e}")
            yield event.plain_result(f"回顾失败: {e}")

    @filter.command("nn_consolidate")
    async def nn_consolidate(self, event: AstrMessageEvent):
        """手动触发记忆巩固（短期→长期）"""
        try:
            user_id = self._get_user_id(event)
            data = self.store.get_user_data(user_id)
            short_term = data["short_term"]
            remaining, to_long = self.consolidator.process_consolidation(short_term)
            data["short_term"] = remaining
            data["long_term"] = data["long_term"] + to_long
            self.store.save_user_data(user_id, data)
            count = len(to_long)
            if count > 0:
                yield event.plain_result(f"已巩固 {count} 条短期记忆为长期记忆")
            else:
                yield event.plain_result("没有需要巩固的短期记忆（需召回3次或强度达80%）")
        except Exception as e:
            logger.error(f"nn_consolidate 处理失败: {e}")
            yield event.plain_result(f"巩固失败: {e}")

    @filter.command("nn_forget")
    async def nn_forget(self, event: AstrMessageEvent, keyword: str):
        """手动遗忘包含关键词的记忆"""
        try:
            user_id = self._get_user_id(event)
            data = self.store.get_user_data(user_id)
            short_term = data["short_term"]
            long_term = data["long_term"]
            removed = []
            for m in list(short_term):
                if keyword in m.content:
                    removed.append(m)
                    short_term.remove(m)
            for m in list(long_term):
                if keyword in m.content:
                    removed.append(m)
                    long_term.remove(m)
            self.store.save_user_data(user_id, data)
            if removed:
                result = f"已遗忘 {len(removed)} 条包含「{keyword}」的记忆:\n"
                for i, m in enumerate(removed, 1):
                    result += f"{i}. {m.content[:40]}\n"
            else:
                result = f"未找到包含「{keyword}」的记忆"
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"nn_forget 处理失败: {e}")
            yield event.plain_result(f"遗忘失败: {e}")

    @filter.command("nn_affinity")
    async def nn_affinity(self, event: AstrMessageEvent):
        """查看好感度与关系阶段"""
        try:
            user_id = self._get_user_id(event)
            affinity = self.store.get_affinity(user_id)
            data = self.store.get_user_data(user_id)
            interaction_count = data.get("interaction_count", 0)
            info = self.affinity_engine.format_affinity_info(affinity, interaction_count)
            yield event.plain_result(f"=== 好感度信息 ===\n{info}")
        except Exception as e:
            logger.error(f"nn_affinity 处理失败: {e}")
            yield event.plain_result(f"查询失败: {e}")

    async def terminate(self):
        """插件卸载时的清理工作。"""
        logger.info("LLM神经网络记忆插件已卸载")
