import { useEffect, useState } from "react";

import {
  Banner,
  EmptyState,
  ErrorState,
  PageTitle,
  Section,
  StatusPill,
  formatTimestamp,
} from "../components/primitives";
import { useQueryTraceDetail, useQueryTraces } from "../hooks/queries";

export function QueriesPage() {
  const traces = useQueryTraces({ page_size: 50 });
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  useEffect(() => {
    if (!selectedTraceId && traces.data && traces.data.items.length > 0) {
      setSelectedTraceId(traces.data.items[0].trace_id);
    }
  }, [traces.data, selectedTraceId]);
  return (
    <div>
      <PageTitle title="查询审计" subtitle="原 query、改写/扩写、变体命中与依据" />
      {traces.error && <ErrorState error={traces.error} />}
      {!traces.data && !traces.error && <EmptyState title="Loading…" />}
      {traces.data && traces.data.items.length === 0 && (
        <Banner tone="ok" title="还没有查询记录">
          调用 <code>memory recall --retrieval bm25</code> 后这里会出现 trace。
        </Banner>
      )}
      {traces.data && traces.data.items.length > 0 && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(280px, 1fr) minmax(480px, 1.4fr)",
            gap: 16,
          }}
        >
          <Section title={`查询（${traces.data.items.length}）`}>
            <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {traces.data.items.map((t) => (
                <li
                  key={t.trace_id}
                  tabIndex={0}
                  onClick={() => setSelectedTraceId(t.trace_id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelectedTraceId(t.trace_id);
                    }
                  }}
                  style={{
                    padding: 8,
                    border: "1px solid var(--ma-line)",
                    borderRadius: 4,
                    marginBottom: 8,
                    cursor: "pointer",
                    background:
                      selectedTraceId === t.trace_id
                        ? "rgba(49,93,146,0.10)"
                        : "var(--ma-surface)",
                  }}
                  data-testid={`trace-row-${t.trace_id}`}
                >
                  <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <StatusPill tone="info">{t.retrieval}</StatusPill>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {t.result_count} hits
                    </span>
                  </div>
                  <div style={{ marginTop: 4 }}>{t.original_query ?? <span className="muted">（原 query 未记录）</span>}</div>
                  <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                    {formatTimestamp(t.occurred_at)}
                  </div>
                </li>
              ))}
            </ul>
          </Section>
          <Section title="查询详情">
            {!selectedTraceId ? (
              <EmptyState title="选择左侧查询" />
            ) : (
              <TraceDetail traceId={selectedTraceId} />
            )}
          </Section>
        </div>
      )}
    </div>
  );
}

function TraceDetail({ traceId }: { traceId: string }) {
  const detail = useQueryTraceDetail(traceId);
  if (detail.error) return <ErrorState error={detail.error} />;
  if (!detail.data) return <EmptyState title="Loading…" />;
  const d = detail.data;
  return (
    <div data-testid="trace-detail">
      <div className="muted" style={{ fontSize: 12 }}>trace_id</div>
      <div className="mono">{d.trace_id}</div>
      <div style={{ marginTop: 8 }}>
        <span className="muted" style={{ fontSize: 12 }}>原 query：</span>
        <span className="mono" data-testid="trace-original">{d.original_query ?? "(none)"}</span>
      </div>
      <div style={{ marginTop: 6 }}>
        <span className="muted" style={{ fontSize: 12 }}>fingerprint：</span>
        <span className="mono">{d.fingerprint ?? "(none)"}</span>
      </div>
      <div style={{ marginTop: 6 }}>
        <span className="muted" style={{ fontSize: 12 }}>provider / prompt_version：</span>
        <span className="mono">{d.provider ?? "(none)"} / {d.prompt_version ?? "(none)"}</span>
      </div>
      <hr style={{ margin: "12px 0", border: 0, borderTop: "1px solid var(--ma-line)" }} />
      <div>
        <div className="muted" style={{ fontSize: 12 }}>改写</div>
        <div className="mono-block">
          {d.rewrite ?? "(none)"}
        </div>
      </div>
      <div style={{ marginTop: 8 }}>
        <div className="muted" style={{ fontSize: 12 }}>扩写</div>
        <ul>
          {(d.expansions ?? []).length === 0 ? <li className="muted">（无）</li> : d.expansions.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      </div>
      <div style={{ marginTop: 8 }}>
        <div className="muted" style={{ fontSize: 12 }}>接受的变体</div>
        <ul>
          {d.accepted_variants.length === 0 ? <li className="muted">（无）</li> : d.accepted_variants.map((v, i) => <li key={i}>{v}</li>)}
        </ul>
      </div>
      <div style={{ marginTop: 8 }}>
        <div className="muted" style={{ fontSize: 12 }}>拒绝的变体</div>
        <ul>
          {d.rejected_variants.length === 0 ? <li className="muted">（无）</li> : d.rejected_variants.map((v, i) => <li key={i}>{v}</li>)}
        </ul>
      </div>
      {d.warnings.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div className="muted" style={{ fontSize: 12 }}>警告</div>
          <ul>{d.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
        </div>
      )}
    </div>
  );
}