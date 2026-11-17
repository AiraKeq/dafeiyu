# QQ 群 DeepSeek 部署记录

部署日期：2026-11-13。使用 AstrBot 与 NapCat 的现成程序，自己编写的部分是 Compose、SSH 隧道、隐藏密钥输入和部署校验脚本。

## 当前配置

当前状态（2026-11-15 23:23:35，北京时间）：**QQ聊天已按用户要求暂停，AstrBot为 `exited / running=false`；NapCat保持运行，配置、聊天记录、记忆与QQ登录资料保留。** 已安装v1.4.1，不卸载或回退；暂停期间不接收、记录或自动回复新消息，每日整理暂停。用户明确要求恢复前不得运行启动、安装、改密或部署脚本。详见[当前暂停记录](<F:/work/qq-deepseek-deploy/暂停发言.md>)。以下规则及验收信息描述暂停前的配置和历史。

2026-11-15 23:51仅更新 `base_style` 为用户最新版贴吧十级老哥人设，其他设置不变、未恢复生产聊天。人设提示为1–3句/每句20字/总60字；未修改旧发送与分页实现，不保证每句逐字硬限制。详情见[当前人设](<F:/work/qq-deepseek-deploy/persona-魔丸风格.txt>)与[风格更新记录](<F:/work/qq-deepseek-deploy/STYLE-20261115.md>)。

| 项目 | 配置 |
| --- | --- |
| 服务器 | `8.217.178.5`，Alibaba Cloud Linux 3，2 核 / 2 GiB |
| 部署目录 | `/opt/qq-deepseek` |
| AstrBot | v4.29.4 |
| NapCat | v4.18.42，使用社区归档 Docker 镜像 |
| 固定名字 | 大肥鱼 |
| 默认模型 | DeepSeek 官方 API，`deepseek-flash` |
| 群内触发 | @/称呼立即处理；普通消息进入10秒冷却队列；原生命令保留 |

AstrBot 原生随机主动回复和模型创建定时任务保持关闭；普通群消息由 `whale_memory` 的确定性冷却队列控制，不再按概率抽取。机器人忽略自己的消息和 @全体成员，管理员也遵守群白名单。当前使用文字聊天与记忆扩展，没有加载本地模型或执行沙盒。

## 固定身份与接话规则（v1.3.0）

- 固定名字是“大肥鱼”，主人QQ是`XXX`；按平台真实QQ号识别主人，固定身份不随风格成长或记忆清理丢失。
- 每天北京时间08:00–24:00回应，夜间保持静默；群聊启用还依赖QQ实际登录成功。
- @机器人或叫“大肥鱼”“鲸鱼娘”“鲸鲸”“大鲸鱼”“小鲸鱼”时立即开始处理，绕过冷却；旧普通模型/发送任务被抢占，连续称呼不重复认领同一批输入。
- 普通消息不再随机：未冷却时直接处理，冷却中先积累，完整回复结束10秒后自动处理积累的消息，无需再发一条触发。不设小时或每天次数上限。
- 10秒从完整回复发送结束计算，模型生成和必要分段还需要时间；没有新消息时安静，上一段回复只作上下文，不无限自聊。
- 待回复队列原子持久化、重启恢复；发送失败/夜间阻断不假确认，热重载回收旧任务。原生命令保留原流程，不误送入队列。
- 每条最多20字，14天双评分记忆、重要旧事件压缩保留核心的规则保持不变。去重仅保留待处理ID和最近2048个已完成ID，不承诺14天ID去重。

## v1.3.0 分段评分与正式群

默认一/二/三段权值60/30/10，按场景、完整度和自然度评分，近分才微随机。每段20字和内容完整优先，必要长回复可以超过3段。评分日志只存元数据，不影响记忆I/R或是否回复，不增加模型调用。实现、降级边界与配置见[分段说明](<F:/work/qq-deepseek-deploy/SEGMENTATION.md>)。

16:02备份部署，170项检查通过。16:38按用户要求把测试群1000000004换为正式群1000000003；旧会话数据隔离保留，不迁移。17:53实读新群24条用户消息/23条完整回复，队列无待处理/失败，评分一段22次/两段1次。魔丸风格与其他规则保持，QQ在线。下面v1.2与旧群验收属于历史。

## 使用与管理

新增 **[大肥鱼聊天管理器](<C:/Users/ASUS/Desktop/大肥鱼聊天管理器.lnk>)**：桌面双击打开本机专用面板，管理启停、性格、当前成长便笺和事件记忆，查看关键规则并创建冷态备份。打开不启用QQ聊天，编辑前先停止。见[使用说明](<F:/work/qq-deepseek-deploy/manager/README.md>)及[最终验收](<F:/work/qq-deepseek-deploy/manager/交付记录.md>)。下面旧管理页面仍可用于原生AstrBot/NapCat维护。
本机管理地址：

- AstrBot：<http://127.0.0.1:6185/>
- NapCat：<http://127.0.0.1:6099/webui/>

管理密码和 NapCat Token 保存在本机 `C:\Users\ASUS\.ssh\qq-deepseek-panels.txt`，文件权限只授予当前用户和 SYSTEM。API Key 不写入这份管理文件或聊天记录。

