import { useEffect, useMemo, useState } from "react";

import {
  Banner,
  EmptyState,
  ErrorState,
  PageTitle,
  StatusPill,
  formatTimestamp,
} from "../components/primitives";
import { Card } from "../components/Card";
import { CardCarousel } from "../components/CardCarousel";
import { useDetail } from "../components/DetailContext";
import {
  useClient,
  useConflicts,
  useMemoryHistory,
} from "../hooks/queries";
import {
  CommandRejectedError,
  StaleRevisionError,
} from "../api/client";
import type { ConflictDTO } from "../api/contracts";

type DecisionAction = "adopt_new" | "keep_old" | "scope_split" | "defer";

function toneForConflictStatus(status: string): "pending" | "ok" | "muted" | "error" {
  switch (status) {
    case "open":
    case "pending":
      return "pending";
    case "decided":
      return "ok";
    case "superseded":
      return "error";
    default:
      return "muted";
  }
}

export function MemoriesPage() {
  const conflicts = useConflicts({ page_size: 50 });
  const detail = useDetail();
  const [selectedConflictId, setSelectedConflictId] = useState<string | null>(null);
  useEffect(() => {
    if (!selectedConflictId && conflicts.data && conflicts.data.items.length > 0) {
      setSelectedConflictId(conflicts.data.items[0].conflict_id);
    }
  }, [conflicts.data, selectedConflictId]);

  return (
    <div>
      <PageTitle
        title="记忆与决定"
        subtitle="活跃记忆、版本链、待决冲突"
        right={
          <span className="muted">共 {conflicts.data?.items.length ?? 0} 条</span>
        }
      />
      {conflicts.error && <ErrorState error={conflicts.error} />}
      {!conflicts.data && !conflicts.error && (
        <EmptyState title="Loading…" hint="查询冲突列表" />
      )}
      {conflicts.data && conflicts.data.items.length === 0 && (
        <Banner tone="ok" title="当前没有待决冲突">
          reconciler 不会自动采用新要求；所有冲突都需要用户决策。
        </Banner>
      )}
      {conflicts.data && conflicts.data.items.length > 0 && (
        <CardCarousel
          title="冲突"
          count={conflicts.data.items.length}
          data-testid="memories-carousel"
        >
          {conflicts.data.items.map((c) => (
            <Card
              key={c.conflict_id}
              variant="swipe"
              data-testid={`conflict-row-${c.conflict_id}`}
              aria-label={c.conflict_id}
              onClick={() => {
                setSelectedConflictId(c.conflict_id);
                detail.open({
                  title: c.conflict_id,
                  content: <ConflictDetail conflict={c} />,
                });
              }}
            >
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <StatusPill tone={toneForConflictStatus(c.status)}>{c.status}</StatusPill>
                <span className="muted tnum" style={{ fontSize: 12 }}>rev {c.revision}</span>
              </div>
              <div className="mono" style={{ marginTop: 6, fontSize: 12, wordBreak: "break-all" }}>
                {c.conflict_id}
              </div>
              <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                opened {formatTimestamp(c.opened_at)}
              </div>
            </Card>
          ))}
        </CardCarousel>
      )}
    </div>
  );
}

