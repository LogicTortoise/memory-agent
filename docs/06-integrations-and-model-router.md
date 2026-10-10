# 入口 Reader 与模型路由

自动记录来自本地 session 增量扫描，不以用户消息 hook 为主路径。Claude Code/Codex Reader 将各自记录格式转换成统一事件；脚本、worker 和 UI 调用同一应用服务。显式召回仍使用命令/skill。

## 接入职责

| 能力 | 首版做法 | 验收边界 |
|---|---|---|
| Claude Code session | 配置允许目录，Reader 识别消息、角色、turn/parent、重放和来源位置 | 固定适配版本与脱敏 fixture，不读取整份私人历史来猜格式 |
| Codex session | 独立 Reader 处理本地 session/rollout 记录与分支 | 命令历史不是完整对话；双重日志表示不重复产生用户事件 |
| 增量发现 | 周期扫描注册 roots + 文件身份/完整记录游标 | 续写、部分尾行、轮转与改写可恢复；mtime 仅是线索 |
| 人工/外部导入 | 脚本传标准事件到 capture 应用服务 | 仍经分批、证据与范围校验，不直接写 active 记忆 |
| 前台 hook | 非必需；未来最多作为文件变更提示 | 关闭 hook 不影响完整的扫描链路 |
| 显式召回 | Claude 的 user-invocable skill；Codex 的禁止隐式调用 policy | 不扫描到“回忆一下”就替用户执行召回 |
| 审计 UI | 用户主动查看、筛选和决定 | 不自动向 Agent 注入记忆，不因刷新触发模型 |

Reader 协议与 source locator 详见 [扫描与分批](09-session-scan-and-batching.md)。官方 hooks 文档可佐证 transcript 路径和格式限制，但不能因此保证所有历史版本的 session JSONL 具有同一 schema。具体字段与 roots 在开发时通过受支持版本 fixture 核验，未知格式明确报错，不静默跳过。

## 显式召回包装

Claude Code skill 使用 `disable-model-invocation: true` 与 `user-invocable: true`；Codex skill 在 `agents/openai.yaml` 设置 `policy.allow_implicit_invocation: false`。前者可用 `/memory recall …`，后者显式选择 memory skill；包装器调用同一 recall 应用服务。

外部脚本使用明确 recall 命令，审计 UI 的查询按钮也是显式入口。自然语言关键词只由当前交互入口识别，不从历史 session 扫描中触发。普通扫描、整理与 UI 概览刷新均没有默认召回行为。

首版依靠显式命令/skill policy 和接口 activation 标记表达用途，不把客户端自报 explicit=true 当作安全证明。MCP 读取能力后续再开放，只有可信交互入口可提供授权时才做硬门控；同一 OS 用户可直接读文件，系统不宣称对该用户实现不可绕过的隔离。

## 独立后台运行

项目提供 `memory worker`，直接运行于用户机器，负责定期 session 扫描、日/周/月任务到期检查、失败重试、进度与通知。它只认识整理、分类复查、冲突决定和通知这几类固定工作，不建设通用任务队列或外部中控 API。

默认日 02:30、周日 03:30、月初 04:00，使用 Asia/Shanghai，属于可配置建议。SQLite 的 runtime_state 记录调度窗口，jobs/runs/batches 保存运行和队列状态；事件是否已消费仍以记忆提交清单为准。日增量未追平时周/月任务先追平或延后。休眠/关机恢复后合并补跑未完成窗口，不重复跑每个已经错过的空周期；无增量不调用模型。

同一 vault 只启动一个 worker，由 OS 进程锁约束。整理作业串行安排，模型作为有超时的异步子进程执行；通知接收和轻量事件落盘可继续运行，记忆提交统一经过 writer 锁。终止或崩溃后依据文件状态恢复未完成批次，既不靠常驻内存保存唯一状态，也不让调度状态代替消费状态。

macOS 首版可由本项目提供 launchd 配置托管 worker；直接执行 CLI 也能完成捕获、召回、整理和本地决定。其他操作系统以后只增加进程托管适配，不改核心文件协议。worker 离线时，各入口继续写自己的 session 文件，下次扫描补录；显式召回仍能读取最近的有效快照。

