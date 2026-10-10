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
import { useAuditEvents, useClient } from "../hooks/queries";
import { HttpError } from "../api/client";
import type { CommandResponse } from "../api/contracts";

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
        <Section title={`审计（${audit.data.items.length}）`}>
          <table role="grid" aria-label="audit events">
            <thead>
              <tr>
                <th>seq</th>
                <th>时间</th>
                <th>动作</th>
                <th>对象</th>
                <th>actor</th>
                <th>outcome</th>
              </tr>
            </thead>
            <tbody>
              {audit.data.items.map((ev) => (
                <tr key={ev.audit_id} data-testid={`audit-row-${ev.audit_id}`}>
                  <td className="mono">{ev.seq}</td>
                  <td>{formatTimestamp(ev.occurred_at)}</td>
                  <td className="mono">{ev.action}</td>
                  <td className="mono" style={{ wordBreak: "break-all" }}>
                    {ev.object_type ? `${ev.object_type}:` : ""}
                    {ev.object_id ?? "—"}
                  </td>
                  <td className="mono">{ev.actor}</td>
                  <td>
                    <StatusPill tone={toneForOutcome(ev.outcome)}>{ev.outcome}</StatusPill>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
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