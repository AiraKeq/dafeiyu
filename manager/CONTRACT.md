# 大肥鱼管理器 v1 协作契约

独立本机 Node 标准库服务，127.0.0.1:17865。通过现有 SSH 密钥访问现有云部署，不安装第二套机器人，不修改插件，不为管理启动 AstrBot。前端原生 HTML/CSS/JS 无依赖。管理器自身可启动，生产聊天保持暂停。

## HTTP
- GET /api/session -> {ok:true,data:{authenticated:true,csrf:string}}，要求Cookie + 当前页面内存中的X-CSRF-Token。单独Cookie或未登录401，不从Cookie重新授予浏览器能力。
- POST /api/login JSON {token}，Origin同源，仅用于一次启动链接的fragment令牌兑换HttpOnly SameSite=Strict会话，data返回{authenticated:true,csrf:string}；页面只在内存中保留csrf，再携带它请求/api/session。整页刷新或关闭后须重新从本机启动器授权，不把令牌写浏览器storage。
- POST /api/action JSON {action,...fields}，Cookie + X-CSRF-Token，Origin同源。
- 全部响应 {ok:true,data:...} 或 {ok:false,error:{code,message}}。UI用textContent，不插入数据库HTML。未登录只能看静态空壳。
- GET /health -> {app:'whale-manager',version:'1.0.0'} 无敏感字段。

## SSH远端
remote.py 从stdin读单个JSON对象，stdout只输出单个JSON响应。默认root=/opt/qq-deepseek，固定生产容器qq-deepseek-astrbot-1 / qq-deepseek-napcat-1；测试允许构造Controller(root=临时目录,runtime=假实现)，生产入口不接收客户端root或命令。Python标准库。保存/备份/启动必须锁、身份验证、确认暂停、先备份、CAS。紧急停止单独严格核验AstrBot及CAS、写独立审计，不依赖NapCat可读性或机器人数据库健康，不覆盖旧pending意图。status/memory_list/style_get/personality等读操作不改机器人文件/数据库、不调用生命周期/模型。只读SQLite mode=ro + query_only，无存储构造器。凭据采用字段白名单，错误不能回显命令/配置/异常内容。

## actions 与 data
- status -> {checked_at:epoch_seconds, service:{state:'running'|'stopped'|'unknown',id:string,started_at:string,paused_marker:bool},qq:{state:'online'|'offline'|'unknown',account:string,reason?:string},version:string, rules:{...白名单已配置字段},model:{name:string,key_configured:bool|null,availability:'unknown'},scopes:[{scope,label,messages,events,style_day}],queue:{pending:number|null,failures:number|null},review:{completed_day,last_result:{processed_batches,failed_batches,events,style_updated}},pagination:{active:number|null,expired:number|null},warnings:[string]}
- personality -> {text:string,revision:string,characters:number,combined_characters:number,polish_budget:2000}
- personality_save {text,revision,confirm:'保存性格'} -> {backup_id,revision}
- style_get {scope} -> {scope,notes:string,day:string,revision:string}
- style_save {scope,notes,revision,confirm:'保存成长便笺'} -> {backup_id,revision}
- memory_list {scope,kind:'events'|'messages',query?:string,cursor?:string,limit?:number} -> {items:[...],next_cursor:string|null,as_of_now:number}；事件项{id,title,detail,core,importance,recency,tier,day,ts,keywords,revision,predicted_action}；原文项{id,role,sender_name,text,day,ts,processed}。只允许编辑事件，原消息只读，明示不等于删除衍生记忆。分页必须稳定并绑定scope/kind/query。
- event_update {scope,id,revision,title,detail,core,importance,keywords,confirm:'保存记忆'} -> {backup_id,revision}
- event_delete {scope,id,revision,confirm:'删除这条记忆'} -> {backup_id}，只删除指定事件，备份保留，原文/便笺不随之删除。
- backup {confirm:'备份记忆',revision:status.service.id+':'+status.service.started_at} -> {backup_id,created_at}；仅冷态，备份包含记忆库、成长便笺、当前人设，不回传全配置/模型密钥，不自动下载聊天正文。
- backups -> {items:[{backup_id,created_at,action}]}，只列管理器自己创建的备份，完整恢复属维护操作，UI说明暂停下从云端备份恢复，不冒称一键撤销。
- service_start {confirm:'启用大肥鱼',revision:status.service.id+':'+status.service.started_at} -> {service:{state,...},warning:string}；显式用户确认才可启动固定已核验CID，不compose up、不拉镜像、不启NapCat。需告知可能处理旧队列与日评生命周期，不进行默认实际验证。
- service_stop {confirm:'停止大肥鱼',revision:status.service.id+':'+status.service.started_at} -> {service:{state,...}}；停止固定已核验CID，失败绝不启动回滚。

所有写动作运行中拒绝（service_stop除外）；UI禁用并提示先停止，仍由服务端最终验证。未知状态拒绝写。启动后QQ readiness与模型可用性分开，不将running说成成功在线。状态不读正文；进入记忆标签才按选定scope读正文。自动刷新不得覆盖编辑内容。长任务防重复提交与错误提示，脏表单切换确认。前端所有修改与启停需确认弹窗，删除需输入确认文字。键盘可用，窄屏响应式。

代理分工：远端代理只写remote.py和test_remote.py；UI代理只写public/；主代理写server.mjs、启动器、HTTP/SSH测试、部署、文档、最终集成。
