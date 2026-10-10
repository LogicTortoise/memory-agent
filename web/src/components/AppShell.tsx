// Theme toggle + global app shell.

import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useOverview } from "../hooks/queries";
import { describeProjection } from "../api/client";

const navItems: Array<{ to: string; label: string }> = [
  { to: "/", label: "处理概览" },
  { to: "/sources", label: "会话与来源" },
  { to: "/batches", label: "处理批次" },
  { to: "/memories", label: "记忆与决定" },
  { to: "/queries", label: "查询审计" },
  { to: "/audit", label: "审计与作业" },
];

type Theme = "system" | "light" | "dark";

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.dataset.theme = theme === "system" ? "" : theme;
}

export function AppShell() {
  const overview = useOverview();
  const header = overview.data?.header ?? null;
  const projection = header ? describeProjection(header) : null;
  const loc = useLocation();
  const [theme, setTheme] = useState<Theme>(() => {
    const stored = localStorage.getItem("ma-theme") as Theme | null;
    return stored ?? "system";
  });
  useEffect(() => {
    applyTheme(theme);
    localStorage.setItem("ma-theme", theme);
  }, [theme]);
  return (
    <div className="app-shell">
      <header className="app-header">
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <strong>Memory Agent · Audit</strong>
          <span className="muted" style={{ fontSize: 12 }}>
            v1 (P7/P8)
          </span>
          {header && (
            <span
              className="status-pill"
              data-tone={projection?.tone === "ok" ? "ok" : projection?.tone === "error" ? "error" : "pending"}
              data-testid="projection-state"
              title={`snapshot=${header.snapshot_id} lag=${header.db_lag_behind_head} ahead=${header.db_ahead_of_head}`}
            >
              {projection?.label}
            </span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span className="muted" style={{ fontSize: 12 }}>
            {header ? `as_of ${header.as_of.slice(0, 19)}Z` : "connecting…"}
          </span>
          <select
            aria-label="Theme"
            value={theme}
            onChange={(e) => setTheme(e.target.value as Theme)}
          >
            <option value="system">System</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </div>
      </header>
      <nav className="app-nav" aria-label="Audit sections">
        {navItems.map((item) => {
          const isActive =
            item.to === "/"
              ? loc.pathname === "/"
              : loc.pathname.startsWith(item.to);
          return (
            <NavLink
              key={item.to}
              to={item.to}
              className={isActive ? "active" : ""}
            >
              {item.label}
            </NavLink>
          );
        })}
      </nav>
      <main className="app-main" id="main">
        <Outlet />
      </main>
    </div>
  );
}