# Memory Agent v1.0 开发计划（v0.1 边界）

设计时间 2026-10-08。本计划承接 [架构](03-architecture.md) 与 [开发工作包](07-delivery-and-evaluation.md) 的整体设计，圈定首个可运行版本（v0.1）的边界。本文件是 v0.1 唯一权威范围声明；旧 doc/00–13 中的"本轮只交付设计"已不再有效。

## v0.1 目标

一个独立 Python 包，能：

- 增量扫描两类外部边界（Claude Code、Codex）的本地 session 文件，把新增完整 JSONL 记录转成标准事件并登记；
- 把事件按逻辑轮次组装，按"每批最多 5 轮 + 6000 输入 token"小批规划（可调）；
- 调用一个类型化模型 provider（v0.1 内置 Claude Code CLI 与一个用于测试的 fake provider），抽取用户明确要求/偏好/自述事实；
- 严格校验 quote 与源 code-point 切片一致，与同批/已有记忆做保守冲突检查；
- 将非冲突记忆以不可变 Markdown+JSON 发布到 vault，原子切换 HEAD，DB finalize；
- 通过 `init`/`scan`/`batch plan`/`batch process`/`maintain`/`recall`/`conflicts`/`decide`/`doctor` 等同步 CLI 子命令工作；
- 默认首次启动不读任何真实历史，只从安装时刻起登记增量；历史 backfill 在 v0.1 内不可用（无命令，无 recipe；详见 README "What is not in v0.1"）。

不做：worker、schedules、Telegram、HTTP 审计 API、React UI、备份/隐私删除设计、自动 worker 等增量来源、查询时 BM25 重排（v0.1 的 recall 仅 keyword 基线）。这些明确延后到 v1+。

## 优先级与范围

| ID | 主题 | 范围 | 依赖 |
|---|---|---|---|
| **P0.0** | 搬迁与可执行骨架 | 目标路径 `/Users/Hht/Documents/10.github/memory-agent`，Python 3.12+ 包，提供 `memory` 与 `python -m memory_agent` 入口；`.gitignore` 排除 vault/DB/logs/secrets；README 给出安全 fixture 化的快速开始 | — |
| **P0.1** | 契约 + 状态 + 内容提交 | 版本化事件/候选/记忆/CLI 响应契约；未知字段/版本错误/非法状态拒绝；JSON Schema 导出；SQLite WAL+migrations+唯一约束+lease token+审计 metadata；不可变 segment/候选 JSON；记忆以 Markdown+证据+commit manifest 发布；segment-before-DB 与 HEAD-before-finalize 恢复窗口；记忆正文不入库 | P0.0 |
| **P0.2** | 两种 Reader + 增量扫描 | Claude Code / Codex 分别的 adapter；显式配置的 roots；UTF-8 完整行；增量 offset 持久化在 SQLite；append、split final line、重复扫描零新增；同一文本在两个 session 仍是不同事件；未知格式报错并标记；model call 数始终为 0 | P0.1 |
| **P0.3** | 轮次组装 + 小批规划 | 用户输入开始一轮；assistant/tool 留在该轮；保留 branch/lineage、open/incomplete 状态、context_only；默认 5 轮/6000 输入 token；永不混 session/branch；同一 event/work_unit 仅一个 active target 分配；过载输入标 needs_context/oversize | P0.1, P0.2 |
| **P0.4** | 抽取、校验、保守 reconcile、发布 | 类型化 Claude CLI provider + bounded prompt/output/timeout + 注入式 fake provider；只抽用户明确要求/偏好/自述事实；quote == source Unicode code-point slice；role/origin/scope 校验；JSON/auth/timeout/空输出视为失败/可重试，**不**当 no_memory；**scope 缺失/level 与 ID 不一致一律拒绝（只有显式 `global` 才是 global，裸 `"project"` 这类欠定 scope 不放行）**；scope 不明/可能冲突写 pending；非冲突完全验证的候选才发布；**同批互相矛盾且 scope 交叠的候选整组一起进 pending，不按 source order 挑赢家**；批 completed 仅在发布/finalize 或 durable pending/no_memory 决策后；prepare 不写 projection / 不 finalise batch；publish 必须 HEAD==parent；**finalize 从 commit manifest 里已提交 memory 的真实 scope/domains 写 memory_projection（不再写空 `{}`）**；recovery 对 HEAD-at-parent 的 prepared abort 而不是 auto-publish，对 missing/corrupt manifest 返回 recovery_required 并阻断新写；same-batch 候选互相比较而非只比已有 memory | P0.1, P0.3 |
| **P0.5** | 显式可用 CLI + 召回 | `init`/`scan`/`batch plan`/`batch process`/`maintain`/`recall`/`conflicts`/`decide`/`doctor` 全部 JSON-only stdout；同步路径不依赖 worker；`maintain` = scan→plan→process；`recall` 仅在显式命令；keyword baseline + project/domain filter；**recall 的 quote 必须是从 immutable segment 解析回来源事件、并逐字校验 `source_text[span_start:span_end] == quote` 后的精确切片，locator 是该事件在原始 JSONL 中的 byte 区间 + record ordinal；证据缺失/损坏则整条 omit 并给出 warning，绝不返回 Markdown 偏移或近似引文**；保守决策需 current proposal revision 与 idempotency key；稳定退出码；stdout 不输出调试文本 | P0.1–P0.4 |

