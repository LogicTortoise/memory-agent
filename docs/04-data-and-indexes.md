# Memory Agent 数据与查询索引设计

记忆正文、来源证据和提交历史保存在 Markdown/JSON 文件中；SQLite 保存扫描、队列和审计等运行元数据。日期与语义索引依然是可阅读的文本视图。BM25 仅在用户显式查询时计算，用于候选排序，不进入记忆捕获、分批和整理写入。

## 数据层级与权威来源

| 层 | 内容 | 存储与语义 |
|---|---|---|
| ConversationEvent | session Reader 解析出的消息/记录及来源位置 | segment JSON 原文为权威，SQLite 登记引用与去重键 |
| WorkUnit / Batch | 轮次、过长片段、按顺序分配的目标与上下文 | SQLite 负责分配/状态；输入快照在工件文件 |
| Candidate | 模型提案及原句引用 | 文件工件，未验证不能算用户记忆 |
| Memory / Conflict | 原子记忆版本与待决冲突 | Markdown/JSON + commit manifest |
| DomainSummary | 某快照、某范围的浓缩导航 | 派生 Markdown；不能成为新的用户证据 |
| TextIndex | 日期、领域、项目、标签、别名与目录 | 可重建的 Markdown/JSON 视图 |
| Runtime / Audit | 游标、lease、任务、用量、审计事件、通知 | SQLite；不复制全文和秘密 |
| DecisionEvent | 用户对具体版本提案的决定 | 来源文件 + DB 接收记录，按同一发布流程应用 |

## 文件布局

```text
memory-vault/
  config.toml
  state/
    state.sqlite
    worker.lock
    writer.lock
  sources/segments/<segment-id>.json
  inbox/decisions/<decision-id>.json
  artifacts/
    batches/<batch-id>/input.json
    candidates/<candidate-set-id>.json
    model-calls/<call-id>/result.json
    queries/<trace-id>.json
    jobs/<job-id>/result.json
  objects/
    memories/<memory-id>/<revision>.md
    conflicts/<conflict-id>/<revision>.json
    taxonomy/<revision>.json
    projects/<revision>.json
  commits/<commit-id>/manifest.json
  HEAD.json
  views/<commit-id>/
    dates/observed/YYYY/MM/DD.md
    dates/recorded/YYYY/MM/DD.md
    dates/effective/YYYY/MM/DD.md
    domains/<domain-id>/index.md
    domains/<domain-id>/summary.md
    projects/<project-id>.md
    tags/<tag-id>.md
    catalog.jsonl
    aliases.json
  privacy/HEAD.json
  privacy/deletions/<deletion-operation-id>.json
  trash/<deletion-operation-id>/manifest.json
```

以上是运行库的目标布局，尚未创建。sources/segments 是耐久事件收件箱的内容层，SQLite source_events 是事件目录与可见性入口；批处理队列直接用 SQLite batches，不再靠目录移动模拟状态。

正文对象与已发布 manifest 不原地改写。HEAD 是当前记忆快照指针；state.sqlite 是运行状态权威，两者通过发布恢复协议协调。视图和查询内存索引可以重建，数据库中的操作进度与审计不能随意丢弃。

## ConversationEvent 协议

```json
{
  "schema_version": 1,
  "event_id": "evt_example",
  "logical_message_id": "u42",
  "revision_hash": "sha256-of-source-message",
  "source": {
    "connector": "claude-code",
    "adapter_version": "reader-v1",
    "host_id": "local-mac",
    "source_file_id": "src_example",
    "generation": 1,
    "session_id": "session_example",
    "branch_id": "main",
    "turn_key": "turn_12",
    "project_id": "proj_memory_agent",
    "locator": {"byte_start": 18000, "byte_end": 18256, "record_ordinal": 42},
    "source_time": "2026-10-08T13:00:00+08:00",
    "discovered_at": "2026-10-08T13:15:00+08:00"
  },
  "role": "user",
  "kind": "message",
  "text": "这个项目只用文件存记忆。",
  "parent_id": "a41",
  "replaces_event_id": null,
  "origin": "user_session",
  "privacy": "standard",
  "context_only": false
}
```

