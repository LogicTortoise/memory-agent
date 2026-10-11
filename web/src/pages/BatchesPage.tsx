import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

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
import { useDetail } from "../components/DetailContext";
import { useBatchDetail, useBatchEvidenceLazy, useBatches } from "../hooks/queries";
import type { BatchEvidenceInputDTO } from "../api/contracts";

function toneForBatchStatus(status: string): "ok" | "pending" | "error" | "info" | "muted" {
  switch (status) {
    case "succeeded":
    case "finalized":
      return "ok";
    case "failed":
    case "superseded":
      return "error";
    case "leased":
    case "running":
      return "info";
    case "ready":
    case "retry_wait":
      return "pending";
    default:
      return "muted";
  }
}

export function BatchesPage() {
  const [params] = useSearchParams();
  const sessionFilter = params.get("session");
  const statusFilter = params.get("status") || null;
  const [pageSize] = useState(50);
  const batches = useBatches({ page_size: pageSize, status: statusFilter });
  const items = batches.data?.items ?? [];
  const filteredItems = useMemo(() => {
    if (!sessionFilter) return items;
    return items.filter((b) => b.session_id === sessionFilter);
  }, [items, sessionFilter]);
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(null);
  const detail = useDetail();
  useEffect(() => {
    if (!selectedBatchId && filteredItems.length > 0) {
      setSelectedBatchId(filteredItems[0].batch_id);
    }
  }, [filteredItems, selectedBatchId]);

  return (
    <div>
      <PageTitle
        title="处理批次"
        subtitle={
          sessionFilter
            ? `过滤：session=${sessionFilter}`
            : "按时间倒序展示批次；原话/候选对照需手动展开"
        }
        right={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span className="muted">共 {filteredItems.length} 条</span>
          </div>
        }
      />
      {batches.error && <ErrorState error={batches.error} />}
      {!batches.data && !batches.error && (
        <EmptyState title="Loading…" hint="查询 batches" />
      )}
      {batches.data && items.length === 0 && (
        <Banner tone="warn" title="没有批次">
          当前过滤条件下没有 batch；尝试取消 status 过滤，或先运行 maintain。
        </Banner>
      )}
      {batches.data && items.length > 0 && (
        <CardCarousel
          title="批次列表"
          count={filteredItems.length}
          data-testid="batches-carousel"
        >
          {filteredItems.map((b) => (
            <Card
              key={b.batch_id}
              variant="swipe"
              data-testid={`batch-row-${b.batch_id}`}
              aria-label={b.batch_id}
              onClick={() => {
                setSelectedBatchId(b.batch_id);
                detail.open({
                  title: b.batch_id,
                  content: <BatchDetailPanel batchId={b.batch_id} />,
                });
              }}
            >
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <StatusPill tone={toneForBatchStatus(b.status)} data-testid={`batch-status-${b.batch_id}`}>
                  {b.status}
                </StatusPill>
                <span className="muted tnum" style={{ fontSize: 12 }}>{formatNumber(b.member_count)} members</span>
              </div>
              <div className="mono" style={{ marginTop: 6, fontSize: 12, wordBreak: "break-all" }}>
                {b.session_id}
              </div>
              <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                {b.branch_id} · attempt {b.attempt} · {formatTimestamp(b.created_at)}
              </div>
            </Card>
          ))}
        </CardCarousel>
      )}
    </div>
  );
}

