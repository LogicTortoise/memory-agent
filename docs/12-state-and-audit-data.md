# SQLite 状态与审计数据设计

SQLite 负责细碎的运行数据：扫描游标、事件引用、轮次与批次、任务状态、模型调用、通知和审计。Markdown/JSON 文件仍是证据原文、记忆内容、提案和已发布提交的权威来源。数据库不承载向量检索，也不使用 FTS 替代文本记忆索引。

## 数据归属

| 数据 | 权威存储 | 其他位置的含义 |
|---|---|---|
| 标准事件原文与必要上下文 | immutable segment JSON | SQLite 只存 ID、角色、位置、hash 和统计 |
| 批次分配、lease、重试、游标 | state.sqlite | UI 与日志为视图，不再平行维护 state/*.json |
| 批次输入、模型结果、候选正文 | artifacts 下的不可变 JSON | DB 指向工件及其 hash，不复制大段对话 |
| 已确认记忆、冲突提案、分类版本 | objects 文件 + commit manifest + HEAD | SQLite 的 memory/conflict 状态为可校正投影 |
| 已提交的消费结果 | commit manifest 的 applied_batch_ids/target_unit_ids | DB 的 completed 只有与清单一致才成立 |
| 审计事件与调用用量 | SQLite audit_events/model_calls | 导出文件是快照，不作为另一套可写事实源 |
| 删除授权 | privacy/deletions 文件清单 | DB 为便于检索的投影；内容恢复不能撤销删除 |
| 查询 BM25 | 查询进程内存 | 从当次允许读取的有效记忆构建，可丢弃，无数据库搜索服务 |

SQLite 文件放在 vault/state/state.sqlite，配置仍在 config.toml。不得同时维护同义的 JSON 游标和 SQLite 游标。运行库是产品运行数据，不是代码仓库中的数据库文件。

## 关键选型

Python 标准库 sqlite3，显式参数化 SQL 与编号 migration SQL；首版不引入 ORM。开启 foreign_keys、WAL，短写事务设置 busy_timeout；扫描/接收/提交关键路径采用 synchronous=FULL。网络模型调用、长文件读取和用户等待绝不放在数据库事务中。

数据库在本机磁盘，UI 读连接与后台写连接分开，写冲突依靠事务/唯一约束处理。SQLite 锁不能替代文件库的 writer 锁。schema_version 明确检查，升级使用一次性迁移与备份，不保留旧协议兼容分支。

## 表与约束

| 表 | 主键及关键字段 | 关键约束与用途 |
|---|---|---|
| source_files | source_file_id；connector、session/branch、path、identity、generation、adapter_version、complete_offset、anchor_hash、size/mtime、last_scan/error | 游标按逻辑源与 generation 区分；偏移只在 segment 登记事务内推进 |
| segments | segment_id；path、sha256、source_file_id、generation、start/end_offset、created_at | 已登记路径必须存在且 hash 正确；不可变 |
| source_events | event_id；logical_message_id、revision_hash、session_id、branch_id、segment_id、record/span locator、role/kind、source_time、discovered_at、replaces_event_id | UNIQUE canonical_event_key；不按单纯文本去重 |
| turns | turn_id；connector、session/branch、ordinal、revision、state、first/last_event_id | 同 session/branch 的逻辑顺序稳定；open/closed/incomplete 分开 |
| work_units | unit_id；event/turn revision、span、split_policy、parent_unit_id、epoch | 目标内容片段的最小处理单位；不复制正文 |
| batches | batch_id；session/branch、policy_version、input_hash、status、attempt、lease_token、lease_until、next_retry_at | 状态迁移使用预期状态和 lease token 校验 |
| batch_members | batch_id、unit_id、purpose、order、epoch、active_target | purpose=target/context；对 active_target=1 的 unit+epoch 建部分唯一索引；重规划事务释放旧占用后分配新批次 |
| candidate_sets | candidate_set_id；batch_id、artifact_ref、sha256、schema/prompt/model版本、validation_state | 已验证抽取可复用；旧范围校验结果不能直接复用 |
| jobs | job_id；request_id、idempotency_key、request_hash、operation、params_ref、state、cancel_requested、timestamps | UNIQUE client_id+idempotency_key；同 key 不同参数报冲突 |
| runs | run_id；job_id、stage、batch_id、attempt、status、input/output refs、error_code、start/end | 一次尝试一条；失败重试新 attempt，不覆盖历史结果 |
| model_calls | call_id；run_id、purpose、provider/model、prompt/schema、usage、cost_estimate、duration、input/output refs | 正文放工件；记录是否缓存命中与估计量缺失 |
| commit_intents | commit_id；parent_id、manifest_ref、batch_ids、expected_revisions、privacy_epoch、phase | prepared/published/finalized/aborted；承接文件 HEAD 与 DB 状态的一致性恢复 |
| memory_projection | memory_id、revision、status、scope/domain/project refs、commit_id | 只存列表导航数据；以 HEAD 对照修复，不能写成另一份记忆 |
| conflict_projection | conflict_id、revision、status、old/new refs、commit_id | UI 和通知查询入口；决定仍经应用服务提交 |
| audit_events | audit_id、seq、time、actor、action、object_type/object_id、job/run/batch/session/commit/query_id 等可索引关联列、before/after refs、outcome、reason_code、details_json | append-only；更正用新事件，不覆盖旧事件；隐私删除例外需脱敏/删除正文载荷 |
| notification_outbox | notification_id、conflict_revision、channel、payload_ref、status、attempt、next_retry、remote_id | UNIQUE proposal+channel+notification_kind；发送至少一次 |
| telegram_updates | update_id、decision_event_ref、status、received_at | 输入去重；持久化决定事件后推进接收位置 |
| runtime_state | key、value_json、revision、updated_at | 小型调度窗口、Telegram cursor、worker heartbeat；不放记忆正文或秘密 |

ID 为 TEXT，偏移/token/计数为非负 INTEGER，时间用统一 UTC RFC3339 字符串；枚举以 CHECK 或契约校验约束。小型 JSON 参数是 TEXT，由 Python 校验，不要求全文搜索扩展。关联对象使用 FK 或在文件发布恢复时显式验证，不能用未声明字段建索引。

需要的索引仅服务运行查询：batch(status,next_retry_at)、source(connector,session_id)、event(session,branch,source_time)、run(job,stage,start)、audit(object_id,seq)/(correlation_id,seq)、outbox(status,next_retry)。UI 按 seq/时间+ID 游标分页，不做无限 offset 扫描。

## 文件与数据库提交协议

### 采集

segment 内容先写盘并 fsync/rename，然后同一个 SQL 事务插入 segments/source_events 并推进 source_files。失败之前游标不动；落下的孤立文件按 hash 复用或在无引用时清理。消费者只读取 DB 已登记的 segment，不直接 glob 孤立文件当已接收事件。

### 批次与模型输出

planner 在 SQL 事务内登记 batches 和 target membership。claim 使用状态条件更新；提取模型在事务外运行。输出文件持久化后再以正确 lease_token 登记 candidate_sets 与 extracted 状态。旧进程即使恢复也不能覆盖新 lease 的结果。

### 记忆发布

1. 模型在锁外提出变更。协调器获取 writer 锁，确认 HEAD、目标 revision、批次结果与 privacy epoch 未变化。
2. 写好并耐久保存新对象和完整 commit manifest；SQL 登记 prepared intent，绑定 parent、批次与目标单元。
3. 原子替换 HEAD，发布文件快照。HEAD 切换是记忆内容生效的时刻。
4. SQL 短事务核对 intent，标记 finalized，投影新记忆/冲突，完成批次覆盖，追加 audit_events，生成 outbox。事务后才返回 completed。

HEAD 发布与 SQL 事务不是一次原子操作。服务启动、写入前与 doctor 必须恢复未完成 intent：HEAD 仍是 parent 时检查并放弃未发布 intent，保留批次重试；HEAD 已包含 commit 时按 manifest 幂等补齐 SQL finalize。找不到清单或 hash 不符进入 recovery_required，阻止新写入，不凭猜测回滚用户记忆。

发布后 finalize 失败，响应状态为 committed_pending_finalize，带 commit_id；不能返回“未写入”诱使调用方另起请求覆盖。恢复过程可以重复执行，audit/outbox 用稳定 ID 去重。只读查询固定 HEAD，UI 若投影滞后显示“状态对齐中”，不将旧 DB 状态当作当前内容。

### 删除与恢复

删除授权先持久化到独立 privacy 清单，再原子更新 `privacy/HEAD.json` 中的删除版本。查询在开始与返回前读取这个文件版本并重新过滤，不依赖可能滞后的 DB privacy 投影；随后在发布协议内更新当前快照与投影。清单优先级高于旧 HEAD/旧数据库，恢复内容库之前先合并最新删除清单。无法取得最新删除清单的旧备份隔离检查，不直接上线。

## 审计事件设计

动作词表：source.discovered/read/rewritten/error、ingest.segment_committed、batch.planned/claimed/extracted/retried/failed/superseded、memory.proposed/committed、conflict.opened/decided、query.planned/rewritten/retrieved/reranked、taxonomy.proposed/committed、privacy.deleted、recovery.finalized。

actor 为 user_cli/user_ui/user_telegram/worker/model_proposal；模型提出建议与用户批准必须是两种事件。每个事件关联 request/job/run/batch/session/source/memory/commit/query 中实际存在的 ID，允许从 UI 点击逐层追溯。before/after 保存对象 ID、revision 和 hash，不在审计行里复制整段个人记忆。

```json
{
  "audit_id": "aud_example",
  "action": "batch.extracted",
  "actor": {"kind": "worker", "id": "worker_local"},
  "occurred_at": "2026-10-08T02:31:00+08:00",
  "correlation": {"job_id": "job_example", "run_id": "run_example", "batch_id": "batch_s1_01", "session_id": "s1"},
  "object": {"type": "candidate_set", "id": "cand_example", "artifact_ref": "artifacts/candidates/cand_example.json"},
  "outcome": "success",
  "details": {"target_turns": 5, "input_tokens": 4200, "candidate_count": 3, "context_truncated": false}
}
```

审计中的 source text 通过原文引用按需读取；默认日志与列表不展示敏感正文。查询文本、改写和候选 trace 也是个人数据，按可配置保留期保存到工件，仅把引用存入 DB。删除记忆时清理相关查询缓存、审计载荷和工件；可保留不含正文的删除操作凭据。

## UI 统计口径

“新增事件”是本次去重后 source_events 行数；“新增轮次”是本次新建立的 turn，不是 JSONL 行数；“待处理批次”仅计 ready/retry_wait；“执行中”计有效 lease；“已完成批次”必须有 commit manifest 的应用回执；“待确认事项”计开放冲突，不能与批次失败混为一类。

显示扫描覆盖时间、最近 successful scan、积压最早 discovered_at、unknown/unsupported 文件数。进度分成 scanned/planned/extracted/committed 四段，阶段计数不足时不伪造统一百分比。一次扫描尚未结束或新事件继续进入时，总量为变化的估计，界面明确“仍在发现新增内容”。

## 备份与迁移

备份使用 sqlite3 backup API 获取一致数据库快照，不直接复制运行中的 .sqlite 文件而漏掉 WAL。进入短暂发布/采集暂停窗口，先恢复 intent，再固定 HEAD、删除清单和数据库快照；复制被引用的不可变文件时锁定 GC。模型调用可在锁外完成，结果等待恢复写入。

备份包含 state.sqlite 快照、HEAD/manifest、被引用对象/segment/必要工件、最新删除清单及 hashes。纯记忆文件可恢复阅读，但丢失状态库意味着扫描/队列/审计进度需要重新对账，不能承诺“数据库随便删掉也不影响工作”。迁移失败恢复整个一致备份，不单独恢复某张状态表。