function ConflictDetail({ conflict }: { conflict: ConflictDTO }) {
  const old = useMemoryHistory(conflict.old_memory_ref);
  const new_ = useMemoryHistory(conflict.new_memory_ref);
  const client = useClient();
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "ok" | "error"; message: string } | null>(null);
  const [staleSince, setStaleSince] = useState<{ proposal_revision: number; conflict_id: string } | null>(null);

  const submit = async (action: DecisionAction) => {
    if (pending) return; // double-click guard
    setPending(true);
    setFeedback(null);
    try {
      await client.postCommand({
        operation: "decide",
        mode: "sync",
        client_id: "audit-ui",
        idempotency_key: `ui-${conflict.conflict_id}-${action}-${Date.now()}`,
        params: {
          conflict_id: conflict.conflict_id,
          proposal_revision: conflict.revision,
          action,
          expected_memory_revisions: [],
        },
        activation: { kind: "user_ui", actor: "audit-ui" },
      });
      setFeedback({ tone: "ok", message: `决定已应用：${action}` });
      setStaleSince(null);
    } catch (err) {
      const isStaleFromMessage =
        err instanceof CommandRejectedError &&
        /proposal_revision\s+\d+\s+is\s+stale/i.test(err.message);
      if (err instanceof StaleRevisionError || isStaleFromMessage) {
        setStaleSince({ proposal_revision: conflict.revision, conflict_id: conflict.conflict_id });
        setFeedback({
          tone: "error",
          message: "该提案的 revision 已经变化；已禁止重复提交。请刷新后重试。",
        });
      } else if (err instanceof CommandRejectedError) {
        setFeedback({ tone: "error", message: `${err.code}: ${err.message}` });
      } else {
        setFeedback({ tone: "error", message: err instanceof Error ? err.message : String(err) });
      }
    } finally {
      setPending(false);
    }
  };

  const isStale =
    !!staleSince && staleSince.conflict_id === conflict.conflict_id && staleSince.proposal_revision === conflict.revision;

  return (
    <div data-testid="conflict-detail">
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
        <StatusPill tone={toneForConflictStatus(conflict.status)}>{conflict.status}</StatusPill>
        <span className="muted">rev {conflict.revision}</span>
        {conflict.commit_id && (
          <span className="muted" data-testid="conflict-commit">
            commit={conflict.commit_id.slice(0, 12)}…
          </span>
        )}
      </div>
      {isStale && (
        <Banner tone="warn" title="提案已过期" data-testid="stale-banner">
          当前按钮引用的是过期的 proposal_revision。请刷新冲突列表后重新提交。
        </Banner>
      )}
      {feedback && (
        <Banner tone={feedback.tone === "ok" ? "ok" : "error"} title={feedback.tone === "ok" ? "成功" : "失败"}>
          {feedback.message}
        </Banner>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <RevisionColumn label="旧要求" history={old.data} testId="old-memory" />
        <RevisionColumn label="新候选" history={new_.data} testId="new-memory" />
      </div>
      <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
        <button
          type="button"
          onClick={() => submit("adopt_new")}
          disabled={pending || !isOpenConflict(conflict.status) || isStale}
          data-testid="decide-adopt-new"
        >
          采用新要求
        </button>
        <button
          type="button"
          onClick={() => submit("keep_old")}
          disabled={pending || !isOpenConflict(conflict.status) || isStale}
          data-testid="decide-keep-old"
        >
          保留旧要求
        </button>
        <button
          type="button"
          onClick={() => submit("scope_split")}
          disabled={pending || !isOpenConflict(conflict.status) || isStale}
          data-testid="decide-scope-split"
        >
          按范围并存
        </button>
        <button
          type="button"
          onClick={() => submit("defer")}
          disabled={pending}
          data-testid="decide-defer"
        >
          稍后
        </button>
      </div>
    </div>
  );
}

function isOpenConflict(status: string): boolean {
  return status === "open" || status === "pending";
}

function RevisionColumn({
  label,
  history,
  testId,
}: {
  label: string;
  history: import("../api/contracts").MemoryHistoryDTO | null | undefined;
  testId: string;
}) {
  const latest = useMemo(
    () => (history ? history.revisions[history.revisions.length - 1] : null),
    [history],
  );
  return (
    <div className="card" data-testid={testId}>
      <div className="muted" style={{ fontSize: 12 }}>
        {label}
      </div>
      {!history && <div className="muted" style={{ marginTop: 6 }}>Loading…</div>}
      {history && (
        <div style={{ marginTop: 8 }}>
          {!latest ? (
            <div className="muted">（无修订记录）</div>
          ) : (
            <>
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <StatusPill tone={latest.status === "active" ? "ok" : "muted"}>{latest.status}</StatusPill>
                <span className="muted" style={{ fontSize: 12 }}>
                  rev {latest.revision}
                </span>
              </div>
              <div style={{ marginTop: 8 }}>{latest.statement}</div>
              {latest.evidence_quotes.length > 0 && (
                <div style={{ marginTop: 8 }}>
                  <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>证据</div>
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {latest.evidence_quotes.map((q, i) => (
                      <li key={i} className="mono" style={{ fontSize: 12 }}>
                        {q.length > 240 ? q.slice(0, 240) + "…" : q}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}