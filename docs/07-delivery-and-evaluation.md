# Memory Agent 开发文档与验收工作包

本文件定义后续开发过程。每个工作包有确定输入、产出、允许修改范围和验收边界；本轮只交付设计，不创建产品代码或运行功能测试。依赖版本在进入实现时锁定，未测的容量/质量指标只作为目标。

## 关键技术和固定边界

| 层 | 选型 | 原因与限制 |
|---|---|---|
| 核心 | Python 3.12+、Pydantic 2、明确 ports | 文件/批处理适合独立服务；schema 与接口可验证 |
| 运行状态 | 标准库 sqlite3、WAL、显式迁移 SQL | 保存游标、批次、lease、任务和审计；不存大段记忆正文 |
| 权威内容 | Markdown/YAML 原子记忆、JSON segment/工件/manifest | 人可读、来源可追溯、可固定快照 |
| session 接入 | Claude/Codex 独立 Reader + 增量文件扫描 | 不依赖前台 hook；私有格式需脱敏 fixture 和安装验证 |
| 小批模型任务 | 默认 5 轮、6000 输入 token、1500 输出 token、可配置 | 轮数和 token 双限制，超长片段不强塞小模型 |
| 模型 provider | 首个为 Claude Code CLI，JSON 输出、限工具/预算/超时 | 一次任务一个明确 schema，不引入自治 Agent 框架 |
| 查询 | 文本目录/关键词；可选查询时内存 BM25；query 改写/扩写和重排 | BM25 不进入记录/整理，原 query 与硬范围始终保留 |
| 后台 | 项目自有 worker，OS 服务仅托管进程 | 支持独立命令执行，不依赖个人中控服务 |
| Telegram | 项目自己的 Bot API 适配器，单长轮询接收器 | 决定落盘后推进 update cursor，和 UI/CLI 共用规则 |
| 审计 API | FastAPI + 同一应用服务/DTO | localhost，写命令有版本与幂等控制 |
| 审计 UI | React、TypeScript、Vite、CSS 主题变量、原生表格与分页 | 静态资源本地发布；不在前端实现业务规则 |
| 验证 | pytest；前端 Vitest/Testing Library；关键流程 Playwright | 测真实状态/来源/恢复语义，不只测字段复制 |

首次实现不引入 Redis、ES、向量 DB、SQLite FTS 或外部调度平台。Python 与前端依赖分别锁定，前端构建产物随本地发行包交付。完整目录见 [代码结构](13-code-layout.md)。

## 工作包

下表中“允许范围”指代码拥有边界；测试放在对应 tests 子目录。输入协议指向设计文档，字段改变必须同步 contracts、fixture、调用方与验收。

