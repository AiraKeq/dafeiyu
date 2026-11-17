# 大肥鱼语义记忆 v1.5.0 接入契约

2026-11-16 用户明确要求立即实行，并确认“暂时停止并升级，交付仍暂停”。已用真实固定CID停止AstrBot；NapCat与DSH不动。现镜像Python3.12.16 / SQLite3.46.1，宿主Python3.6.8，云内存约1.827GiB，停服前可用约885MiB。父代理亲自查GitHub/B站及官方文档：sqlite-vec + FastEmbed 的 BAAI/bge-small-zh-v1.5，512维，本地CPU，Qdrant公开ONNX约94.8MB。Chroma/Mnemosyne不叠装（重复总结与提示注入、更多依赖），Milvus/Qdrant常驻服务暂不需要；自己写索引同步/原接线，复用成熟检索与embedding库。仅下载公开模型，不把群聊发到新增外部接口。

## 约束
- 事实库 memory.sqlite3 的schema/user_version不变；messages/styles/队列/分页/画像/权限/身份/10秒/08–24全部保留。只索引events的摘要/有效详情。SQLite+sqlite-vec独立派生索引文件 semantic-index.sqlite3，可重新生成，不作为事实备份。
- 索引候选仅ID+scope+fp+similarity。每次回忆按当前源行核验；不存在、改文/改I/tier、ID复用、模型/维度错误、到期低I都拒绝旧向量。输出正文只来自当前事实行的只读生命周期投影。
- 低I已到14天即不回忆；高I到期只core；active详情0/3/7天投影640/320/160。查询不调用apply_lifecycle，不更新源文件/SHM。R现场计算。
- 第三方包仅在独立worker进程lazy import，不进入AstrBot主解释器sys.path。模型预下载并验证SHA；运行local_files_only=True/CPU/threads1，不在模块import/initialize/manager读取自动pip或下载。缺依赖/模型/坏索引安全词法回退，近期文本/成长不丢。
- worker生命周期由插件initialize/terminate管理，终止需等待真实child结束，超时只杀自己创建的child，不留下旧写者。请求有界，排队/超时快速回退；索引同步不能占事实事务；后续source变化重新核验。
- manager读状态不触发embedding/rebuild，不改源库/索引。事件update/delete成功源commit后同步invalidate，失败仅warning“事实已保存，索引延期”；安全依靠召回前源fp核验。style_save不动事件索引。骨干版本兼容1.4.1与1.5.0过渡，保持所有固有写约束。

