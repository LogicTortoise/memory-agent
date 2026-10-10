// Tiny React context that hands the typed MemoryAgentClient to every
// component. Initialised once by <App />; tests inject a custom client.

import { createContext, useContext, type ReactNode } from "react";

import type { MemoryAgentClient } from "../api/client";

const Ctx = createContext<MemoryAgentClient | null>(null);

export function ClientProvider({
  client,
  children,
}: {
  client: MemoryAgentClient;
  children: ReactNode;
}) {
  return <Ctx.Provider value={client}>{children}</Ctx.Provider>;
}

export function useClient(): MemoryAgentClient {
  const c = useContext(Ctx);
  if (!c) {
    throw new Error("MemoryAgentClient is not mounted");
  }
  return c;
}