| ID 与依赖 | 确定输入 | 产出与允许修改范围 | 不在本包范围 | 验收 |
|---|---|---|---|---|
| T01 契约，无依赖 | 04/09/10/12 的字段、状态、例子 | contracts、domain 的基础类型；fixtures/contracts | IO、模型、UI | 所有操作与事件可校验；错版本/未知字段/不合法状态明确拒绝；JSON Schema 可导出 |
| T02 SQLite，T01 | 表清单、状态约束、游标与 lease 规则 | infrastructure/sqlite、运行 repository tests | 解析 session、记忆正文、UI | 唯一键去重、事务回滚、lease token、WAL 并发读取、migration 备份/失败恢复正确 |
| T03 文件库与恢复，T01–02 | segment/object/manifest 格式、commit intent 协议 | infrastructure/files、application/recovery、故障测试 | LLM、检索评分、通知 | 原子 HEAD；文件写成/DB 未提交、HEAD 已切/DB 未 finalize 等故障点重启不漏不重；删除清单优先 |
| T04 两种 Reader，T01 | 明确 roots 与每种入口脱敏 JSONL fixture | adapters/sessions、fixtures/sessions | 批次策略、记忆抽取、用户真实历史导入 | 角色/branch/parent/turn 映射正确；stream/双重记录去重；未知格式拒绝；没有同文本跨会话误合并 |
| T05 增量扫描，T02–04 | SourceReader 输出、持久游标、扫描范围 | application/scan、扫描集成测试 | 任何模型调用 | 续写旧 session、半行 UTF-8、rename/truncate/rewrite、backfill、崩溃重扫、同源并发不丢记录；扫描调用模型数为零 |
| T06 轮次与分批，T01–02/05 | 标准事件、5 轮/token policy、context refs | domain/batching、application/plan_batches | provider、冲突决定 | 20 轮→4 批、12 轮→3 批；工具不计轮；超 token 拆小；尾轮/超长/指代有明确状态；重规划不重复目标归属 |
| T07 provider 与抽取，T01/06 | BatchSpec、提取 schema、模型能力与预算 | adapters/models、infrastructure/process、application/process_batches 的抽取部分、prompts/extract | 分类树迁移、自动覆盖记忆 | 结构错误、认证失败、超时、取消、预算耗尽不被当 no_memory；quote/span 校验；只抽 target，保留 context_only |
| T08 范围与提交，T03/07 | 候选工件、active 与相交历史、DecisionEvent | domain/scope/lifecycle、application/reconcile/decide、相关 prompts | 读取任意 session、UI、统计检索 | 同批/跨批/跨入口冲突发现；范围未知不猜；explicit 更正绑定唯一对象；未决不覆盖；重复决定幂等 |
| T09 查询，T01/03/08 | RecallRequest、文件视图、QueryPlan | application/recall、domain/query_policy、infrastructure/search、query prompts | 捕获、分批、任务调度 | 原 query 保留；扩写不改硬范围/否定；BM25 仅 query 调用；过滤先于送模；词项基线/模型失败均可用；版本化分页正确 |
| T10 脚本入口，T01/05–09 | CommandRequest/Response 与退出码表 | application/dispatch、interfaces/cli、CLI 集成测试 | HTTP/前端、复制业务规则 | subprocess argv+stdin 调用成功；stdout 纯 JSON；sync 不需 worker；async 返回 job；相同 key 重试不重复副作用 |
| T11 worker，T02/05–10 | 队列、时间窗口、预算/并发、心跳 | worker、deploy/launchd、运行测试 | 通用任务平台、前台 hook | 休眠补跑、日周月依赖、lease 恢复、取消与 backlog；无增量零模型；CLI 与 worker 共享锁不抢写 |
| T12 Telegram，T02/08/11 | 通知 outbox、提案、允许用户、update | adapters/telegram、Telegram mock tests | 新记忆判定、绕过 decide | 发→回复→持久化→游标推进→提交；重复/乱序/过期/离线正确；机器人回显不成为证据 |
| T13 审计查询，T02–09 | 表/工件引用和已发布 manifest | application/audit_queries、contracts/audit、审计查询 tests | 直接改变记忆或执行模型 | source→batch→call→candidate→commit 可追溯；计数口径统一；DB 滞后可见；隐私/分页/脱敏正确 |
| T14 本地 HTTP，T10/13 | 10 中 endpoints、命令与 DTO | interfaces/http、HTTP contract tests | 独立 SQL 写逻辑、远程多租户 | 只读 GET 不调模型；POST 共用版本/幂等；同源/token 校验；静态 UI 资源可本地加载 |
| T15 UI 结构与主题，T01/13 | 11 的布局/token/假数据 DTO、静态设计稿 | web/app/pages/components/theme、UI unit tests | 后端 schema、真实写调用 | 概览/批次/决定/查询轨迹导航，原话定位与对照，空/失败/离线/部分状态，键盘/窄屏/明暗主题 |
| T16 UI 联调，T14–15 | 真实 HTTP 合同与命令回执 | web/api 与页面动作、Playwright | 修改后端业务规则来绕过问题 | 旧提案按钮拒绝；重复点击单次提交；更新不抢焦点；浏览不调模型；提交后显示真实 job/commit |
| T17 备份与交付，T03/10–16 | 一致快照、删除清单、部署配置、验收样例 | application/backup、doctor 完整检查、scripts、打包/发布说明 | 未授权真实历史迁移 | SQLite backup+文件快照可恢复；缺删除清单隔离；干净环境独立运行；卸载不删 vault |

T07 的抽取分支和 T08 的 reconcile 通过明确候选工件交接；最终 batch process 要包含二者，不能在只提取成功时返回 completed。T15 可以先用合同假数据开发，假数据模块不进入生产数据路径。

## 阶段交付闸门