### P0 验收

P0.0
1. `pip install -e .` 在干净 venv 中成功；`memory --help`、`python -m memory_agent --help` 都输出帮助。
2. `.gitignore` 覆盖 vault/、state/、logs/、.secrets、cache。
3. README 给出"构造脱敏 fixture → init → maintain → recall"快速开始。

P0.1
1. 测试演示事务回滚、duplicate ingestion 幂等、旧 lease 不能 finalize、注入崩溃恢复后无丢/重记忆。
2. Schema 导出文件存在且覆盖核心契约；错版本/未知字段/非法状态返回稳定的结构化错误码。
3. 记忆 Markdown 文件含 frontmatter、quote、span、scope、project、provenance；证据 JSON 不可改；HEAD 是原子切换的；commit manifest 列表完整。

P0.2
1. 测试：append 三步 → 零新事件、partial final line → 不推进、重复扫描 → 零新事件、同一文本在两个 session 仍是不同事件、未知格式报错、扫描期间模型调用数为 0。
2. 默认 `init` 不读任何旧历史，登记文件到 end-of-file baseline；backfill 需显式 `--since/--until`。
3. 示例 vault 不含任何真实 user data。

P0.3
1. 测试：20 普通轮 → 4 批；12 轮 → 3 批；interleaved tools 不增加批数；重 plan 零 duplicate targets；oversize/needs_context 显式标记。
2. 同 session/branch 严格串行；不同 session 在 commit 阶段才交叉读。

P0.4
1. 测试：用户原句 vs 引用助手建议被拒绝；同批/跨批矛盾要求进 pending；retry 幂等；bad provider outputs 是 failed/retryable，不是 no_memory；HEAD/DB 恢复后没有虚假 completed。
2. v0.1 是初始保守 reconcile，不宣称完整语义冲突召回；测试在受控 fixture 上验证明确行为。

P0.5
1. 子进程端到端测试：从构造的 session append → maintain → recall 返回带证据的结果；二次 maintain 零 duplicate memory/模型调用；冲突保持 pending 直到 decide；malformed/versioned 请求被拒绝。
2. 默认首次启动不读真实历史（即使路径包含真实 home 也不读）。

### P1（v1 之后）

