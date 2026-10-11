import { Link } from "react-router-dom";

import {
  Banner,
  EmptyState,
  ErrorState,
  PageTitle,
  StatusPill,
  formatNumber,
  formatTimestamp,
} from "../components/primitives";
import { Card } from "../components/Card";
import { CardCarousel } from "../components/CardCarousel";
import { useOverview, useAuditEvents } from "../hooks/queries";
import { describeProjection } from "../api/client";

export function OverviewPage() {
  const overview = useOverview();
  const audit = useAuditEvents({ page_size: 5 });
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

      <CardCarousel title="扫描覆盖 → 已规划 → 已抽取 → 已提交" wide>
        {(["scanned", "planned", "extracted", "committed"] as const).map((stage) => (
          <div key={stage} className="card" style={{ padding: "14px 16px" }}>
            <div className="muted" style={{ fontSize: 12 }}>
              {stage}
            </div>
            <div
              style={{ fontSize: 24, fontWeight: 600, marginTop: 4 }}
              className="tnum"
              data-testid={`stage-${stage}`}
            >
              {formatNumber(pipeline_stages[stage] ?? 0)}
            </div>
          </div>
        ))}
      </CardCarousel>

      <CardCarousel title="来源与事件" count={5}>
        <KpiCard label="来源文件" value={counts.sources_total} />
        <KpiCard
          label="不支持来源"
          value={counts.sources_unsupported}
          tone="pending"
        />
        <KpiCard label="事件总数" value={counts.events_total} />
        <KpiCard label="用户事件" value={counts.events_user} />
        <KpiCard label="轮次" value={counts.turns_total} />
      </CardCarousel>

      <CardCarousel title="处理批次" count={5}>
        <KpiCard label="待处理" value={counts.batches_pending} tone="pending" />
        <KpiCard label="执行中" value={counts.batches_running} tone="info" />
        <KpiCard label="已完成" value={counts.batches_succeeded} tone="ok" />
        <KpiCard label="失败/已废弃" value={counts.batches_failed} tone="error" />
        <KpiCard label="候选集" value={counts.candidate_sets_total} />
      </CardCarousel>

      <CardCarousel title="记忆与决定" count={5}>
        <KpiCard label="活跃记忆" value={counts.memories_active} tone="ok" />
        <KpiCard label="被替代记忆" value={counts.memories_superseded} tone="muted" />
        <KpiCard label="待决冲突" value={counts.conflicts_open} tone="pending" />
        <KpiCard label="审计事件" value={counts.audit_events_total} />
        <KpiCard
          label="后台运行"
          value={counts.worker_lease_active ? "活跃" : "空闲"}
          tone={counts.worker_lease_active ? "info" : "muted"}
          isText
        />
      </CardCarousel>

      <CardCarousel title="快捷导航" count={5}>
        <Link to="/sources" style={{ textDecoration: "none", color: "inherit" }}>
          <Card variant="swipe" aria-label="查看来源">
            <div className="muted" style={{ fontSize: 12 }}>来源</div>
            <div style={{ fontSize: 16, fontWeight: 500, marginTop: 4 }}>查看来源 ({counts.sources_total})</div>
          </Card>
        </Link>
        <Link to="/batches" style={{ textDecoration: "none", color: "inherit" }}>
          <Card variant="swipe" aria-label="查看批次">
            <div className="muted" style={{ fontSize: 12 }}>批次</div>
            <div style={{ fontSize: 16, fontWeight: 500, marginTop: 4 }}>查看批次</div>
          </Card>
        </Link>
        <Link to="/memories" style={{ textDecoration: "none", color: "inherit" }}>
          <Card variant="swipe" aria-label="查看冲突">
            <div className="muted" style={{ fontSize: 12 }}>冲突</div>
            <div style={{ fontSize: 16, fontWeight: 500, marginTop: 4 }}>待决冲突 ({counts.conflicts_open})</div>
          </Card>
        </Link>
        <Link to="/queries" style={{ textDecoration: "none", color: "inherit" }}>
          <Card variant="swipe" aria-label="查询审计">
            <div className="muted" style={{ fontSize: 12 }}>查询</div>
            <div style={{ fontSize: 16, fontWeight: 500, marginTop: 4 }}>查询审计</div>
          </Card>
        </Link>
        <Link to="/audit" style={{ textDecoration: "none", color: "inherit" }}>
          <Card variant="swipe" aria-label="审计日志">
            <div className="muted" style={{ fontSize: 12 }}>审计</div>
            <div style={{ fontSize: 16, fontWeight: 500, marginTop: 4 }}>审计日志</div>
          </Card>
        </Link>
      </CardCarousel>

      <CardCarousel
        title="最近审计"
        count={audit.data?.items.length ?? 0}
        data-testid="overview-recent-audit"
      >
        {(audit.data?.items ?? []).map((evt) => (
          <Card key={evt.audit_id} variant="swipe" aria-label={evt.action}>
            <div className="muted" style={{ fontSize: 12 }}>{formatTimestamp(evt.occurred_at)}</div>
            <div style={{ fontSize: 14, fontWeight: 500, marginTop: 4 }}>{evt.action}</div>
            <div className="muted tnum" style={{ fontSize: 11, marginTop: 4 }}>{evt.object_id}</div>
          </Card>
        ))}
      </CardCarousel>
    </div>
  );
}

function KpiCard({
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
    <div className="card" style={{ padding: "14px 16px" }}>
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
      <div style={{ marginTop: 6 }}>
        {isText ? (
          <StatusPill tone={tone}>{String(value)}</StatusPill>
        ) : (
          <span className="tnum" style={{ fontSize: 22, fontWeight: 600 }}>
            {formatNumber(value as number)}
          </span>
        )}
      </div>
    </div>
  );
}