字节 locator 指原始文件，证据 span 指解码后的 text code points，二者不能混用。消息语义和 text hash 决定事件修订；session generation 用于读取定位，不把轮转文件中的同一原生消息认作新要求。无原生 ID 的降级规则由各 Reader 显式说明。

segment 保存已选择的用户/助手原文和必要工具片段，不只存会失效的源路径。来源里私密/禁止记录内容在送模前过滤，裁剪范围可审计。正文中的“只用文件”是协议示例，不是重新取消本项目已获允许的 SQLite 状态库。

扫描、轮次、批次与跨批次上下文规范见 [Session 扫描与小批处理](09-session-scan-and-batching.md)。

## 原子记忆格式

```yaml
---
schema_version: 1
id: mem_example
revision: 1
status: active
kind: requirement
abstraction: principle
statement: 本项目的记忆数据使用本地文件存储。
subject: project.memory-agent
predicate: memory.storage.backend
value: files
modality: must
scope:
  level: project
  project_ids: [proj_memory_agent]
  domain_ids: []
  task_ids: []
  conditions: []
  excludes: []
domains: [dom_ai_agents_memory]
primary_domain: dom_ai_agents_memory
tags: [tag_file_storage]
horizon: persistent
valid_from: null
valid_until: null
review_after: null
asserted_at: 2026-10-08T13:00:00+08:00
recorded_at: 2026-10-09T02:30:00+08:00
evidence:
  - source_id: evt_example
    message_id: u42
    quote: 这个项目只用文件存记忆。
    span: {start: 0, end: 12}
    provenance: user_explicit
relations: []
supersedes: []
extraction:
  job_id: job_example
  prompt_version: extract_v1
  evidence_quality: explicit
---

本项目的记忆数据使用本地文件存储。

适用范围：Memory Agent 项目。不扩展为所有项目的数据库偏好。
```

上面的格式用于展示字段。若原话含“先”“这次”“一般”等限定，必须保留到规范表述和条件中；不能因为示例字段简洁就丢弃限定。`span` 采用 Unicode code point 半开区间 `[start,end)`，保存前重新验证 quote 与源切片完全一致；捕获与显示必须使用相同的 Unicode 规范化约定，不对原始证据文本做不可逆归一化。

`predicate/value` 用于寻找可能相反的要求，只有抽取足够可靠时才填写；不要求为每句自然语言发明本体。它们缺失时仍可通过域/范围和文本查找，不能因为 predicate 不同就排除语义冲突。

正文是规范表述；原话在 evidence 对应 source 中完整保留。生成摘要不允许删除原句中的否定、例外、程度词、条件和数量。

## 分类分面

| 分面 | 示例 | 规则 |
|---|---|---|
| 内容类别 kind | requirement / preference / self_fact / procedure / task_note | 自述事实和要求不能混为一类 |
| 抽象度 abstraction | principle / detail / null | 原则约束方法，细节指定操作；事实可为 null |
| 范围 scope | global / domain / project / task / session | 表达适用条件，独立于存放领域 |
| 时间 horizon | persistent / temporary / unknown | 原则也可能短期，细节也可能长期 |
| 领域 domains | AI 编程、AI 图片、检索研究 | 可有主领域和有限相关领域 |
| 项目 project_id | memory-agent / project-alpha | 稳定 ID，路径和 repo 名是别名 |
| 标签 tags | 文件存储、身份一致性、兼容性 | 可交叉查找，不替代领域树 |
| 强度 modality | must / prefer / avoid / unspecified | 忠实于用户措辞，不能把偏好升级为禁令 |

范围的可执行含义：同一分面内多个 ID 取并集，不同分面、conditions 与有效期之间取交集，再减去 excludes。global 表示未额外限定；domain/project/task/session 级别必须有对应 ID，级别与字段相矛盾时拒绝提交。domain 适用集合绑定稳定域 ID 和 scope 基准版本，是否包含下级必须明确记录，不能随目录移动静默改变。条件能确定时返回 true/false，无法判断时返回 unknown 并进入澄清；不把 unknown 当作不相交。

