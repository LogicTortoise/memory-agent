# Memory Agent 研究证据与复核

本轮使用实际 Claude Code CLI 子进程进行专题调研及独立核验，主控负责最终综合。研究报告包含事实、推断和局限，未经筛选的子报告不是产品规范。采用的方案以 `docs/` 为准。2026-10-08 最新设计为独立项目、session 增量扫描、默认 5 轮批处理、查询时可选 BM25、SQLite 运行/审计状态及本地审计 UI。原始研究报告/运行记录中的旧采集方式、中控依赖和旧存储限制属于历史探索，不是当前方案。

## 研究输出

- [社区记忆机制初轮报告](../.workspace/codex-subagents-kit/runs/20261008-memory-design/outputs/A-community.md)
- [文件索引与分类初轮报告](../.workspace/codex-subagents-kit/runs/20261008-memory-design/outputs/B-files-index-taxonomy.md)
- [入口与基础设施初轮报告](../.workspace/codex-subagents-kit/runs/20261008-memory-design/outputs/C-integrations-runtime.md)
- [社区来源独立核验](../.workspace/codex-subagents-kit/runs/20261008-memory-design/outputs/D-source-audit.md)
- [入口能力独立核验](../.workspace/codex-subagents-kit/runs/20261008-memory-design/outputs/E-integration-audit.md)
- [设计独立审查](../.workspace/codex-subagents-kit/runs/20261008-memory-design/outputs/F-design-review.md)
- [审查意见处理](review-resolution.md)

初轮 A/B/C 存在未经证据支持的推断和错误接入建议，已经由 D/E 和主控筛选。尤其不能从这些报告直接复制“按最新覆盖”“原则等于长期”“Codex 没有显式 skill”“Codex hooks 没有 transcript_path”等断言。

## 原文证据

[evidence](evidence/) 保存 Claude Code 核验任务实际下载的公开原文。CLI 探测快照另见运行目录的 `manifests/runtime-probe.json`，E 报告所称 txt 文件未实际落盘，不作为证据。代码文件如 `mem0_prompts.py` 是社区源码的引用快照，不是本项目实现。它们不作为脚本运行。

[SOURCE-MANIFEST.json](SOURCE-MANIFEST.json) 记录文件大小和 SHA-256，方便核对本轮引用内容。原 URL/用途分别见来源核验报告和设计文档的来源台账；文档网站或 main 分支后续可能变化。

## 运行与权限边界

[运行目录](../.workspace/codex-subagents-kit/runs/20261008-memory-design/) 保存各任务 prompt、output、日志、preflight、registry 和 audit。所用拓扑是主控综合 + 专题 sidecar + 独立核验；不是本会话原生 Claude Task。

CLI 版本探测为 2.1.280。未覆盖子进程的默认模型或 effort；因 bare 认证未配置，实际使用非 bare 登录路径，显式禁用 hooks、限制工具并采用 dontAsk。未授予研究进程 Edit/Write/Agent/Task；独立核验任务仅允许 curl 在 evidence 目录保存公开资料。非 bare 仍可能加载用户已有配置，属于研究执行隔离的已知局限，不把它当成生产 provider 隔离方案。

未访问用户真实历史会话、未迁移公共私人记忆、未修改八爪鱼运行状态、未安装 hook 或 bot，也未向 Telegram 发消息。

## 后续设计补充

按用户追加要求补充了扫描/分批、脚本 API、SQLite 状态/审计、17 个开发工作包、代码目录和静态 UI 设计。此轮属于对已研究机制的产品设计细化，没有新增社区调研任务，也没有访问真实 session；初轮 6 个 Claude Code 子任务数量不变。UI 稿已由本地浏览器渲染核对，文档与示例校验另见 document-validation.json。