function BatchDetailPanel({ batchId }: { batchId: string }) {
  const detail = useBatchDetail(batchId);
  const evidence = useBatchEvidenceLazy(batchId);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [highlightInput, setHighlightInput] = useState<string | null>(null);
  return (
    <div>
      {!detail.data ? (
        <EmptyState title="Loading…" />
      ) : (
        <div>
          {detail.data.commit_id && (
            <div className="muted" style={{ fontSize: 12, marginBottom: 8 }} data-testid="batch-commit-id">
              commit={detail.data.commit_id.slice(0, 12)}…
            </div>
          )}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
              gap: 8,
              marginBottom: 12,
            }}
          >
            <Tile label="policy" value={detail.data.policy_version} />
            <Tile label="input_hash" value={detail.data.input_hash.slice(0, 10) + "…"} mono />
            <Tile label="active" value={String(detail.data.members.filter((m) => m.active_target).length)} />
            <Tile label="context" value={String(detail.data.members.filter((m) => !m.active_target).length)} />
          </div>
          <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
            原话与候选对照不会自动加载；点击下方按钮按需展开。
          </div>
          <button
            type="button"
            onClick={() => {
              setEvidenceOpen(true);
              evidence.refresh();
            }}
            aria-expanded={evidenceOpen}
            data-testid="expand-evidence"
          >
            {evidenceOpen ? "重新展开原话/候选对照" : "展开原话/候选对照"}
          </button>
          {evidenceOpen && (
            <div style={{ marginTop: 12 }}>
              {evidence.error && <ErrorState error={evidence.error} />}
              {!evidence.data && !evidence.error && (
                <EmptyState title="Loading…" hint="读取 batch evidence" />
              )}
              {evidence.data && (
                <EvidenceView
                  inputs={evidence.data.inputs as BatchEvidenceInputDTO[]}
                  candidates={evidence.data.candidates}
                  highlightInput={highlightInput}
                  onSelectInput={setHighlightInput}
                />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Tile({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="card" style={{ padding: "10px 12px" }}>
      <div className="muted" style={{ fontSize: 11 }}>
        {label}
      </div>
      <div className={mono ? "mono" : ""} style={{ marginTop: 4, fontSize: 13, wordBreak: "break-all" }}>
        {value}
      </div>
    </div>
  );
}

interface EvidenceViewProps {
  inputs: BatchEvidenceInputDTO[];
  candidates: Array<Record<string, unknown>>;
  highlightInput: string | null;
  onSelectInput: (inputId: string | null) => void;
}

function EvidenceView({ inputs, candidates, highlightInput, onSelectInput }: EvidenceViewProps) {
  const sorted = useMemo(
    () => [...inputs].sort((a, b) => Number(b.active_target) - Number(a.active_target)),
    [inputs],
  );
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "1fr 1fr",
        gap: 12,
      }}
    >
      <div>
        <h3 style={{ fontSize: 14, margin: "0 0 6px" }}>原话（按 unit）</h3>
        <ol style={{ paddingLeft: 18, margin: 0 }}>
          {sorted.map((input) => (
            <li
              key={input.unit_id}
              style={{
                marginBottom: 10,
                background:
                  highlightInput === input.unit_id ? "rgba(36,103,80,0.06)" : undefined,
                padding: 6,
                borderRadius: 4,
                cursor: "pointer",
              }}
              data-testid={`evidence-input-${input.unit_id}`}
              onClick={() => onSelectInput(input.unit_id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelectInput(input.unit_id);
                }
              }}
              tabIndex={0}
              aria-label={`${input.purpose} ${input.unit_id}`}
            >
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <StatusPill tone={input.active_target ? "ok" : "muted"}>
                  {input.purpose}
                </StatusPill>
                <span className="muted" style={{ fontSize: 11 }}>
                  {input.role} · {formatTimestamp(input.source_time)}
                </span>
              </div>
              <div className="mono-block" style={{ marginTop: 4, fontSize: 12 }}>
                {input.text || <span className="muted">（无原文）</span>}
              </div>
              <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                locator: bytes {input.locator.byte_start}-{input.locator.byte_end}
              </div>
            </li>
          ))}
        </ol>
      </div>
      <div>
        <h3 style={{ fontSize: 14, margin: "0 0 6px" }}>候选（来自 candidate_set）</h3>
        {candidates.length === 0 ? (
          <div className="muted">（该批次无候选集）</div>
        ) : (
          <ul style={{ paddingLeft: 18, margin: 0 }}>
            {candidates.map((c, idx) => {
              const inputs = Array.isArray(c.inputs) ? c.inputs : [];
              const matched = highlightInput
                ? inputs.findIndex((i) => i && typeof i === "object" && (i as { unit_id?: string }).unit_id === highlightInput)
                : -1;
              return (
                <li
                  key={idx}
                  data-testid={`candidate-${idx}`}
                  style={{ marginBottom: 12 }}
                >
                  <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <strong>#{idx + 1}</strong>
                    {typeof c.statement === "string" && (
                      <StatusPill tone="info">{c.statement.slice(0, 32)}…</StatusPill>
                    )}
                  </div>
                  <div className="mono-block" style={{ marginTop: 4, fontSize: 12 }}>
                    {typeof c.statement === "string" ? c.statement : JSON.stringify(c)}
                  </div>
                  {inputs.length > 0 && (
                    <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                      inputs:{" "}
                      {inputs
                        .map((i: { unit_id?: string }, ix: number) => {
                          const isMatch = ix === matched;
                          return (
                            <span
                              key={ix}
                              onClick={() => onSelectInput(i.unit_id ?? null)}
                              className="mono"
                              style={{
                                marginRight: 6,
                                background: isMatch ? "rgba(36,103,80,0.18)" : undefined,
                                cursor: "pointer",
                              }}
                            >
                              {i.unit_id ?? "(missing)"}
                            </span>
                          );
                        })}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}