范围不是简单优先级排序。具体项目要求只有在用户确实表达局部例外时才可与全局原则并存；“更具体的总是赢”和“最新的总是赢”都不能成为写入规则。

## 时间和生命周期

区分用户表达时间 `asserted_at`、系统收录时间 `recorded_at`、事实有效时间 `valid_from/valid_until`。明确有效区间使用半开区间 `[valid_from, valid_until)`，召回时按当前/as-of 时间过滤，不能依赖夜间任务是否已经把 status 改为 expired。未知有效期用 null；不能把收录时间伪装成偏好的真实起始时间。

日期视图默认按表达时间展示，同时提供收录/更改记录和明确有效日期入口。迟到事件进入旧日期视图，但在本次增量处理中可见。`dates/effective` 只记录有效期开始/结束的边界日，不为长期记忆在每个有效日复制引用；as-of 查询对目录记录的时间区间执行过滤。

状态建议为 `candidate → active`，或 `candidate → needs_confirmation → active/rejected`。已生效记忆可转 `superseded`、`expired`、`archived`。存在冲突是关系和待决提案，不必改写旧记忆正文来“撤销”尚未被撤销的要求。

明确“本周”“直到发布”的内容解析为本地时区的有效范围，并保存原文。任务结束可使绑定任务的内容失效。模型猜测的期限只能设置复查建议，不能自动删除或失效长期要求；过久未用仅能归档建议。

## 日期与语义索引

`dates/observed/YYYY/MM/DD.md` 列该日来源、记忆 ID、变更类型和状态，支持“上周我说过什么”。`domains/<id>/index.md` 列本域简介、父子领域、有效原则、细节、项目例外和待决事项。项目/标签索引采用同一 ID 链接，不复制权威正文。

机器目录 `catalog.jsonl` 包含 ID、revision、正文短片段、各分面、有效时间、来源引用；它是某提交的投影。检索器启动时读入内存，允许完全扫描作为正确性基线。1 万条记忆的延迟和内存必须实测，不能靠估计承诺规模。

没有 embedding 时，“语义索引”指可解释的分类、别名、同义词与摘要导航，不声称具有完整的语义相似度。意义相同但没有词项重合的查询可能漏召回，因此必须有域浏览和模型扩展的降级路径。

## 查询阶段的 BM25 与 query 处理

BM25 只用于 `recall`。记忆生成、分类、冲突检查、日周月整理不通过它决定哪些用户要求值得保存。它是从文件记忆临时构建的词项排序函数，不是数据库、持续写入的搜索服务或必需的权威索引。

### QueryPlan

保留 original_query，解析硬 filters（用户明确的项目、日期、状态和隐私范围），记录 exact_terms、数字/版本/路径、条件与否定表达。模型可建议 1 个等义 rewrite 和至多 2 个 expansion，原查询总是保留。每个变体记录 origin、文本、用途、采用/拒绝理由与模型版本。

示例：原问题“我之前对 AI 视频换脸有什么要求，不要图片生图的设置？”可等义改写为“查询视频人脸替换的用户要求”，扩写“视频身份保持”“整段视频人脸迁移”；不能扩成所有图片生成参数，也不能推定用户已指定某个工具或项目。否定意图保持到候选判断，不能简单过滤掉含“图片”一词的对比说明或例外。

优先用明确别名；仅在需要时调用小模型改写，且限制 token/变体数。新加的项目、时间、数字、工具强约束必须有原 query/用户上下文依据，否则拒绝。记忆正文、网页和候选中的指令不参与修改 QueryPlan。

### 执行顺序