更丰富的源修订 + 长轮 fragmentation；完整 scope 语义；查询时 BM25 + 受约束改写/扩写；通用 `call` 异步/任务；worker/schedules；Telegram；HTTP 审计 API；React UI；备份/隐私删除；完整容量/语义评测。这些明确记录在 README，不做装饰性 stub。

若 P0 安全需要的最小前置（例如最小 privacy deletion 屏障或最小恢复检查）则在 P0 自身中实现并说明；否则不在 P0 范围。

## 输入/输出与依赖

| 项 | v0.1 | 备注 |
|---|---|---|
| Python | 3.12+ | pyproject.toml 锁定 |
| 依赖 | 标准库 + `pydantic>=2.6` | 可选 `tomli` 兼容 Python 3.11 fallback |
| SQLite | 标准库 sqlite3，WAL、foreign_keys、busy_timeout | 唯一运行状态源 |
| LLM | Claude Code CLI（首版）；fake provider（测试） | 与 `claude -p` JSON 输出契约 |
| 输入来源 | Claude Code `<root>/.claude/projects/**/<sessionId>.jsonl`；Codex `<root>/sessions/<id>.jsonl`（具体 glob 在配置中显式） | reader 检测已知 schema；未知报错 |
| 输出 vault | 任意 `MEMORY_VAULT` 路径 | 默认 `~/.local/share/memory-agent/vault` |
| 时区 | UTC 内部存储；显示按配置时区 | 默认 `Asia/Shanghai` |
| 数据量 | 1 万原子记忆 + 10 万事件的工程目标 | 不在 v0.1 实测，仅作为开发目标 |

## 工程与运行原则

- 架构保留：contracts/domain 独立于 IO；application 通过 ports；CLI 仅做协议翻译。SQLite 权威运行状态，文件权威内容。模型绝不直写记忆文件。
- 范围严格：v0.1 只做 P0 范围；不构建 worker/Telegram/UI/打包自动化超出的部分。
- 聚焦的、有意义的测试 + 真实本地 install + CLI 子进程对隔离临时 vault 的 e2e；可选 live Claude Code provider smoke 仅在能安全做时尝试，否则明示。
- 文档与代码不一致时：更新本计划 + README + doc/15，明确 implemented/deferred；不得改写历史研究/原始目标。
- 不提交、不推送，除非后续明确授权。

## 风险与已知边界

- v0.1 recall 只用 keyword + scope filter，不做 BM25 与模型改写/扩写；可能漏召回近义词或同义词。
- v0.1 的 project/domain filter 是"保守排除"语义：global 记忆在任何 filter 下都返回；只匹配被点名的 facet；`task`/`session` 级记忆无法回答"属于哪个 project/域名"，在带 filter 的查询里被排除（并在 `warnings` 里说明），不做猜测。未带任何 filter 的"通用查询"默认只取 global + domain 规则，project / task / session 级记忆被隐藏，跨 project 浏览必须显式声明（见 docs/04 §"召回"）；`scope_json.level` 缺失或未识别的行一律排除并 warn，绝不静默 widen 到 global。
- v0.1 的 recall 宁缺毋滥：证据解析不出或 span 切片对不上的记忆**不返回**，只给 warning。这会牺牲召回率，换取"每条返回的引文都能在原始 JSONL 里逐字定位"。
- v0.1 reconcile 是 conservative：scope 不明或 predicate 不可比时直接 pending，不试图"聪明的全局仲裁"；同批矛盾整组 pending，不按出现顺序挑一个发布。
- 本地仓库没有配置 Git remote，也没有发布过 GitHub 仓库，所以 `pyproject.toml` 不声明 Homepage/Source/Issues URL。
- Claude CLI provider 在无 OAuth/凭据时不可用；fake provider 是默认测试路径。
- v0.1 不内置完整 privacy deletion；目前"删除"等同于删文件/重置 vault。
- vault 与真实 home 共存时，默认 `init` 仍不会扫真实 home（除非显式 root 指向）。真实数据库的 hit 风险为零。