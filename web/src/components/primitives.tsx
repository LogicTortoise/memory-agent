// Shared layout primitives: status pills, banner, two-column page,
// empty / error states, focus-visible helpers.

import { type ReactNode } from "react";

export type Tone = "ok" | "pending" | "error" | "info" | "muted";

export function StatusPill({
  tone = "muted",
  children,
  "data-testid": testid,
}: {
  tone?: Tone;
  children: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <span className="status-pill" data-tone={tone} data-testid={testid}>
      {children}
    </span>
  );
}

export function Banner({
  tone = "info",
  title,
  children,
  "data-testid": testid,
}: {
  tone?: "info" | "warn" | "error" | "ok";
  title: string;
  children?: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <div
      className={`banner ${tone === "info" ? "" : tone}`}
      role="status"
      data-testid={testid}
    >
      <div style={{ fontWeight: 600 }}>{title}</div>
      {children && <div className="muted" style={{ marginTop: 4 }}>{children}</div>}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div
      className="card"
      role="region"
      aria-live="polite"
      style={{ textAlign: "center", padding: "32px 24px" }}
    >
      <div style={{ fontWeight: 600, marginBottom: 8 }}>{title}</div>
      {hint && <div className="muted">{hint}</div>}
    </div>
  );
}

export function ErrorState({ error }: { error: Error }) {
  return (
    <div className="banner error" role="alert">
      <div style={{ fontWeight: 600 }}>Request failed</div>
      <div className="muted" style={{ marginTop: 4 }}>
        {error.message}
      </div>
    </div>
  );
}

export function Section({
  title,
  right,
  children,
}: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section style={{ marginBottom: 24 }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 12,
        }}
      >
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>{title}</h2>
        {right}
      </header>
      {children}
    </section>
  );
}

export function PageTitle({
  title,
  subtitle,
  right,
}: {
  title: string;
  subtitle?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "baseline",
        justifyContent: "space-between",
        marginBottom: 16,
      }}
    >
      <div>
        <h1 style={{ fontSize: 24, fontWeight: 600, margin: 0 }}>{title}</h1>
        {subtitle && <div className="muted" style={{ marginTop: 4 }}>{subtitle}</div>}
      </div>
      {right}
    </div>
  );
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  try {
    const date = new Date(value);
    if (isNaN(date.getTime())) return value;
    return date.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
  } catch {
    return value;
  }
}

export function formatNumber(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toLocaleString();
}