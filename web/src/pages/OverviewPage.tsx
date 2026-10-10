import { Link } from "react-router-dom";

import {
  Banner,
  EmptyState,
  ErrorState,
  PageTitle,
  Section,
  StatusPill,
  formatNumber,
  formatTimestamp,
} from "../components/primitives";
import { useOverview } from "../hooks/queries";
import { describeProjection } from "../api/client";

export function OverviewPage() {
  const overview = useOverview();
  if (overview.error) return <ErrorState error={overview.error} />;
  if (!overview.data) {
    return (
      <div>
        <PageTitle title="处理概览" subtitle="扫描覆盖、批次、待决与投影状态" />
        <EmptyState title="Loading…" hint="连接本地审计 API" />
      </div>
    );
  }
  const { header, counts, pipeline_stages, warnings } = overview.data;
  const projection = describeProjection(header);
  return (
    <div>
      <PageTitle
        title="处理概览"
        subtitle="扫描覆盖、批次、待决与投影状态"
        right={
          <span className="muted" data-testid="last-scan-at">
            最近扫描：{formatTimestamp(counts.last_scan_at)}
          </span>
        }
      />
      {header.projection_state !== "aligned" && (
        <Banner tone={projection.tone === "error" ? "error" : "warn"} title={projection.label}>
          SQL 投影与 HEAD 不同步：lag={header.db_lag_behind_head} ahead={header.db_ahead_of_head}。
          浏览仍可继续；写命令按版本控制语义拒绝过期操作。
        </Banner>
      )}
      {warnings.length > 0 && (
        <Banner tone="warn" title="需要注意的提示">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Banner>
      )}

      <Section title="扫描覆盖 → 已规划 → 已抽取 → 已提交">
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
            gap: 12,
          }}
        >
          {(["scanned", "planned", "extracted", "committed"] as const).map((stage) => (
            <div key={stage} className="card">
              <div className="muted" style={{ fontSize: 12, textTransform: "uppercase" }}>
                {stage}
              </div>
              <div style={{ fontSize: 24, fontWeight: 600, marginTop: 4 }} data-testid={`stage-${stage}`}>
                {formatNumber(pipeline_stages[stage] ?? 0)}
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section title="来源与事件">
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
            gap: 12,
          }}
        >
          <StatTile label="来源文件" value={counts.sources_total} />
          <StatTile label="不支持来源" value={counts.sources_unsupported} tone="pending" />
          <StatTile label="事件总数" value={counts.events_total} />
          <StatTile label="用户事件" value={counts.events_user} />
          <StatTile label="轮次" value={counts.turns_total} />
        </div>
      </Section>

      <Section title="处理批次">
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
            gap: 12,
          }}
        >
          <StatTile label="待处理" value={counts.batches_pending} tone="pending" />
          <StatTile label="执行中" value={counts.batches_running} tone="info" />
          <StatTile label="已完成" value={counts.batches_succeeded} tone="ok" />
          <StatTile label="失败/已废弃" value={counts.batches_failed} tone="error" />
          <StatTile label="候选集" value={counts.candidate_sets_total} />
        </div>
      </Section>

      <Section title="记忆与决定">
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
            gap: 12,
          }}
        >
          <StatTile label="活跃记忆" value={counts.memories_active} tone="ok" />
          <StatTile label="被替代记忆" value={counts.memories_superseded} tone="muted" />
          <StatTile label="待决冲突" value={counts.conflicts_open} tone="pending" />
          <StatTile label="审计事件" value={counts.audit_events_total} />
          <StatTile
            label="后台运行"
            value={counts.worker_lease_active ? "活跃" : "空闲"}
            tone={counts.worker_lease_active ? "info" : "muted"}
            isText
          />
        </div>
      </Section>

      <Section title="快捷导航">
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Link to="/sources">
            <button>查看来源 ({counts.sources_total})</button>
          </Link>
          <Link to="/batches">
            <button>查看批次</button>
          </Link>
          <Link to="/memories">
            <button>待决冲突 ({counts.conflicts_open})</button>
          </Link>
          <Link to="/queries">
            <button>查询审计</button>
          </Link>
          <Link to="/audit">
            <button>审计日志</button>
          </Link>
        </div>
      </Section>
    </div>
  );
}

function StatTile({
  label,
  value,
  tone = "muted",
  isText = false,
}: {
  label: string;
  value: number | string;
  tone?: "ok" | "pending" | "error" | "info" | "muted";
  isText?: boolean;
}) {
  return (
    <div className="card">
      <div className="muted" style={{ fontSize: 12, textTransform: "uppercase" }}>
        {label}
      </div>
      <div style={{ marginTop: 6 }}>
        {isText ? (
          <StatusPill tone={tone}>{String(value)}</StatusPill>
        ) : (
          <span style={{ fontSize: 22, fontWeight: 600 }}>{formatNumber(value as number)}</span>
        )}
      </div>
    </div>
  );
}