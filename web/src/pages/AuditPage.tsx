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
import { Card } from "../components/Card";
import { CardCarousel } from "../components/CardCarousel";
import { useAuditEvents, useClient } from "../hooks/queries";
import { useDetail } from "../components/DetailContext";
import { HttpError } from "../api/client";
import type { AuditEventDTO, CommandResponse } from "../api/contracts";

function toneForOutcome(outcome: string): "ok" | "pending" | "error" | "muted" {
  switch (outcome) {
    case "success":
      return "ok";
    case "failure":
      return "error";
    case "skipped":
      return "pending";
    default:
      return "muted";
  }
}

export function AuditPage() {
  const [actionFilter, setActionFilter] = useState("");
  const audit = useAuditEvents({ page_size: 50, action: actionFilter || null });
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobState, setJobState] = useState<CommandResponse | null>(null);
  const [jobError, setJobError] = useState<Error | null>(null);
  const client = useClient();
  const detail = useDetail();

  // Light polling on job state when the operator has asked.
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const response = await client.postCommand({
          operation: "job.status",
          mode: "sync",
          client_id: "audit-ui",
          params: { job_id: jobId },
        });
        if (cancelled) return;
        setJobState(response);
        setJobError(null);
        if (
          response.result?.job_state &&
          ["succeeded", "failed", "cancelled"].includes(String(response.result.job_state))
        ) {
          return;
        }
      } catch (err) {
        if (cancelled) return;
        setJobError(err instanceof Error ? err : new Error(String(err)));
      }
      setTimeout(tick, 2000);
    };
    void tick();
    return () => {
      cancelled = true;
    };
  }, [client, jobId]);

  return (
    <div>
      <PageTitle
        title="审计与作业"
        subtitle="审计事件流、按 action/object 过滤；查询异步作业状态"
        right={
          <select
            aria-label="Action filter"
            value={actionFilter}
            onChange={(e) => setActionFilter(e.target.value)}
          >
            <option value="">全部动作</option>
            <option value="source.discovered">source.discovered</option>
            <option value="batch.extracted">batch.extracted</option>
            <option value="memory.committed">memory.committed</option>
            <option value="conflict.opened">conflict.opened</option>
            <option value="conflict.decided">conflict.decided</option>
            <option value="query.retrieved">query.retrieved</option>
            <option value="recovery.finalized">recovery.finalized</option>
          </select>
        }
      />
      {audit.error && <ErrorState error={audit.error} />}
      {!audit.data && !audit.error && <EmptyState title="Loading…" />}
      {audit.data && audit.data.items.length === 0 && (
        <Banner tone="ok" title="没有审计事件" />
      )}
      {audit.data && audit.data.items.length > 0 && (
        <CardCarousel
          title="审计"
          count={audit.data.items.length}
          data-testid="audit-carousel"
        >
          {audit.data.items.map((ev) => (
            <Card
              key={ev.audit_id}
              variant="swipe"
              data-testid={`audit-row-${ev.audit_id}`}
              aria-label={ev.action}
              onClick={() =>
                detail.open({ title: ev.action, content: <AuditEventPanel ev={ev} /> })
              }
            >
              <div className="muted tnum" style={{ fontSize: 11 }}>#{ev.seq}</div>
              <div style={{ display: "flex", gap: 6, alignItems: "center", marginTop: 4 }}>
                <span className="mono" style={{ fontSize: 13 }}>{ev.action}</span>
              </div>
              <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                {formatTimestamp(ev.occurred_at)}
              </div>
              <div style={{ marginTop: 6 }}>
                <StatusPill tone={toneForOutcome(ev.outcome)}>{ev.outcome}</StatusPill>
              </div>
            </Card>
          ))}
        </CardCarousel>
      )}
      <Section title="作业查询" right={
        jobId ? <button onClick={() => setJobId(null)}>关闭</button> : null
      }>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="text"
            placeholder="job_id（来自 POST /commands 返回）"
            value={jobId ?? ""}
            onChange={(e) => setJobId(e.target.value || null)}
            aria-label="job_id"
          />
        </div>
        {jobError && (
          <Banner tone="error" title="job.status 失败">
            {jobError instanceof HttpError ? `${jobError.status}: ${jobError.message}` : jobError.message}
          </Banner>
        )}
        {jobState && (
          <div className="mono-block" style={{ marginTop: 8 }} data-testid="job-state">
            <div>api_version: {jobState.api_version}</div>
            <div>status: {jobState.status}</div>
            <div>job_id: {jobState.job_id ?? "—"}</div>
            <div>job_state: {String(jobState.result?.job_state ?? "—")}</div>
          </div>
        )}
      </Section>
    </div>
  );
}

function AuditEventPanel({ ev }: { ev: AuditEventDTO }) {
  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
        <StatusPill tone={toneForOutcome(ev.outcome)}>{ev.outcome}</StatusPill>
        <span className="muted tnum">#{ev.seq}</span>
        <span className="muted">{formatTimestamp(ev.occurred_at)}</span>
      </div>
      <div className="muted" style={{ fontSize: 12 }}>action</div>
      <div className="mono" style={{ marginTop: 4 }}>{ev.action}</div>
      <div className="muted" style={{ fontSize: 12, marginTop: 12 }}>object</div>
      <div className="mono" style={{ marginTop: 4 }}>
        {ev.object_type ? `${ev.object_type}:` : ""}
        {ev.object_id ?? "—"}
      </div>
      <div className="muted" style={{ fontSize: 12, marginTop: 12 }}>actor</div>
      <div className="mono" style={{ marginTop: 4 }}>{ev.actor}</div>
      <div className="muted" style={{ fontSize: 12, marginTop: 12 }}>audit_id</div>
      <div className="mono" style={{ marginTop: 4 }}>{ev.audit_id}</div>
      {ev.reason_code && (
        <>
          <div className="muted" style={{ fontSize: 12, marginTop: 12 }}>reason_code</div>
          <div className="mono" style={{ marginTop: 4 }}>{ev.reason_code}</div>
        </>
      )}
      {ev.redacted && (
        <Banner tone="warn" title="已脱敏" >
          该事件的 details / correlation / before_after 字段在审计层被部分或完全脱敏（依据 privacy 策略）。
        </Banner>
      )}
    </div>
  );
}