## 独立 Telegram 适配

本项目直接接入 Telegram Bot API，使用项目自己的 bot 配置。worker 中的 Telegram 适配器发送通知，并通过单个长轮询接收器接收用户回复/按钮决定；首版不需要公网 webhook 或单独 HTTP 服务。

消息接收游标保存在 SQLite runtime_state，update 在 telegram_updates 去重。收到 update 后先把必要决策事件持久化，再推进游标；崩溃重放由 update/decision ID 去重。同一个 bot 只允许一个接收器，启动检测失败时提示配置问题，不与其他进程争用更新流。

发送依据本地 outbox，接收依据允许的 chat/user、提案版本和短期 token 校验。网络中断只使消息待发或决定延后，不影响其他入口捕获和无冲突记忆提交。通知模板、限频、重试和决策状态机全部属于本项目。

未配置 Telegram 时，`memory conflicts` 和 `memory decide` 可直接查看、处理同一批提案。完整 Telegram 验收仍须证明发送→回复→身份/版本校验→本地提交闭环，不能以单向发送成功代替。

机器人回显不作为用户原话；只有验证过的明确用户决定才生成 DecisionEvent。Telegram 凭据通过环境变量或系统凭据存储提供，项目配置只保存引用。本轮不创建 bot、不读取凭据、不发送消息。

## 模型路由的最小抽象

采用一个可替换的 `ModelProvider.run(ModelTask) -> ModelResult` 接口。协调器负责业务步骤，provider 只负责调用模型。首版实现 Claude CLI provider；直接 API 或本地模型可在后续接入，业务模块不依赖某个 CLI 的 stdout 形状。

| 任务 | 输入 | 输出 schema | 调用策略 |
|---|---|---|---|
| extract | BatchSpec，默认最多 5 轮且受 token 限制；少量有来源前文 | candidates + evidence spans + scope/expiry uncertainty | 适配弱模型，不同时做全库整理；目标与 context_only 分开 |
| reconcile | 新候选及范围相交的原子记录/证据 | add/corroborate/clarify/conflict/reject + target IDs | 先规则筛选，只对可能相关集合调用 |
| classify | 候选、当前领域定义/别名 | 领域/标签建议及不确定项 | 与 extract 可同一次返回，保持单独 schema |
| summarize | 同范围、同快照的一组记录 | 带 ID 引用的概览和遗漏检查字段 | 只对变更领域调用；生成失败保留原子清单 |
| review_taxonomy | 变更领域、统计指标、检索反馈 | 拆分/合并/改名提案及影响集合 | 周/月运行，范围变更不能自动提交 |
| expand_query | 显式问题、导航摘要、已给范围 | 1 个等义改写 + 至多 2 个扩写，原 query 不变 | 可选，精确查询跳过 |
| rerank | 问题、有限候选、作用范围 | 候选 ID 排序、简短理由 | 可选；超时返回本地排序 |

不按未经测试的印象宣称某家模型擅长哪种记忆工作。默认使用现有 Claude Code 环境模型，具体任务路由通过同一评测集测成本和质量后设置。高风险范围判断可升级一次模型，仍无法确定就请求用户澄清，不能无限调用“更聪明的 Agent”。

## ModelTask 与 ModelResult

ModelTask 至少包含 `job_id/batch_id/target_unit_ids/context_refs/batch_policy/input_token_count/task_type/schema_version/prompt_version/input_hash/base_commit_id/context_refs/privacy_class/output_schema/deadline/max_output_tokens/budget/provider_policy`。传输内容由协调器从来源/候选挑选，模型没有自行读整库、改文件、发送 Telegram 或联网查用户资料的权限。

ModelResult 包括 `status/validated_payload/provider/model/duration/usage/cost_estimate/attempts/error_code`。JSON 合法不等于语义正确，还要检查引用的 ID 存在、quote 原样匹配、scope 没扩展、删除/覆盖是否有决定、摘要引用是否来自输入集合。