如果电脑重启后管理页打不开，在 PowerShell 执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File F:\work\qq-deepseek-deploy\open-panels.ps1
```

隧道只用于管理页面。机器人在云服务器上运行，本机关闭不影响群聊。Docker已开启开机自启，两项服务设置 `restart: unless-stopped`。QQ资料已持久化；曾遇错误168，用户已在手机QQ恢复使用并重新登录，快速登录设置已恢复。

v1.2.0的134项检查与真实模型连接/身份问答已通过；2026-11-14小号1000000001已确认在线，用户确认三种真实群聊测试均正常，生产队列7/7已确认、0待处理、0失败。首次非空日评2批/3事件，错误0。最新[魔丸人设](<F:/work/qq-deepseek-deploy/persona-魔丸风格.txt>)通过原生配置API仅更新base_style并热重载；旧版记录与本轮证据见[项目状态记录](STATUS.md)。

更换 DeepSeek API Key：双击本目录的 `填写DeepSeek密钥.cmd`。输入不会回显，密钥通过 SSH 标准输入传给服务器。操作会短暂停止并重新启动 AstrBot，以保证配置写入生效。

## 服务与数据

服务器命令：

```bash
cd /opt/qq-deepseek
docker compose ps
docker stats --no-stream
python3 napcat-access.py
python3 verify-server.py
```

`python3 verify-server.py --model-test` 会调用一次 DeepSeek API，发送固定的连接测试文本并产生少量 API 用量；常规状态检查不调用模型。

持久化目录：

- `data/astrbot`：AstrBot 配置、模型密钥、对话数据。
- `data/napcat/qq`：QQ 登录状态。
- `data/napcat/config`：OneBot 和 WebUI 配置。
- `data/napcat/plugins`：NapCat 插件目录。
- `backups`：部署时保存的配置备份。
- `artifacts`：保留的 NapCat Docker 归档安装包。

WebUI 仅映射到服务器 `127.0.0.1`；OneBot 6199 端口仅在 Compose 网络内部使用。无需在云安全组公开 6099、6185 或 6199。日志设置每个容器最多 3 个 10 MB 文件。

`configure-server.py` 的初始配置只允许执行一次；日后使用 WebUI 或 `--set-key` 更新。备份和数据目录包含凭据，应按私有数据保管。

## 来源与版本固定

调研入口包括 GitHub 与 B 站的 [QQ 接入教程](https://www.bilibili.com/video/BV14fxN6bEe4/)。选择 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 管理模型和群聊，NapCat 负责 QQ 小号接入。Windows 整合包不适用于这台 Linux 服务器；没有另写聊天机器人。

NapCat 上游仓库当前仅保留结束声明，原 Docker Hub 镜像无法拉取，因此使用 [XingYunStar/NapCatQQ 的 v4.18.42 归档](https://github.com/XingYunStar/NapCatQQ/releases/tag/v4.18.42)。下载文件已与归档发布的 SHA-256 核对。该校验确认下载文件与归档一致，不是上游签名验证。后续 QQ 更新可能影响这一历史版本。

- 归档文件：`napcat-docker-v4.18.42-amd64.tar`
- 字节数：`612216320`
- SHA-256：`85e3fd02b58fd836a0177734b6ff77a896000bcfcf209b3b200d7e8b8452df7c`
- 导入后 image ID：`sha256:039f78c89e67e0403d9cf11f7acda1ace7cf9564456c5db65ba67aa1f52fce7e`
- NapCat 设置 `pull_policy: never`，保留安装包以便恢复。
- AstrBot 固定镜像：`soulter/astrbot@sha256:72f341f0ffcf6621ef820277642c5d63955f7beeac3683433ce09cac96379c78`

模型名称依据 [DeepSeek 当前官方文档](https://api-docs.deepseek.com/)配置，使用 `deepseek-flash`。

## 基础部署历史验收

- 两个 WebUI曾返回HTTP 200；管理员为1000000002，管理页密码已通过实际登录核验。
- 旧版NapCat曾确认isLogin/coreReady=true，用户曾确认目标群正常回复；这些是历史记录，不代表本轮已重新登录。
- 原有镜像、QQ数据和管理凭据已保留。

## v1.2.0 本轮验收

- 134项代码/存储/原生隔离演练通过；生产已加载whale_memory 1.2.0，15项配置检查通过。
- 真实DeepSeek返回“连接成功”，测试14 tokens；合成身份问答正确包含“大肥鱼”和1000000002。
- 备份、版本、群白名单、10秒冷却、概率移除、08:00–24:00和20字分段均已核验。
- 2026-11-14已核验本人登录小号1000000001，ready/isLogin/coreReady=true；用户确认三种真实群测正常，队列7/7已确认、0待处理、0失败。
- 日评14日完成2批/3事件、0失败/错误，旧/新群独立事件与笔记实际入库。
- 14:34原生API仅更新base_style并热重载魔丸风格；读回与批准文本完全一致，QQ持续在线。两个真实模型合成情景分别给出轻怼与主动安慰。

不需要重新安装或再提供API Key。配置可在插件“稳定的说话基调”中改，或使用[风格更新工具](<F:/work/qq-deepseek-deploy/set-whale-style.py>)；当前[人设文本](<F:/work/qq-deepseek-deploy/persona-魔丸风格.txt>)已保留。若以后QQ登录过期，再在NapCat刷新实时登录码，不能用历史图片。后台每日00:20整理继续运行，未另建提醒任务。