1. 验证显式触发，固定 HEAD 与 privacy epoch。解析 filters；没有项目的通用查询默认只取全局/领域规则，跨项目浏览要明确请求。
2. 根据文本索引、别名和领域导航确定查找范围；硬限制先执行，模型建议的领域只是路由线索，不能覆盖用户限制。导航不确定时允许在已授权范围内更广扫描，记录这一行为。
3. 从有效原子正文构建查询语料。先加入必须展示的相关上位原则、项目例外与待决冲突。不要预先按 mtime 或短摘要裁掉正文后再声称召回充分。
4. 原查询和有效变体分别执行精确词项查找与 BM25。`retrieval=keyword` 时仅走文本查找；精确 ID 查询直接读取，无需模型或统计排序。
5. BM25 推荐起点 `k1=1.2,b=0.75`。中文用查询时字符 bigram 加受控别名，英文保留单词及完整技术标识；路径/版本/命令另保留精确匹配。参数和 tokenizer_version 均需通过中文样例检验，不预设性能保证。
6. 每个 query 在同一允许语料上评分。多个变体不直接相加不可比的原始分数；可使用查询内排名融合 `Σ w_i/(60+rank_i)`，原查询权重 2，所有扩写/改写总权重至多 1。精确 ID、必备约束和冲突提示独立处理，不被扩写票数淹没。
7. 取约 30 条候选作可选模型重排/总结，模型只返回已有 ID、相关性理由和引用。更多候选保留分页与未评审计数，不声称已经读完；模型失败降级到原始检索结果。
8. 输出约 8 条/2,000 tokens，带 snapshot、filters、来源、query_trace_id 和 cursor。必备集合超预算时 `required_constraints_complete=false`，给遗漏数量与优先展开入口；冲突元数据不会被条数上限隐藏。

原 query 路线独立执行，改写失败或产生漂移时只丢弃变体。扩写最多增加候选，不能成为记忆证据、写回用户偏好或触发新的录入。

### 临时索引与边界

BM25 的 token、TF/DF、文档长度只在查询进程内计算，基于已过滤语料；可使用短期进程内 LRU，key 包含 commit、过滤范围、privacy epoch、tokenizer/alias 版本。内存缓存失效可随时丢弃，不加入 SQLite FTS、向量表或持久搜索集群。

小语料或没有共同词项时，BM25 不能凭空找出同义概念；靠别名、目录阅读和模型改写补足。查询前构建的 CPU/内存成本必须实测，1 万条只是首版容量目标。无答案时说明未找到足够证据，不由模型补造偏好。

审计 trace 记录原 query、所有变体/拒绝原因、硬 filters、候选通路、词项命中、BM25 分数/排名、重排前后 ID、token/耗时/失败、最终引用与截断范围；不存隐藏思考。UI 可比较“仅原 query”和“加入扩写”的结果，便于判断是否真正改善召回。

## 发布一致性与删除

SQLite 管理采集游标和批次队列；文件 manifest 管理已生效的记忆及消费结果。输入文件先写成再在数据库登记；记忆发布采用 prepared intent→HEAD 原子切换→SQL finalize，不宣称文件和 SQLite 是同一个事务。崩溃恢复和表约束以 [SQLite 状态与审计](12-state-and-audit-data.md)为准。

读取固定一个 HEAD，使用相同提交的视图与原子版本；投影滞后时直接按文件清单校验并提示状态对齐。模型计算不持 writer 锁；提交前检查范围、基准 revision 与 privacy epoch。

“忘记”先保存独立删除授权并立即过滤查询，再清理当前/历史对象、相关来源片段、查询工件、缓存和审计中的正文载荷。保留无正文 source identity 防止重扫同一历史再导入；未来新的明确表达可重新建记忆，不按一句话文本 hash 永久禁止用户重复偏好。

删除清单不随 HEAD 或旧数据库回滚。托管备份恢复前应用最新删除清单，不能取得最新副本时隔离检查。备份通过 SQLite backup API 与固定文件快照生成，不能直接复制运行中的数据库主文件而漏掉 WAL。保存的独立来源片段不足以支撑某条记忆时标为需核验，不能留悬空引用。