## 分工（互不写对方文件）
- 核心代理：plugin/semantic_state.py（纯标准库Py3.6兼容）, plugin/semantic_index.py（纯标准库顶层/lazy sqlite_vec，仅worker用）, tests/test_semantic_state.py, tests/test_semantic_index.py。
- 管理器代理：manager/remote.py + manager/public/* + manager/test_remote.py + manager/test_browser.mjs；只加语义状态卡、同步失效和版本支持，不改server.mjs/runtime/launcher，不访问安装版/生产。
- 父代理：plugin/memory_store.py、daily_review.py、main.py、semantic_worker.py、semantic_client.py、配置schema/版本/包、模型与依赖准备、真实测试/备份部署、安装版同步、文档。

## 核心API（稳定名字，先通知再改）
semantic_state.py：
- 常量 MODEL_ID='BAAI/bge-small-zh-v1.5', DIMENSION=512, MODEL_REVISION='46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59', INDEX_NAME='semantic-index.sqlite3', INDEX_SCHEMA=1, INDEX_APP_ID=0x57565331。
- project_event(row, now, active_days=14, important_threshold=8) -> dict|None。复制源row并以有效detail/core/tier投影，不写原row。Source fp需同时覆盖实际源身份全部字段与有效投影（排除随秒变动的R）；index文档用title+core+有效detail，核心靠前。
- event_fingerprint(row, now, active_days=14, important_threshold=8) -> str|None；低I已过期返回None，含scope/id/ts/day/created_at/title/detail/core/importance/participants/keywords/tier + 有效投影 + model/revision/dimension。
- document_text(row, now, active_days=14, important_threshold=8) -> str|None。最大1000字符，来源core靠前，保留原指纹完整核验；模型会512 token截断，注明摘要索引边界。
- read_events(db_path, scope=None, attempts=2) -> list[dict]。严格物理只读：DB/WAL复制私有临时目录，前后源hash/stat一致，mode=ro/query_only/BEGIN，缺失/格式未知失败而不建源库。禁止Store构造器、live immutable。
- render_recall(rows, scope, query, now, active_days=14, important_threshold=8, half_life_days=4, max_chars=2200, limit=8, semantic_scores=None, min_similarity=0.70) -> str。semantic_scores格式 {event_id:{fingerprint:str,similarity:float}}；scope精确匹配、阈值与fp当前核验；语义相关度与原词法融合后仍0.5相关+0.3I+0.2R、最多2近3天补位、最多8、全行metadata+换行算2200。归档必须真相关；词法fallback也有效生命周期过滤；format保持旧MemoryStore样式。
- index_status(data_dir, now=None, active_days=14, important_threshold=8) -> sanitized dict，字段：state='ready'|'stale'|'missing'|'unavailable', engine='sqlite-vec', model, dimensions, indexed_events:number|null, effective_events:number|null, pending_events:number|null, last_sync:number|null, error_type:string|null。只读源与索引，不init/repair/touch；缺索引state missing/已知源有效数量，未知用null；只有fp+元数据匹配的条目计indexed（不是全部BLOB行数）。count要求source eligible fp核验。
- invalidate_index(data_dir, scope, event_id, reason='source_change') -> bool。若侧库存在且精确schema可写，短事务DELETE vectors(scope,id)+标stale/清source_signature；模型不用加载，缺侧库返回True（本就未索引）；坏侧库失败抛精简异常供warning，不触事实库、不误改styles。

semantic_index.py：
- SemanticIndex(data_dir, embedder, active_days=14, important_threshold=8, dimension=512)。embedder是父代理给的 callable(list[str])->list[list[float]]，本机假模型也能测试；向量库内部lazy sqlite_vec，missing raises安全错误，父进程捕获。只在显式reconcile建侧库。
- reconcile(now=None) -> sanitized dict {indexed_events,effective_events,pending_events,last_sync,...}。全量指纹核对/增量只embed变更文本（doc_hash可复用），生成后再次read_events/currentfp核对后原子发布；删除孤儿/过期/旧fp；模型/schema不符不静默清坏库；单worker事实安全，不声称跨库全局原子。
- search(scope, query_vector, limit=12, now=None) -> list[{event_id:int,scope:str,fingerprint:str,similarity:float}]。侧库mode=ro快照，在副本加载sqlite-vec、SQL WHERE scope=? + vec_distance_cosine(embedding,?) ORDER BY distance LIMIT ?；用成熟kernel精确向量搜索，当前小库不引入ANN大服务；校验维度/finite/normalized范围、模型/schema。
- 普通侧库表，便于宿主标准SQLite只做invalidations：vectors(scope TEXT,event_id INTEGER,fingerprint TEXT,document_hash TEXT,embedding BLOB,PRIMARY KEY(scope,event_id)); index_meta(id INTEGER PRIMARY KEY CHECK(id=1),schema_version INTEGER,model_id TEXT,dimension INTEGER,model_revision TEXT,sqlite_vec_version TEXT,generation INTEGER,last_sync REAL,source_signature TEXT,error_type TEXT)。约束/字段最终DDL由核心统一，manager不要复制DDL自证，只用semantic_state函数。整个index不存可直接注入的历史正文/查询文本，不输出真实embedding。

## worker协议（父代理实现）
stdin/out逐行JSON、id关联，stdout不含logs/正文/令牌；stderr丢弃或只精简type。启动参数固定data_dir/deps_dir/model_dir，原source path从data_dir固定派生。命令 {'id':n,'action':'status'|'reconcile'|'search'|'stop',scope?,query?,limit?}；回复{ id,ok,data|error:{type} }。search返回candidate列表。启动显式加载固定模型，校验本地manifest，不联网；初次reconcile及后续60秒（父client合并dirty）在独立进程运行。manager读不创建worker。

## 管理器
status新增 semantic 字段包含上述index_status + enabled:boolean|null, execution='local_cpu', external_text_transfer=false。UI明确暂停时仅索引已就绪，不声称聊天正在用；缺/坏显示未知/未建立，保留主服务stop。展示模型、维度、有效索引数量/事件数量、待同步、最近同步；初版不加语义聊天原文搜索或rebuild按钮（父代理初次离线rebuild、恢复后plugin自动增量）。rules允许semantic_enabled、semantic_similarity_threshold、semantic_top_k、semantic_query_timeout_seconds、semantic_sync_interval_seconds原字段。manager import标准库shared模块仅可信固定plugin路径semantic_state.py，不构造整个插件；缺模块status未知/warning、不挡老版功能/停止。事件成功后invalidate失败返回warning、fp源核验确保不读旧事实；备份仍原两文件+manifest，不将索引当事实备份。

## 验证
老263单测+53manager+原生68演练保持；新重点删除/改I/同id新scope和同scope新created_at/生命周期/阈值/损坏NaN/错误dimension/缺依赖/源零写(含SHM)/两worker争用/生成中源变化/terminate。真实本地模型用纯合成中文事件与不同措辞query测试，非事实原文测试消息不进入正式库/QQ。正式索引构建只本机CPU处理已有事件、原事实库hash保持，明确派生索引本身新增为预期变化。部署需要备份生产代码/配置/数据且不中途启动；安装版、云helper与本机bridge digest同步后安排仅管理器本机重载。
