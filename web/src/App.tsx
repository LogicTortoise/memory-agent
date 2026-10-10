import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { OverviewPage } from "./pages/OverviewPage";
import { SourcesPage } from "./pages/SourcesPage";
import { BatchesPage } from "./pages/BatchesPage";
import { MemoriesPage } from "./pages/MemoriesPage";
import { QueriesPage } from "./pages/QueriesPage";
import { AuditPage } from "./pages/AuditPage";
import { ClientProvider, useClient } from "./hooks/useClient";
import { MemoryAgentClient } from "./api/client";

import "./styles/theme.css";

interface AppProps {
  client?: MemoryAgentClient;
}

/**
 * App wires the loopback HTTP client into a React context and mounts
 * the routed pages under <AppShell />. Tests pass a custom
 * MemoryAgentClient; production reads the runtime config injected by
 * index.html / main.tsx.
 */
export function App(props: AppProps) {
  const injected = props.client ?? defaultClient();
  return (
    <ClientProvider client={injected}>
      <BrowserRouter>
        <AppShell />
        <Routes>
          <Route path="/" element={<OverviewRoute />} />
          <Route path="/sources" element={<SourcesPage />} />
          <Route path="/batches" element={<BatchesPage />} />
          <Route path="/memories" element={<MemoriesPage />} />
          <Route path="/queries" element={<QueriesPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </ClientProvider>
  );
}

function OverviewRoute() {
  // Force AppShell children to be wrapped under the routes layer —
  // useClient is just to validate the context is present.
  useClient();
  return <OverviewPage />;
}

function defaultClient(): MemoryAgentClient {
  const cfg = (globalThis as { __MA_CONFIG__?: { origin?: string; sessionToken?: string | null } })
    .__MA_CONFIG__;
  const origin = cfg?.origin ?? `${window.location.protocol}//${window.location.host}`;
  const sessionToken = cfg?.sessionToken ?? null;
  return new MemoryAgentClient({ origin, sessionToken });
}