- G1（T01–06）：仅靠本地文件与 SQLite 完成扫描→轮次→批次，无模型。这是最先验收的可检查垂直链路。
- G2（T07–10）：脚本可处理一批并召回有证据的结果；失败/冲突不丢队列，不自动扩范围。
- G3（T11–12）：独立后台日整理与 Telegram 决策闭环；关机恢复不会遗漏增量。
- G4（T13–16）：审计 UI 能解释整个链路，并通过同一应用服务完成一次真实决定。
- G5（T17）：发布包、备份恢复与影子试用。先人工抽查再开启无冲突自动提交。

不把“UI 有页面”当 G4 完成，也不把“某一入口解析成功”当两个 Reader 都已支持。未支持的格式必须显示明确局限。

## 关键验收情景

| 组 | 必须覆盖的具体输入 | 预期结果 |
|---|---|---|
| 增量 | 同一 session 分三次追加；一行 JSON 被分两次写；尾部 UTF-8 未完成 | 只处理完整新增事件；游标在安全边界；重扫零重复 |
| 重写 | 同路径缩短/替换/中部修改；文件改名；原生消息 ID 重放 | generation/来源修订正确；不跨项目误合并；旧候选失效 |
| 轮次 | 20 用户轮次中穿插 40 工具记录；另有 12 轮与未完成尾轮 | 通常 4 批/3 批，工具不增加轮数，尾轮状态可见 |
| 长输入 | 一轮 12k tokens、跨轮“这个除外”、大段工具输出 | 有预算内片段或 needs_context；不截掉条件后直接生效 |
| 语义 | 引用/假设/助手建议、短期原则、长期细节、否定与项目例外 | 只确认用户明确要求；范围/时间不互相替代 |
| 冲突 | 同批相反、相邻批相反、跨入口相反、三年前原则 | 真冲突待决；不按最新/入口不同直接忽略 |
| 执行 | 同 batch 重试 20 次、lease 过期旧进程回写、模型坏 JSON | 单次有效结果；旧 lease 拒绝；非正常空结果不是 no_memory |
| 一致性 | segment 写后 DB 前、HEAD 后 finalize 前进程终止 | 恢复后可重扫/补 finalize；没有虚假 completed |
| 查询 | 原 query 精确版本号/否定；扩写误加项目；无词面同义 | 拒绝漂移；保留原路线；不足时说明局限 |
| UI | 批次完成但待决、投影落后、旧按钮、仅浏览刷新 | 执行/业务状态分开；写操作版本检查；零隐式模型调用 |
| 备份删除 | 忘记后从旧快照恢复，重扫旧 source | 删除清单先应用，禁止悄悄复活 |
| 脚本 | JSON stdin、异步 worker 离线、重复 key 参数变化、超时续跑 | 可机读状态；queued 不冒充执行；冲突拒绝；已有提交不重做 |

## 质量目标与测量

正确性硬门槛：所有 active 要求都有原句来源；未决覆盖、作用域越界、重复消费在验收集为零；身份/版本不匹配决定不能生效。扫描、计划和 UI 浏览必须能证明零模型调用。

语义样例集初期约 100 条，明确要求抽取精确率目标 ≥95%、召回率 ≥85%；检索 Recall@20 目标 ≥90%，分别报告中文、跨项目、时间和同义词子集。先建立 keyword 基线，再比较查询时 BM25 与改写/扩写/重排的增益，不以总体平均掩盖否定或范围错误。

性能目标：1 万条记忆上不含云模型的查询 p95 ≤1 秒；分页 UI 状态查询 p95 ≤300ms；扫描按 MB/s、完整记录数/秒、内存峰值报告，不再用前台 hook 延迟衡量。token 估算误差、每 100 轮的模型调用/花费和失败重试单独统计。这些均未实测，开发时用指定机器与数据量报告，未达到时先定位瓶颈。

## 开发提交要求

每个工作包提交说明包含：使用的输入 fixture/schema 版本、修改文件范围、输出工件/接口、通过的场景、未通过的边界和迁移影响。只运行与变更相关的检查；核心状态或发布协议改动必须重跑对应故障注入。不得把跳过测试、仅 mock 成功或手工看图说成全链路通过。

依赖方未完成时使用合同 fixture，不自行改另一模块的字段含义。需要新字段先更新 contracts 和相关文档；不保留旧代码兼容层。用户以后明确要求提交时按其 git commit + push 约定执行，本轮不创建提交或部署。
