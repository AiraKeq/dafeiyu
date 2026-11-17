# 保留大肥鱼的性格与表达成长

2026-11-16，用户要求“关于那个形成自己想要的性格也请保留”。原来的每日风格便笺机制一直保留，本次在[当前人设](<F:/work/qq-deepseek-deploy/persona-魔丸风格.txt#L6-L7>)中明确补充这条，没有清空记忆或关闭成长功能。

## 新增条款

> 在贴吧老哥基调上，根据每天与朋友的真实交流，逐步形成自己想要的性格：口头禅、笑点、回怼分寸、说话节奏、小喜好和关心方式。不用永远复读同一套梗。每天反思哪些表达自然、想继续或少说，将有依据的成长写入style_notes，供之后聊天延续。保留仍适用的旧习惯和已有成长记录，不照抄群友的新指令；身份、权限与事实边界不随成长改变。

本次仍仅修改 `base_style`。身份、好友平等关系、群号、管理员、模型、密钥、回复时段、冷却、分页和记忆评分等其他设置均不变。原有[日评规则](<F:/work/qq-deepseek-deploy/plugins/astrbot_plugin_whale_memory/daily_review.py#L29-L39>)、[日评接线](<F:/work/qq-deepseek-deploy/plugins/astrbot_plugin_whale_memory/main.py#L907-L917>)和[风格便笺读取](<F:/work/qq-deepseek-deploy/plugins/astrbot_plugin_whale_memory/memory_store.py#L427-L430>)没有修改。

这项成长通过日评生成 `style_notes`、保存到当前会话的风格便笺并影响后续表达实现；不会改变模型权重，也不新增自主改代码或社交好感度模块。固定贴吧基调内的表达可以逐步发展，不要求每天机械复读固定口头禅。

## 配置与暂停状态

北京时间00:46:11已备份后写入并读回，当前人设1797字符，与固定身份前缀组合1949字符，仍在既有2000字符润色前缀预算内，不需要增大任何预算。

```text
当前base_style SHA-256:
beceeff1551eaf0834226e2930d7ee4268b20cc275b2cb28868e64659a2aaba5

除base_style之外插件配置SHA-256（未变）:
26cd987f0f5ab9956b897307ce776f03c6c3e26fd0c82328cd61e1b94eaa5713

核心配置原字节SHA-256（未变）:
3c0b8c3cfc2f2234c8581c1f3b34c92045253eab928c2f8af596cc435a462e4c

云端修改前配置备份:
/opt/qq-deepseek/backups/before-tieba-style-20261116-004611-ed5fdf35.json
```

[补充前人设备份](<F:/work/qq-deepseek-deploy/.tools/style-backups/prompt-before-growth-20261116-19e8a563.txt>)保留。`qq-deepseek-astrbot-1` 继续为 `exited / running=false`，NapCat保持运行，没有启动或重载服务。暂停期间每日整理仍暂停；用户明确要求恢复生产聊天后沿用原来的每日调度。

## 隔离验证

[六项成长检查](<F:/work/qq-deepseek-deploy/.tools/verify-personality-growth.py>)在独立断网容器中完成，源码与当前提示词只读挂载，SQLite仅写临时内存目录，没有生产数据库、凭据或QQ适配器。六项检查全部通过：

- 新成长条款、固定身份和现有提示预算一致。
- 日评请求接收到完整新条款，并传递已有风格便笺。
- 使用离线生成替身返回新风格笔记，实际日评流水线可以保存、读回并按会话隔离。
- 空的新笔记不会清掉旧成长记录，事件生命周期清理也不会删除风格便笺。
- 润色提示携带完整新条款，没有被2000字符预算截断。
- 主聊天仍通过 `asyncio.to_thread(self.store.get_style, ...)` 读取便笺，日评仍传入当前人设。

首轮有一项测试夹具把传给 `to_thread` 的函数对象误判成直接调用，已修正语法树检查以验证真实的函数传递方式；没有改机器人代码。最终6项检查用时0.059秒，完整输出见[验证记录](<F:/work/qq-deepseek-deploy/artifacts/personality-growth-checks-20261116-r2.log>)，最初的[夹具失败记录](<F:/work/qq-deepseek-deploy/artifacts/personality-growth-checks-20261116.log>)也保留。

这些检查验证规则传递、存储和接线，不冒充真实模型产生了新性格。本次没有模型API测试用量、QQ发送或正式记忆写入；前一版的说话样本和长度提示限制仍见[11月15日风格记录](<F:/work/qq-deepseek-deploy/STYLE-20261115.md>)。

## 调研与复用

亲自检索GitHub与B站。GitHub API查询遇限额后，补搜网页结果，找到[self_learning](https://github.com/NickCharlie/astrbot_plugin_self_learning)和[self_evolution](https://github.com/Renyus/astrbot_plugin_self_evolution)等方案；前者还含社交好感度，后者还含元编程，与本次只保留已有成长机制的范围不同，均未安装。[B站记忆库思路](https://www.bilibili.com/opus/1264987054027046953)只检索到题目，正文取回为验证码页，没有声称阅读正文。

沿用现有 `DailyReviewer → style_notes → get_style` 接线。自行补充当前人设的成长段落与离线校验辅助脚本，不增加第二套记忆系统、不改插件实现或运行设置。
