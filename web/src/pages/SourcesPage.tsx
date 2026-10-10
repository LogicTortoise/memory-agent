import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  Banner,
  EmptyState,
  ErrorState,
  PageTitle,
  Section,
  StatusPill,
  formatNumber,
} from "../components/primitives";
import { useSessionTurns, useSources } from "../hooks/queries";

export function SourcesPage() {
  const [connector, setConnector] = useState("");
  const sources = useSources({ connector: connector || null, page_size: 50 });
  const items = sources.data?.items ?? [];
  const [selectedSourceFileId, setSelectedSourceFileId] = useState<string | null>(null);

  // The session id of the currently focused source — for the turns panel.
  const focusedSessionId = useMemo(() => {
    const focused = items.find((s) => s.source_file_id === selectedSourceFileId);
    return focused?.session_id ?? null;
  }, [items, selectedSourceFileId]);

  const turns = useSessionTurns({ session_id: focusedSessionId });

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
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(360px, 1fr) minmax(360px, 1fr)",
            gap: 16,
          }}
        >
          <Section title={`来源（${items.length}）`}>
            <table role="grid" aria-label="source files">
              <thead>
                <tr>
                  <th>路径 / 会话</th>
                  <th>连接器</th>
                  <th>世代</th>
                  <th>游标</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {items.map((s) => (
                  <tr
                    key={s.source_file_id}
                    tabIndex={0}
                    aria-selected={selectedSourceFileId === s.source_file_id}
                    onClick={() => setSelectedSourceFileId(s.source_file_id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelectedSourceFileId(s.source_file_id);
                      }
                    }}
                    style={
                      selectedSourceFileId === s.source_file_id
                        ? { background: "rgba(49,93,146,0.10)" }
                        : undefined
                    }
                  >
                    <td>
                      <div className="mono" style={{ wordBreak: "break-all" }}>{s.canonical_path}</div>
                      <div className="muted" style={{ fontSize: 12 }}>
                        session={s.session_id} branch={s.branch_id}
                      </div>
                    </td>
                    <td>{s.connector}</td>
                    <td>g{s.generation}/r{s.revision_seq}</td>
                    <td>
                      {formatNumber(s.complete_offset)} /{" "}
                      {formatNumber(s.size)}
                    </td>
                    <td>
                      <StatusPill tone={toneForSource(s)}>{s.last_status ?? "—"}</StatusPill>
                      {s.pending_partial_tail && (
                        <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                          等待记录写完
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
          <Section
            title={
              focusedSessionId
                ? `会话 ${focusedSessionId} 的轮次`
                : "未选中来源"
            }
            right={
              focusedSessionId ? (
                <Link to={`/batches?session=${focusedSessionId}`}>
                  <button>查看批次</button>
                </Link>
              ) : null
            }
          >
            {!focusedSessionId ? (
              <EmptyState title="选择左侧来源" hint="展示其 session 下的轮次与原话预览" />
            ) : turns.error ? (
              <ErrorState error={turns.error} />
            ) : !turns.data ? (
              <EmptyState title="Loading…" />
            ) : (
              <ol style={{ paddingLeft: 18 }}>
                {turns.data.items.map((t) => (
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
                      {t.quote_redacted && <span className="muted">…（已截断，原文请到批次证据页）</span>}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Section>
        </div>
      )}
    </div>
  );
}

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