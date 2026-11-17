# 鲸鱼的记忆与性格

为这只 QQ 群 AI 朋友提供说话基调、每日表达成长、群聊记录和双评分事件记忆。使用 Python 标准库与 SQLite，复用 AstrBot 已配置的 DeepSeek，不需要额外服务或本地嵌入模型。

## 当前行为

- 群由部署配置 `group_ids` 指定，当前为 `1000000004`，管理员 `1000000002`；群和管理员私聊的记忆相互隔离。
- 固定名字是“大肥鱼”，主人QQ为 `1000000002`；身份每次写入模型请求，日常风格成长和记忆清理不会移除。用平台发言者QQ号识别主人。
- 活泼、可爱、体贴；朋友难过时先理解和安慰，合适时善意玩笑；每个聊天的人都是好朋友。
- 每天的聊天会形成新的口头禅、幽默和表达习惯，稳定的温柔基调持续保留。
- 北京时间 **08:00–24:00** 回复；夜里继续记录，保持安静。
- v1.2.0：在回复时段内，@机器人，或消息出现“大肥鱼”“鲸鱼娘”“鲸鲸”“大鲸鱼”“小鲸鱼”时立即开始处理，绕过普通回复冷却。正在生成或发送的旧普通回复会被取消；连续点名的请求不重复认领同一批消息。
- 普通群消息**不再随机抽取**：没有冷却时直接处理；冷却中先积累，到期自动创建新的原生事件，结合这段聊天和上一段回复回应，无需再发一条消息触发。
- **10 秒从完整回复发送结束起算**，没有每小时或每天次数上限。模型生成与必要分段发送还需要时间，不能将此理解为消息恰好10秒后送达。
- 没有待回复的新消息时保持安静；上一段回复仅作衔接背景，机器人自己的输出不启动无限自聊。
- 夜间仍静默；发送逐段检查时间，并在回复时段截止时取消尚未完成的发送等待。已经被 QQ 接受的请求无法由本地取消撤回。
- 待回复队列独立于每日记忆整理，原子落盘、重启恢复、按会话隔离；失败不将输入当作已回复，稍后重试。普通回复开关关闭时，称呼/@仍可在允许时段内唤醒。
- 队列每次最多提供32条、共6000字待回复摘录，单条最多4000字；超预算的消息保留到下一批。去重保留待处理消息和最近2048个已完成ID，不承诺14天消息ID去重。
- v1.3.0：日常能20字以内说清就尽量一条。分段以60/30/10为1/2/3段的基础偏好，结合场景、完整度和自然停顿评分；近分才轻微随机，不强制凑比例。每段最多20字，必要长内容可超过3段，不截断。
- 分段评分与事件记忆I/R独立，不加额外模型调用。成功发送后只记录场景、段数、字数、候选分数等元数据，不将每段拆成独立记忆；[详细规则](<F:/work/qq-deepseek-deploy/SEGMENTATION.md>)。
- 分段使用AstrBot原生能力，通常间隔0.8–1.8秒。发送后将完整回复作为一条记忆记录，不把每个气泡拆成独立事件；逐段检查午夜静默。

## 两个评分

每个事件的重要性 `I` 独立存储，范围 0–10。时间新近度 `R` 根据事件发生时间动态计算：

```text
R = 10 × 2 ^ (−已经过去的天数 / 4)
```

| 距今 | R | 事件细节 |
| --- | --- | --- |
| 0–2 天 | 最近，最高 10 | 最多 640 字详情 |
| 3–6 天 | 随时间衰减 | 最多 320 字 |
| 7–13 天 | 继续衰减 | 最多 160 字 |
| 达到 14 天 | 独立继续计算 | I≥8 保存最多 160 字核心摘要；其余事件移除 |

长期约定、持续喜好、帮助与关心、重要经历通常评分较高。悲伤或困难的经历也可能很重要，不能因情绪负面而打低分。模型必须引用当前批次的真实消息来源，不能把机器人想象的故事当作朋友的事实。

14 天外的重要事件存入摘要档案；只有与当前话题相关时才召回。不会把所有记忆一次放进上下文。事件召回最多 2200 字，近期聊天最多 2000 字；历史对话也会截断。

## 每日整理

默认每天北京时间 **00:20** 整理此前的未处理消息。按会话、日期分批提取事件、重要分和风格笔记。积压会继续分批处理；模型失败或 JSON 校验失败时保留原文并稍后重试。

只有已成功整理、超过保留期的原始消息才会清理。原始记录默认保留 14 天；未成功整理的旧原文不会因为到期而直接丢失。

记录分为用户原话与机器人回复，不保存模型思考内容。群聊队列回复只在所有原生分段的协议发送完成后确认一次，并将完整回答作为一条记忆保存；发送错误、过期响应或夜间阻断不能假确认。分段前保留完整文本，避免边缘空白处理影响记忆。协议调用成功不等于客户端已显示或已读，不作最终送达保证。

