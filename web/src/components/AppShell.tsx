// Theme toggle + global app shell.
//
// Layout: grid with a collapsible left tab strip and a top header.
// The tab strip is collapsed by default (56 px wide; only the Chinese
// monograms are visible) so the audit surface gets more room. Click
// the chevron to expand to 220 px and reveal the full Chinese labels.
// Collapsed state persists to localStorage["ma-sidebar"]; the inline
// <script> in web/index.html reads it before React mounts to avoid
// the FOUC where labels flash expanded for users who collapsed.
//
// Page headings still render into <main class="app-main"> via
// <Outlet /> — see docs/22-v1-p7-p8 acceptance: the heading must be
// a descendant of .app-main.

import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useOverview } from "../hooks/queries";
import { describeProjection } from "../api/client";
import { DetailProvider } from "./DetailContext";

const navItems: Array<{ to: string; label: string; monogram: string }> = [
  { to: "/", label: "处理概览", monogram: "概" },
  { to: "/sources", label: "会话与来源", monogram: "源" },
  { to: "/batches", label: "处理批次", monogram: "批" },
  { to: "/memories", label: "记忆与决定", monogram: "记" },
  { to: "/queries", label: "查询审计", monogram: "查" },
  { to: "/audit", label: "审计与作业", monogram: "审" },
];

type Theme = "system" | "light" | "dark";
type SidebarState = "expanded" | "collapsed";

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.dataset.theme = theme === "system" ? "" : theme;
}

function readInitialSidebar(): SidebarState {
  if (typeof window === "undefined") return "collapsed";
  const stored = window.localStorage.getItem("ma-sidebar");
  return stored === "expanded" ? "expanded" : "collapsed";
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
  const [sidebar, setSidebar] = useState<SidebarState>(readInitialSidebar);
  useEffect(() => {
    applyTheme(theme);
    localStorage.setItem("ma-theme", theme);
  }, [theme]);
  useEffect(() => {
    document.documentElement.dataset.sidebar = sidebar;
    localStorage.setItem("ma-sidebar", sidebar);
  }, [sidebar]);
  const toggleSidebar = () =>
    setSidebar((s) => (s === "expanded" ? "collapsed" : "expanded"));
  return (
    <DetailProvider>
    <div className={`app-shell${sidebar === "collapsed" ? " app-shell--collapsed" : ""}`}>
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
        <div className="nav-toggle">
          <button
            type="button"
            data-testid="toggle-sidebar"
            aria-expanded={sidebar === "expanded"}
            aria-label={sidebar === "expanded" ? "Collapse sidebar" : "Expand sidebar"}
            onClick={toggleSidebar}
          >
            <span aria-hidden="true">{sidebar === "expanded" ? "«" : "»"}</span>
            <span className="nav-toggle-label">
              {sidebar === "expanded" ? " 收起" : " 展开"}
            </span>
          </button>
        </div>
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
              data-testid={`nav-${item.to.replace("/", "") || "root"}`}
            >
              <span className="nav-monogram" aria-hidden="true">{item.monogram}</span>
              <span className="nav-label">{item.label}</span>
              <span className="sr-only">{item.label}</span>
            </NavLink>
          );
        })}
      </nav>
      <main className="app-main" id="main">
        <Outlet />
      </main>
    </div>
    </DetailProvider>
  );
}