# Memory Agent 社区方案与选型依据

最适合这个项目的组合是：借鉴 Basic Memory 的 Markdown 权威内容、OpenViking 的分层摘要、LangMem/Letta 的后台整理、Graphiti 的来源与时间记录，以及可解释的目录导航与按需读取方式。核心引擎自行做小规模实现，复用这些机制，不整体引入它们的数据库和服务依赖。

调研时间为 2026-10-08。社区方案与本项目约束不同，下面明确区分现有实现事实和本项目的设计选择。关键资料的原文快照与核验报告保存在 [研究目录](../research/README.md)。

## 方案比较

| 社区方案 | 核验到的机制 | 采用的思想 | 本项目不直接复用的部分 |
|---|---|---|---|
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | Markdown 内容、结构化元数据、链接；本地 SQLite 索引，可扩展语义检索 | 人可读文件为权威，索引是投影，稳定标识和关系引用 | 现成 SQLite/语义检索后端；不把“文件优先”误写成“完全没有数据库” |
| [OpenViking](https://github.com/volcengine/OpenViking) | 虚拟文件系统；L0 一句摘要、L1 概览、L2 全文；部署仍需要 embedding/VLM | 按领域先看导航，再看概览，最后展开原子证据 | 向量和虚拟文件系统服务；本项目的纯文本版本是重新设计 |
| [LangMem](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) | semantic/episodic/procedural 类型；profile 单文档与 collection 多记录；后台抽取与整理 | 原子集合为权威，用户画像/领域概览为派生视图；后台抽取 | LangGraph BaseStore、示例中的 Postgres/embedding；不让单一画像文档遮住相冲突的细节 |
| [Letta sleep-time](https://www.letta.com/blog/sleep-time-compute/) | primary 与 sleep-time 的职责分离，后台把 raw context 整理为 learned context | 对话入口轻量，后台处理允许更高延迟 | 完整 Letta server、前台常驻核心记忆自动注入；首版也无需多个自治 Agent |
| [Graphiti](https://github.com/getzep/graphiti) | episodes 保留来源；派生事实可回溯；双时间和失效机制；图存储与混合检索 | 原话/派生记录分层，有效时间与收录时间分开，保留更替历史 | 图数据库；自动失效规则改为用户要求的确认流程 |
| [Mem0 提示模板](https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py) | 源码包含 ADD/UPDATE/DELETE/NONE 四操作模板，也包含 ADD-only 模板 | 模型输出有限动作提案，交给程序验证；操作要有目标 ID | 不从模板存在推断当前默认策略或版本；不允许模型自行删改用户要求 |
| [QMD](https://github.com/tobi/qmd) | 统计词项检索、向量、排名融合、重排；SQLite 与 sqlite-vec 等依赖 | 只借鉴查询时词项排序与重排思路 | 不采用整套引擎、向量和持久搜索库；状态 SQLite 是另一职责 |
| [PageIndex](https://github.com/VectifyAI/PageIndex) | vectorless 文档树与推理式检索；树骨架与摘要/refine 有不同模型角色 | 先导航再下钻，结果保持明确来源 | 不把长文档/PDF 树索引直接套在每条短记忆上；不采用论文跑分推算个人库性能 |
| [LongMemEval](https://github.com/xiaowu0162/LongMemEval) | 信息抽取、跨会话推理、知识更新、时间推理、拒答五类能力 | 以能力拆测试，加上个人产品特有的范围和冲突测试 | 不用公开问答成绩替代本项目验收，不将整个数据集简单称为合成事实问答 |

## 与核心需求的对应

### 原子记录和聚合视图

LangMem 的 profile/collection 区分提供了一个重要选择：画像方便阅读，集合方便保留每条事实的独立证据。这里选择 collection 作为权威数据，profile、coding rules、image preferences 都是投影。这样既能“浓缩”，又能保留例外、更替和来源。

Basic Memory 证明了 Markdown、元数据和链接可以作为实际产品的数据表面。不过它自身依赖本地索引数据库；本项目把记忆导航索引保留为 JSON/Markdown 派生文件，另外按最新要求用 SQLite 管理运行与审计状态；这属于本项目自己的工程取舍，不是直接使用原系统的开箱能力。[S01][S03]

### 分层整理和分类抽象

OpenViking 的 L0/L1/L2 提供了可借鉴的三档读取方式：导航、概览、完整内容。这里进一步把“内容分类层级”和“读取摘要档位”分开。域树可以是 AI → 多媒体 → 图片生成；同一个域仍同时具有导航、概览和原子条目三个读取档位。[S02]

Letta 的 sleep-time 与 LangMem 的 background formation 支撑“后台整理与前台交互分离”这一机制。这里不要求每轮都整理，改成日增量、周分类、月聚合，来自用户对延迟的容忍及期望周期。[S03][S04]

分类本身采用领域树 + 项目/时间/标签/范围等正交分面。社区项目并没有给出适合这位用户的现成唯一领域树；AI Coding、AI 图片、AI 视频的初始层级与拆合阈值是本项目建议，必须通过真实使用反馈调整。

### 冲突和时间

Graphiti 的出处与双时间记录值得保留，但“允许事实失效”不等于“新话自动覆盖旧话”。本项目先生成待确认提案，用户决定之后才更替。表示时间的字段名称属于本项目协议，不能误认为逐字复制 Graphiti 的 schema。[S05]

Mem0 的操作模板说明可以将模型变更限制为结构化动作，但安全性来自外层校验器，而不是 prompt 本身。首版可用 add/corroborate/clarify/conflict/reject；已确认的覆盖由决策执行器实施。模板里的 DELETE 不是给模型的执行权限。[S06]

### 文件检索

QMD 作为较完整检索引擎的对照，帮助明确本项目不采用的复杂度。[S07] 按用户最新要求，本项目允许 BM25 仅在显式查询阶段使用，并配合原 query 保留、改写/扩写、领域导航和模型重排；采集与整理不依赖它。语义覆盖能力主要来自分类、摘要与按需阅读，必须在中文/英文混合问题上评估。

PageIndex 的长文档树检索提供了可解释下钻的参考。对短原子记忆，树应建在领域集合上；为每句偏好生成独立多级树会增加无用复杂度。模型摘要和重排不是向量数据库的专属功能。[S08]

## 未采纳的建议

初轮研究中出现过以下设计推断，经复核不作为方案依据：

- 原则固定长期、细节固定短期，或细节一周后自动失效。
- 同项目因来源 Agent 不同就不构成冲突。
- 最新记忆优先覆盖，先让旧要求失效再问人。
- 无引用就自动归档，重复多次就自动升级为全局原则。
- 文件无法表达关系，或模型重排必须使用向量。
- 直接把 MEMORY.md 作为每次启动自动注入的记忆入口。
- 用公开 benchmark 百分比、未测规模或模型印象作产品性能承诺。

PARA、Johnny Decimal、Zettelkasten、A-MEM、Hindsight 在探索阶段有研究，但部分分类细节仅有二手/摘要证据，或尚未完成独立原文核验。最终方案不依赖这些未经核验的具体编号规则、算法参数或许可断言。可在后续专题中进一步考察，不阻碍当前设计。

## 来源台账

| ID | 一手出处 | 支持的最小事实 | 本地快照 |
|---|---|---|---|
| S01 | [Basic Memory README](https://github.com/basicmachines-co/basic-memory) | “Just files plus a local SQLite index” | [原文](../research/evidence/basic-memory_README.md) |
| S02 | [OpenViking README](https://github.com/volcengine/OpenViking) | L0/L1/L2；embedding/VLM 依赖 | [原文](../research/evidence/openviking_README.md) |
| S03 | [LangMem 概念](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) 与 [后台教程](https://langchain-ai.github.io/langmem/background_quickstart/) | profile/collection；background formation | [概念原文](../research/evidence/langmem_conceptual_guide.html)、[教程原文](../research/evidence/langmem_background_quickstart.html) |
| S04 | [Letta sleep-time](https://www.letta.com/blog/sleep-time-compute/) | primary/sleep-time 职责分离 | [原文](../research/evidence/letta_sleep_time_compute.html) |
| S05 | [Graphiti README](https://github.com/getzep/graphiti) | episodes provenance 与 bi-temporal | [原文](../research/evidence/graphiti_README.md) |
| S06 | [Mem0 prompts.py](https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py) | 四操作模板和 ADD-only 模板并存 | [原文代码快照](../research/evidence/mem0_prompts.py) |
| S07 | [QMD README](https://github.com/tobi/qmd) | 社区检索机制与数据库依赖，仅作对照 | [原文](../research/evidence/qmd_README.md) |
| S08 | [PageIndex README](https://github.com/VectifyAI/PageIndex) | vectorless 文档树检索 | [原文](../research/evidence/pageindex_README.md) |
| S09 | [LongMemEval README](https://github.com/xiaowu0162/LongMemEval) | 五类长期记忆能力 | [原文](../research/evidence/longmemeval_README.md) |

访问日期均为 2026-10-08。main 分支和网站会变化，引用本轮原文快照可复核当时内容；开发时依赖具体库或协议版本还要重新检查。入口能力的独立台账见 [入口与模型路由](06-integrations-and-model-router.md)。
