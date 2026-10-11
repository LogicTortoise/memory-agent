import { useState } from "react";

import {
  Banner,
  EmptyState,
  ErrorState,
  PageTitle,
  StatusPill,
  formatNumber,
} from "../components/primitives";
import { Card } from "../components/Card";
import { CardCarousel } from "../components/CardCarousel";
import { useDetail } from "../components/DetailContext";
import { useSessionTurns, useSources } from "../hooks/queries";
import type { SourceDTO, SessionTurnDTO } from "../api/contracts";

function toneForSource(s: {
  last_status: string | null;
  pending_partial_tail: boolean;
  last_error: string | null;
  file_present: boolean;
}): "ok" | "pending" | "error" | "muted" {
  if (!s.file_present) return "error";
  if (s.last_error) return "error";
  if (s.pending_partial_tail) return "pending";
  if (s.last_status === "ready" || s.last_status === "ok") return "ok";
  return "muted";
}

export function SourcesPage() {
  const [connector, setConnector] = useState<string>("");
  const sources = useSources({ connector: connector || null, page_size: 50 });
  const items = sources.data?.items ?? [];
  const detail = useDetail();

  return (
    <div>
      <PageTitle
        title="会话与来源"
        subtitle="扫描过的 source 文件、生成版本与游标"
        right={
          <select
            aria-label="Connector filter"
            value={connector}
            onChange={(e) => setConnector(e.target.value)}
          >
            <option value="">全部连接器</option>
            <option value="claude-code">claude-code</option>
            <option value="codex">codex</option>
          </select>
        }
      />
      {sources.error && <ErrorState error={sources.error} />}
      {!sources.data && !sources.error && (
        <EmptyState title="Loading…" hint="查询 sources" />
      )}
      {sources.data && items.length === 0 && (
        <Banner tone="warn" title="没有 source 文件">
          当前 vault 没有 source_files 行；先在 CLI 执行 <code>init --root</code>。
        </Banner>
      )}
      {sources.data && items.length > 0 && (
        <CardCarousel
          title="来源"
          count={items.length}
          data-testid="sources-carousel"
        >
          {items.map((s) => (
            <Card
              key={s.source_file_id}
              variant="swipe"
              data-testid={`source-card-${s.source_file_id}`}
              aria-label={`${s.connector} · ${s.session_id}`}
              onClick={() =>
                detail.open({
                  title: `${s.connector} · ${s.session_id}`,
                  content: <SourceDetailPanel source={s} />,
                })
              }
            >
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <span className="mono" style={{ fontSize: 12 }}>{s.connector}</span>
                <StatusPill tone={toneForSource(s)}>{s.last_status ?? "—"}</StatusPill>
              </div>
              <div className="mono" style={{ fontSize: 13, marginTop: 6, wordBreak: "break-all" }}>
                {s.session_id}
              </div>
              <div className="muted tnum" style={{ fontSize: 11, marginTop: 6 }}>
                gen {s.generation} / rev {s.revision_seq}
              </div>
              <div className="muted tnum" style={{ fontSize: 11, marginTop: 2 }}>
                cursor {formatNumber(s.complete_offset)} / {formatNumber(s.size)}
              </div>
              {s.pending_partial_tail && (
                <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                  等待记录写完
                </div>
              )}
            </Card>
          ))}
        </CardCarousel>
      )}
    </div>
  );
}

// Tiny helper because the connector state lives only here.
// (no helper needed — useState directly in the component above)

function SourceDetailPanel({ source }: { source: SourceDTO }) {
  const turns = useSessionTurns({ session_id: source.session_id });
  return (
    <div>
      <div className="muted" style={{ fontSize: 12 }}>canonical_path</div>
      <div className="mono" style={{ wordBreak: "break-all", marginTop: 4 }}>
        {source.canonical_path}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
        <KV label="connector" value={source.connector} />
        <KV label="branch" value={source.branch_id} />
        <KV label="generation / revision" value={`${source.generation} / ${source.revision_seq}`} />
        <KV
          label="cursor / size"
          value={`${formatNumber(source.complete_offset)} / ${formatNumber(source.size)}`}
        />
        <KV label="last_status" value={source.last_status ?? "—"} />
        <KV
          label="file_present"
          value={source.file_present ? "yes" : "no"}
        />
      </div>
      {source.last_error && (
        <Banner tone="error" title="last_error" >
          <div className="mono">{source.last_error}</div>
        </Banner>
      )}
      <h3 style={{ fontSize: 14, margin: "16px 0 8px" }}>
        会话 {source.session_id} 的轮次
      </h3>
      {turns.error ? (
        <ErrorState error={turns.error} />
      ) : !turns.data ? (
        <EmptyState title="Loading…" />
      ) : turns.data.items.length === 0 ? (
        <div className="muted">（该会话无轮次）</div>
      ) : (
        <TurnList turns={turns.data.items} />
      )}
    </div>
  );
}

function TurnList({ turns }: { turns: SessionTurnDTO[] }) {
  return (
    <ol style={{ paddingLeft: 18, margin: 0 }}>
      {turns.map((t) => (
        <li key={t.turn_id} style={{ marginBottom: 10 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
            <strong>#{t.ordinal}</strong>
            <span className="muted">role={t.role ?? "?"}</span>
            <StatusPill tone={t.state === "closed" ? "ok" : "pending"}>
              {t.state}
            </StatusPill>
          </div>
          <div
            className="mono-block"
            style={{ marginTop: 4 }}
            data-testid={`turn-preview-${t.turn_id}`}
          >
            {t.quote_preview ?? <span className="muted">（无原话预览）</span>}
            {t.quote_redacted && (
              <span className="muted">…（已截断，原文请到批次证据页）</span>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

function KV({ label, value }: { label: string; value: string }) {
  return (
    <div className="card" style={{ padding: "10px 12px" }}>
      <div className="muted" style={{ fontSize: 11 }}>{label}</div>
      <div className="mono tnum" style={{ marginTop: 4, fontSize: 13 }}>
        {value}
      </div>
    </div>
  );
}