## 设置和命令

在 AstrBot「插件 → 鲸鱼的记忆与性格 → 配置」调整风格、称呼、时间、普通回复开关、冷却和记忆阈值。v1.2.0已移除接话概率设置。风格描述留空时使用内置基调，修改风格只改变表达方式，固定身份继续保留。管理员在当前会话可使用：

```text
/鲸鱼记忆 状态
/鲸鱼记忆 整理
/鲸鱼记忆 查询 共同的约定
/鲸鱼记忆 忘记 事件编号
/鲸鱼风格
/鲸鱼风格 新的稳定说话基调
```

“整理”也包含今天已经产生的记录，适合提前验收。自动整理不会给群发消息。

存储目录是 AstrBot 的 `data/plugin_data/whale_memory/`，其中 `memory.sqlite3` 存储原文、事件和风格快照，`review-state.json` 存储整理进度，`reply-queue.json` 存储待回复消息、去重ID和冷却状态。活跃模型/发送流程每30秒续租；尚未开始的原生事件有180秒租约回收保障。热重载会取消并等待旧流程退出，避免继续占用原生会话锁。按私有聊天数据备份，不提交到公共仓库。

## 调研与实现边界

部署前查阅了 GitHub 的 LivingMemory、Iris Memory、Neural Network Memory、SQLite Memory System，以及 B 站的 AstrBot 记忆教程。LivingMemory 和 Iris 提供通用检索/生命周期能力；Neural Network Memory 使用单一强度递减，并包含好友阶段。这些现成规则没有完整覆盖本次要求的“两个独立评分 + 精确14天边界 + 重要旧事件只保留核心 + 所有人平等为朋友”。因此继续使用现成 AstrBot 框架，单独实现本扩展；没有复制上述插件源码。

参考入口：

- [AstrBot 官方消息事件文档](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
- [Iris Memory](https://github.com/leafliber/astrbot_plugin_iris_memory)
- [Neural Network Memory](https://github.com/tniany/astrbot_plugin_neural_network_memory)
- [SQLite Memory System](https://github.com/Zxin-Pro/astrbot_plugin_memory_system)
- [B站：三分钟教会你让你的 AstrBot 网友拥有记忆](https://www.bilibili.com/list/31221710?bvid=BV1BcXNBkEgW&oid=116299577559996)

这里自己编写的部分为 SQLite 存储、评分与生命周期、每日整理和风格自省、AstrBot 钩子接入、配置及测试。

v1.1.0的短消息分段查阅了[AstrBot官方配置说明](https://docs.astrbot.app/dev/astrbot-config.html)与[CurrentCortex](https://github.com/backrooms-yrc/astrbot_plugin_currentcortex)。原生分段已经能满足需求，因此复用原生发送流程，只配置20字规则、间隔和阈值；没有引入多功能分段插件。B站本轮未找到直接匹配的专门分段教程。

### v1.2.0 续办范围

本轮读取了 GPT/Codex 同名会话的最后需求，沿用其未上线的 v1.2.0 草稿与现有 AstrBot + NapCat 部署，没有另装机器人、声音服务或决策模型。

亲自搜索 GitHub 与 B 站后，核对了 [Debounce](https://github.com/advent259141/astrbot_plugin_debounce)、[Message Merger](https://github.com/ahuai114514/astrbot_plugin_message_merger) 与 [AstrBot + NapCat 教程](https://www.bilibili.com/video/BV17RnU6pE62/)。Debounce判断用户是否说完，Message Merger侧重同一用户连续输入的取消合并；本次需要整个群按完整回复结束冷却、称呼插队、到期主动派发和持久化恢复，所以没有直接叠装它们，也没有复制其源码。

自行补齐的部分为[回复队列](<F:/work/qq-deepseek-deploy/plugins/astrbot_plugin_whale_memory/reply_queue.py>)的原子事务、并发快照互斥、外部回复重新冷却，以及[原生钩子接线](<F:/work/qq-deepseek-deploy/plugins/astrbot_plugin_whale_memory/main.py>)的真实任务抢占、发送异常确认、午夜截止、活跃心跳、热重载回收和原生命令保护。新增[53项队列测试](<F:/work/qq-deepseek-deploy/plugins/astrbot_plugin_whale_memory/tests/test_reply_queue.py>)和[原生安全演练](<F:/work/qq-deepseek-deploy/plugins/astrbot_plugin_whale_memory/tests/rehearsal_queue_safety.py>)；演练只使用独立数据、假消息和假模型，不发送 QQ 消息。正式上线状态与实际核验记录见[项目状态](<F:/work/qq-deepseek-deploy/STATUS.md>)。