结构错误最多修复一次；瞬时网络失败最多重试两次并退避，所有尝试共享总 deadline。认证错误直接停止对应 provider；预算耗尽时将任务保留待处理，不能删事件或把空结果当成“没有值得记录的信息”。

## Claude CLI 执行边界

使用非交互 `claude -p` 的结构化输出能力，实际参数按安装版本探测。JSON envelope 的 exit code、`is_error` 和结构化结果都要检查，不能只看进程退出 0。CLI 对预算的估计不等于账单。

无工具的总结/抽取任务显式限制工具集，关闭 hooks、自动记忆、插件/MCP 自动加载和会话持久化，避免后台调用产生可被重新扫描的用户会话记录。`--bare` 只在相应 API key/helper 认证已就绪时使用；它不读取 OAuth/keychain，本次运行探测也显示 bare 认证未配置。OAuth 场景使用本机支持的 safe mode/显式隔离设置，并通过安装测试确认 hooks 不触发、上下文不自动混入。不能为了省启动时间把凭据复制进项目。

运行在受控工作目录，将必要上下文通过 stdin 或专门的只读输入工件传入。provider 禁止 shell 工具、文件写工具、网络工具和子 Agent；进程级权限不能只靠 prompt 文案保证。首版不使用 bypassPermissions 作为默认调用方式。

外部进程超时由宿主取消整组子进程，采集 stderr 的必要诊断；残缺输出不进入候选库。provider 最终结果写入结果工件，协调器验证后读取，完整结果不依赖终端日志或通知文案搬运。

## 隐私和回流控制

允许云模型不代表把整个会话库发送出去。后台抽取传必要用户原话及解释上下文；冲突判断只传相交记录；召回只传相关候选和问题。标为 local_only 的内容不进入云 provider；无本地模型时保留待处理并说明原因，不偷偷降级隐私策略。

每个后台事件标记 `origin=memory_agent` 或 `maintenance`，采集器对这些会话跳过。召回结果和整理摘要带派生来源标识；后续出现于工具/助手消息时只当上下文，不能成为用户证据。Telegram 按钮决策才生成新的 user_decision 事件，机器人回显不能成为新的偏好。

日志记录 ID、hash、任务用量和错误类别，默认不打印整段用户原话。模型输出缓存按完整任务输入 hash（含旧记忆集合及版本、base commit、taxonomy/scope）+ prompt/schema/provider 策略版本隔离；清理记忆时同时失效相关缓存。

## 脚本和 UI 共用协议

完整命令、Request/Response、异步 job、退出码、幂等/取消与 HTTP 接口集中定义在 [脚本与服务接口](10-script-and-service-api.md)，这里不维护第二套字段。CLI、worker、HTTP、可选 MCP 统一依赖 contracts 与 application 层。

BM25、query 改写/扩写仅存在于 recall；reconcile 通过范围、实体和文本对照选择历史，不调用查询排序模块。查询 trace 和模型调用用量进入 SQLite 审计，正文/模型结果保存在工件文件。

## 来源与验证边界

- [Codex 官方 Hooks](https://learn.chatgpt.com/docs/hooks)及[本轮快照](../research/evidence/codex_hooks.html)：可定位 transcript，但原文明确格式不是稳定接口。
- [Codex Build skills](https://learn.chatgpt.com/docs/build-skills)及[快照](../research/evidence/codex_skills.html)：显式调用与 allow_implicit_invocation。
- [Claude Code Hooks](https://code.claude.com/docs/en/hooks)及[快照](../research/evidence/cc_hooks.html)：提供来源路径的历史研究依据，不作为首版必须安装的捕获 hook。
- [Claude Code Skills](https://code.claude.com/docs/en/skills)及[快照](../research/evidence/cc_skills.html)：禁止隐式调用的包装能力。

本轮没有扫描用户真实 session，也未验证新的 parser 实现。Reader 的实际支持矩阵、UI 与各模型预算都在开发工作包中设有验收，不能由架构设计冒充已经可用。
