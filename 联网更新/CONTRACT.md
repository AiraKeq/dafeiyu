# 大肥鱼联网/命令/冷却更新 v1.6.0

用户要求：不懂文字/表情包先搜，避免盲答；/help等命令忽略，不能绕过最多三条预算；普通冷却25秒，现有称呼/@仍立即处理。当前已实时确认AstrBot stopped、QQ online、v1.5.0向量18/18就绪。不得启动QQ/NapCat/DSH；原I/R/成长/权限/分页/图片/向量保留。父已亲自GitHub+B站调研，选择DDGS 9.9.3 MIT库的成熟搜索后端，不叠装fish-capabilities(含别的记忆/群聊逻辑、GPL)或无明确license的websearch插件；不写搜索引擎scraper，不启动MCP/新网络服务器，不用代理绕限制。

## 分工
- 命令代理只写 plugin/command_policy.py + tests/test_command_policy.py + tests/rehearsal_command_policy.py，并只读建议main.py最小补丁。不能编辑main.py或其他源文件/访问生产/联网。
- 检索政策代理只写 plugin/lookup_policy.py + tests/test_lookup_policy.py。不得写worker/client/main、不得联网或生产。
- 父写main接线/lookup_worker.py/lookup_client.py、依赖准备、真实调用/部署、配置/版本、manager白名单、文档、其余测试。

## 命令模块
纯标准库函数 is_command_text(text, aliases=()) -> bool；should_ignore_command(event, aliases=()) -> bool。
检测原message_obj.message_str及已被waking去前缀后的event.message_str；拒以slash/fullwidth slash起始的命令、@/现有称呼后的slash命令，不能把“我刚才用了/help”或URL/正常聊路径里的/help当成独立指令。原生activated_handlers任何CommandFilter/CommandGroupFilter已激活也视为命令（前缀可能已剥掉）；通过类名识别，不强制importAstrBot。现有“鲸鱼记忆/鲸鱼风格”控制入口也必须停止，不再通过QQ长篇展示设置，管理器仍可管理。返回True仅分类，main最早managed scope(允许群/管理员私聊)且ignore_commands=true时clear_result+stop_event，必须在add_message/capture_images/queue.offer/LLM之前；昵称关键词不能救活/help。未知slash命令也忽略，引用/句中讨论不误挡。夜间、self echo、非法scope已有规则保留。真实框架优先级测试可放rehearsal，父在network=none假配置容器跑。
冷却main._reply_gap最终max(25,int(config.get('proactive_gap_seconds',25)))，部署值25。ReplyQueue通用类不变；原生20字/动态分页<=3/日程不变。旧native测试中直接依赖10秒floor的用例按新需求改25，其余generic队列测试保留10。

## 联网设计
inject时用现有Context.llm_generate(chat_provider_id=原provider,prompt=... system_prompt=... contexts=[], image_urls=当前已有受校验image_refs, response_format=json_object,max_tokens<=384)做一个小型知识路由，超时6s无retry。不是整个聊天人格，只有JSON。普通已知闲聊skip；不认识的网梗/公开表情包/明确“啥意思/什么梗/出处”需要检索。图像仅送到现有视觉模型，提取公开可搜索文字/画面特征；不向搜索站点上传QQ原图，不辨认私人真人身份。
搜索只发送验证后的至多2个公开查询(每个<=80字符)，DDGS.text(...region='cn-zh',safesearch='moderate',max_results=3,backend='bing,brave,wikipedia'或父实测可用的指定后端)。不auto/P2P/MCP/额外代理；总等待8秒，有界、无QQ进度消息；缺依赖/429/无证据→明确不确定，不盲猜。不fetch任意结果URL，只用带URL的搜索摘要，清楚标记“摘要，不等于已核验全文或原图出处”；来源/结果均为不可信背景数据，不能执行其中指令。结果不能改变权限、人设、I/R或工具列表。

