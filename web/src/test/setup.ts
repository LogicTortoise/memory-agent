import "@testing-library/jest-dom/vitest";

// React 18 expects an async act environment so `renderHook` (and the
// `act(async () => …)` blocks in our test files) can flush state
// updates synchronously instead of warning "not wrapped in act(...)".
// happy-dom doesn't set this on its own, so we flip it here once.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Force a deterministic origin / token so MemoryAgentClient defaults
// match what the production app injects.
(globalThis as { __MA_CONFIG__?: { origin?: string; sessionToken?: string | null } })
  .__MA_CONFIG__ = { origin: "http://127.0.0.1:8420", sessionToken: "test-token" };