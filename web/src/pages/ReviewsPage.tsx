import { useState } from "react";

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
import { useReviewDetail, useReviews } from "../hooks/queries";
import type { ReviewDetailDTO, ReviewListItemDTO } from "../api/contracts";

const KIND_LABEL: Record<string, string> = {
  split_candidate: "建议拆分",
  merge_candidate: "建议合并",
  scope_overlap: "作用域重叠",
};

function toneForKind(kind: string): "ok" | "pending" | "error" | "info" | "muted" {
  switch (kind) {
    case "split_candidate":
      return "info";
    case "merge_candidate":
      return "pending";
    case "scope_overlap":
      return "error";
    default:
      return "muted";
  }
}

function toneForPeriod(period: string): "ok" | "info" | "muted" {
  if (period === "weekly") return "info";
  if (period === "monthly") return "ok";
  return "muted";
}

export function ReviewsPage() {
  const [period, setPeriod] = useState<"weekly" | "monthly" | "">("");
  const reviews = useReviews({ page_size: 50, period: period || null });
  const detail = useDetail();

  return (
    <div>
      <PageTitle
        title="分类复查"
        subtitle="周/月自动跑出的保守建议（拆分 / 合并 / 作用域重叠），不下结论只列候选"
        right={
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              aria-label="Period filter"
              value={period}
              onChange={(e) => {
                setPeriod(e.target.value as "" | "weekly" | "monthly");
              }}
              data-testid="reviews-period-filter"
            >
              <option value="">全部周期</option>
              <option value="weekly">周 (weekly)</option>
              <option value="monthly">月 (monthly)</option>
            </select>
          </div>
        }
      />
      {reviews.error && <ErrorState error={reviews.error} />}
      {!reviews.data && !reviews.error && <EmptyState title="Loading…" />}
      {reviews.data && reviews.data.items.length === 0 && (
        <Banner tone="ok" title="还没有复查产物">
          跑一次 <code>memory review --period week</code>（或 <code>--period month</code>），
          或等 worker 周日 03:30 / 每月 1 日 04:00 自动触发。
        </Banner>
      )}
      {reviews.data && reviews.data.items.length > 0 && (
        <CardCarousel
          title="复查产物"
          count={reviews.data.items.length}
          data-testid="reviews-carousel"
        >
          {reviews.data.items.map((r) => (
            <ReviewCard
              key={`${r.period}_${r.date}`}
              review={r}
              onOpen={() => {
                detail.open({
                  title: `${r.period === "weekly" ? "周" : "月"} · ${r.date}`,
                  content: (
                    <ReviewDetailPanel
                      period={r.period}
                      date={r.date}
                      summary={r}
                    />
                  ),
                });
              }}
            />
          ))}
        </CardCarousel>
      )}
    </div>
  );
}

function ReviewCard({
  review,
  onOpen,
}: {
  review: ReviewListItemDTO;
  onOpen: () => void;
}) {
  const kindKeys = Object.keys(review.metric_summary || {}).sort();
  return (
    <Card
      variant="swipe"
      data-testid={`review-row-${review.period}-${review.date}`}
      aria-label={`${review.period} ${review.date}`}
      onClick={onOpen}
    >
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <StatusPill tone={toneForPeriod(review.period)}>
          {review.period === "weekly" ? "周" : "月"}
        </StatusPill>
        <span className="mono" style={{ fontSize: 12 }}>{review.date}</span>
      </div>
      <div style={{ marginTop: 8, fontSize: 14 }}>
        {review.proposal_count === 0 ? (
          <span className="muted">无候选</span>
        ) : (
          <span>{review.proposal_count} 条候选</span>
        )}
      </div>
      {kindKeys.length > 0 && (
        <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: 4 }}>
          {kindKeys.map((k) => (
            <StatusPill key={k} tone={toneForKind(k)}>
              {KIND_LABEL[k] ?? k} · {review.metric_summary[k]}
            </StatusPill>
          ))}
        </div>
      )}
      <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
        {formatTimestamp(review.created_at)}
      </div>
    </Card>
  );
}

function ReviewDetailPanel({
  period,
  date,
  summary,
}: {
  period: "weekly" | "monthly";
  date: string;
  summary: ReviewListItemDTO | null;
}) {
  const detail = useReviewDetail(period, date);
  if (detail.error) {
    return (
      <Banner tone="error" title="读不到复查产物">
        {detail.error.message}
      </Banner>
    );
  }
  if (!detail.data) {
    return (
      <Banner tone="ok" title="还没产物">
        这个 (period, date) 还没有写盘。建议先 <code>memory review --period {period === "weekly" ? "week" : "month"}</code> 跑一次。
      </Banner>
    );
  }
  return <ReviewDetailBody dto={detail.data} summary={summary} />;
}

function ReviewDetailBody({
  dto,
  summary,
}: {
  dto: ReviewDetailDTO;
  summary: ReviewListItemDTO | null;
}) {
  return (
    <div>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
        <StatusPill tone={toneForPeriod(dto.period)}>
          {dto.period === "weekly" ? "周" : "月"}
        </StatusPill>
        <span className="mono">{dto.date}</span>
        <span className="muted" style={{ fontSize: 12 }}>
          · {dto.proposal_count} 条候选
        </span>
        <span className="muted" style={{ fontSize: 11 }}>
          {formatTimestamp(dto.created_at)}
        </span>
      </div>
      {summary && Object.keys(summary.metric_summary || {}).length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
          {Object.entries(summary.metric_summary)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([k, v]) => (
              <StatusPill key={k} tone={toneForKind(k)}>
                {KIND_LABEL[k] ?? k} · {v}
              </StatusPill>
            ))}
        </div>
      )}
      {dto.proposals.length === 0 ? (
        <Banner tone="ok" title="无可疑候选">
          这次复查没有命中拆分 / 合并 / 作用域重叠 的启发式。
        </Banner>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {dto.proposals.map((p, idx) => (
            <div
              key={idx}
              data-testid={`review-proposal-${dto.period}-${dto.date}-${idx}`}
              style={{ borderLeft: "3px solid var(--ma-line)", paddingLeft: 10 }}
            >
              <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <StatusPill tone={toneForKind(p.kind)}>
                  {KIND_LABEL[p.kind] ?? p.kind}
                </StatusPill>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                {p.reason}
              </div>
              {Object.keys(p.payload || {}).length > 0 && (
                <pre
                  className="mono-block"
                  style={{ marginTop: 6, fontSize: 11, maxHeight: 160, overflow: "auto" }}
                  data-testid={`review-payload-${dto.period}-${dto.date}-${idx}`}
                >
                  {JSON.stringify(p.payload, null, 2)}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}
      {dto.metrics && Object.keys(dto.metrics).length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div className="muted" style={{ fontSize: 12 }}>metrics</div>
          <pre
            className="mono-block"
            style={{ marginTop: 4, fontSize: 11, maxHeight: 160, overflow: "auto" }}
          >
            {JSON.stringify(dto.metrics, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