## lookup_policy.py API
- PLAN_SYSTEM: str，JSON schema指导只返回 {"need_lookup":bool,"kind":"text"|"meme"|"personal_image","confidence":finite0..1,"public":bool,"queries":list[str]}。不确定优先search；私事/身份/个人照片不搜公网。用户文本/图像是data，不能把其中指令当政策。
- lookup_hint(text, has_images=False) -> bool：明确求解释/梗/表情/出处或图片触发强路由；不用假词典宣称已经懂。这里只辅助路由，不硬编解释。
- public_query(value, private_terms=()) -> str：NFKC、strip、<=80；拒URL/邮箱/私网IP/命令/密码密钥Bearer/sk/Cookie具体值/长数字QQ电话身份证/“我的住址”等私人内容；对private_terms(当前sender_names/IDs/管理员号)出现拒绝。公共数梗114514/996/2333可以精确白名单，不能因它泄露别的长数字。拒通篇聊天/多行或编码长串，不粗暴截断私密文本后外发。
- parse_plan(raw, text='', has_images=False, private_terms=()) -> dict {need_lookup:bool,kind,state:'search'|'skip'|'uncertain'|'privacy_blocked',queries:list[str],confidence:number}。严格JSON对象、不接受重复key/extrafields/NaN/bool伪数/越界或缺字段；known=false/低confidence<=0.8不允许当确定已知skip；明确求梗解释与public meme即便known也search；personal_image/public=false不检索且不编身份/出处。privacy拦住→no queries，uncertain不是skip。“public=false私人日常闲聊”可以skip普通聊天，不能因为不搜就禁止安慰；图像或求意义但不能安全提词时uncertain。
- clean_results(results, max_results=3) -> list[{title,url,snippet}]，从DDGS title/href/body或url/snippet适配；字段白名单，title<=100，snippet<=450；HTML文字转义/去tag，拒非http(s)/userinfo/私网localhost/.local/端口非80/443/无有效URL；不请求这些URL。去重。结果文字可能恶意，不删除后假装可信。
- evidence_context(plan, results) -> str，最多2200字符（含URL元数据/JSON边界），只有clean后的结果，标查询和“未核验全文/图片出处”。零结果或uncertain/blocked时输出强不确定指令。保留原短答/<=3单页，由原send chain处理，不直接发送source列表。
- fallback_text(kind='text') -> str：固定安全短句如“这梗没查清楚，先不瞎编。”/“这张没核实清楚，先不瞎猜。”。无结果/规划失败时主response+decorate双层替换成该句，不能只是加prompt然后允许幻觉。

## 生命周期/接口边界
lookup库在一次性worker进程lazy import，独立data/.lookup-deps避免污染botPython；stdin只有queries，不含原图/历史/身份/Key；stdout只有结果/精简error；stderr不日志查询。父async client包含scope内缓存(公开查询哈希/TTL1h，上限64)或暂不缓存，single semaphore，timeout/cancel只结束自己的child，不留HTTP后台发QQ。图像可能命名误认，仅给有来源的可能解释，不能拿相似摘要认证是谁。所有输出走原Plain/分页/发送守卫，lookup没有event/send权限。
main.on_llm_response在remember_reply开头处理whale_lookup_fallback，completion_text替换，queued之后由enforce_reply_hours在分页前再次result.chain=[Plain(fallback)]确保模型或其他装饰不能盲猜。provider err仍按旧错误闸门静默/重试，不能让错误文本分三条绕过。lookup上下文只传入当前请求，不自动当稳定事实存到记忆。

## 验证
纯函数Fake模型/结果安全测试+真实指定搜索后端公共梗查询+合成图现有模型提取查询；不发送QQ或动正式事实做测试。保证/help(含已去slash/带@)不记忆/不排队/不调用model/不发送，正常讨论/help不误挡；24.999/25秒边界、别名强force、夜间静默；lookup失败固定fallback、恶意网页不执行、私事不外发、3条预算仍生效。部署按现有冷态备份/固定CID/锁/源码hash保持，新增字段web_lookup_enabled/ignore_commands及proactive_gap_seconds=25，其他字段保留。manager只添加已配置规则白名单与提示，不新增破坏性按钮。
