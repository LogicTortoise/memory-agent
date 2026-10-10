# 代码目录与架构对应

这是未来代码结构设计。当前目录仍只包含设计与研究工件，不创建空实现、安装依赖或生成可执行产品。核心原则是接口层调用应用服务，应用服务通过 ports 使用基础设施；业务规则不反向依赖 UI、CLI 或具体模型。

```text
memory-agent/
  pyproject.toml
  src/memory_agent/
    __main__.py
    contracts/
      events.py              # ConversationEvent、source locator
      batches.py             # WorkUnit、BatchSpec、BatchResult
      memory.py              # 原子记忆、scope、冲突和决定
      commands.py            # Request/Response、operation params
      query.py               # RecallRequest、QueryPlan、结果
      audit.py               # AuditEvent 与 UI DTO
    domain/
      scope.py               # 范围交集与 unknown
      lifecycle.py           # 状态迁移与允许动作
      batching.py            # 按轮数/token 的纯分批规则
      query_policy.py        # 不可漂移的 query 约束
    application/
      ports.py               # Store/Reader/Provider/Notifier 等协议
      scan.py
      plan_batches.py
      process_batches.py
      reconcile.py
      recall.py
      review_taxonomy.py
      decide.py
      audit_queries.py
      dispatch.py            # 命令分派，不拼 shell
      recovery.py
      backup.py
    adapters/
      sessions/
        base.py              # discover/inspect/read_delta/normalize 协议
        claude_code.py
        codex.py
      models/
        router.py
        claude_cli.py
        structured_output.py
      telegram/
        client.py
        receiver.py
        formatter.py
    infrastructure/
      sqlite/
        connection.py
        repositories.py
        migrations/0001_initial.sql
      files/
        segments.py
        objects.py
        commits.py
        locks.py
        privacy.py
      search/
        text_navigation.py
        tokenizer.py
        bm25_query.py        # 仅 recall 导入，查询时计算
      process.py             # 超时/取消/子进程组
      clock.py
    worker/
      loop.py
      schedules.py
      leases.py
    interfaces/
      cli/
        main.py
        json_io.py
      http/
        app.py
        audit_routes.py
        command_routes.py
        local_auth.py
      mcp/                   # 后续可选，仅翻译协议
    prompts/
      extract_v1.md
      reconcile_v1.md
      query_rewrite_v1.md
      summarize_v1.md
    resources/
      default-config.toml
      ui-dist/               # 构建产物，开发源码在 web/
  web/
    package.json
    src/
      app/
      pages/
        OverviewPage.tsx
        SourcesPage.tsx
        BatchDetailPage.tsx
        MemoriesPage.tsx
        QueryAuditPage.tsx
        SystemPage.tsx
      components/
        PipelineStatus.tsx
        BatchTable.tsx
        EvidenceViewer.tsx
        ProposalDiff.tsx
        QueryVariantList.tsx
      api/
        client.ts
        generated-types.ts   # 从 contracts/OpenAPI 生成
      theme/
        tokens.css
        layout.css
    tests/
  tests/
    fixtures/sessions/{claude_code,codex}/
    fixtures/contracts/
    unit/
    integration/
    failure_recovery/
    evaluation/
  scripts/
    build_ui.sh
    check_contracts.sh
  deploy/
    launchd/
  docs/
  design/                    # 静态 UI 设计稿
  research/                  # 原文与调研记录，不是运行依赖
```

## 依赖规则

- contracts 只做类型、序列化和校验；domain 只做确定性规则。它们不导入 SQLite、FastAPI、React 或 CLI provider。
- application 组织具体用例并依赖 ports。interfaces 只负责传输、身份与错误映射，不自己写 SQL/文件或复制业务规则。
- adapters/infrastructure 实现 ports，不能反向调用界面层。worker 调 application，与外部 CLI 复用同一服务。
- bm25_query 仅可被 recall 链路使用；scan、batching、reconcile、taxonomy review 不依赖它。
- web 只依赖 API DTO，不访问 SQLite、不接触 vault 路径、不存 bot token/模型凭据。
- prompts 版本进入任务 fingerprint；修改提示词不能静默复用旧抽取缓存。SQLite schema 的改动必须带编号迁移和恢复检查。

## 运行数据与代码分开

运行 vault 由 config/CLI 指定，state.sqlite、session 证据、模型工件和私人记忆都不落在 src/web 目录，不提交到源码仓库。研究目录下下载的社区代码仅作资料，不导入执行。

测试夹具必须脱敏或人工构造，标注来源格式/适配版本，不能复制用户整份真实 session 入库。构建前端只更新资源产物，不能修改业务 schema；契约变化由负责 contracts 的开发步骤处理。

## 修改范围控制

每个开发任务按 [开发工作包](07-delivery-and-evaluation.md)中的拥有目录实施。需要跨越未授权模块时，先提交接口变更说明和受影响验收，不顺手重构其他部分；它是工程评审约定，不需要在每个普通编辑前反复向用户申请许可。

UI 任务不能修补后端冲突规则，Reader 任务不能修改记忆分类，检索任务不能改变记录策略。优先用 contract fixture 与 mock port 对接，避免某一模块尚未开发时猜测另一个模块的返回字段。
