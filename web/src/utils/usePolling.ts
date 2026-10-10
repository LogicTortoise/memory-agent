// Polling hook: re-runs an async fetcher at 2s when active, 10s when
// idle, and pauses while the tab is hidden. Preserves focus by only
// updating state when the fetcher succeeds and the resulting data is
// shallow-equal to what we already have. Refs hold the latest fetcher
// so the caller can swap it without retriggering the timer.

import { useEffect, useRef, useState } from "react";

export type PollingStatus = "idle" | "loading" | "success" | "error";

export interface PollingResult<T> {
  data: T | null;
  error: Error | null;
  status: PollingStatus;
  /** Manually trigger an out-of-band refresh. */
  refresh: () => void;
}

export interface PollingOptions {
  /** Polling interval in ms when the tab is active and no errors. */
  activeIntervalMs?: number;
  /** Polling interval in ms when there is nothing to do. */
  idleIntervalMs?: number;
  /** Stop polling (cleanup) when this becomes false. */
  enabled?: boolean;
}

/**
 * Shallow-equal helper for the data refs the polling hook tracks. We
 * intentionally avoid deep comparisons — the responses are JSON
 * objects produced server-side and a quick field-level compare keeps
 * focus management deterministic.
 */
function shallowEqual<T>(a: T | null, b: T | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  if (typeof a !== "object" || typeof b !== "object") return false;
  const aKeys = Object.keys(a as Record<string, unknown>);
  const bKeys = Object.keys(b as Record<string, unknown>);
  if (aKeys.length !== bKeys.length) return false;
  for (const k of aKeys) {
    if ((a as Record<string, unknown>)[k] !== (b as Record<string, unknown>)[k]) {
      return false;
    }
  }
  return true;
}

/**
 * usePolling — generic JSON polling hook.
 *
 *  * active interval: 2 s (matches docs/10 default).
 *  * idle interval: 10 s.
 *  * Pauses when ``document.visibilityState === "hidden"``.
 *  * Updates state only when the new data is *different* from the
 *    current data — this avoids rerenders that would steal focus from
 *    an input field the operator is typing into.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  options: PollingOptions = {},
): PollingResult<T> {
  const {
    activeIntervalMs = 2000,
    idleIntervalMs = 10_000,
    enabled = true,
  } = options;
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [status, setStatus] = useState<PollingStatus>("idle");
  const lastDataRef = useRef<T | null>(null);
  const refreshRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const isHidden = () =>
      typeof document !== "undefined" &&
      document.visibilityState === "hidden";

    const tick = async () => {
      if (cancelled) return;
      if (isHidden()) {
        timer = setTimeout(tick, idleIntervalMs);
        return;
      }
      setStatus("loading");
      try {
        const next = await fetcherRef.current();
        if (cancelled) return;
        if (!shallowEqual(lastDataRef.current, next)) {
          lastDataRef.current = next;
          setData(next);
        }
        setError(null);
        setStatus("success");
        if (enabled) {
          timer = setTimeout(tick, activeIntervalMs);
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err : new Error(String(err)));
        setStatus("error");
        if (enabled) {
          timer = setTimeout(tick, idleIntervalMs);
        }
      }
    };

    // refresh() must always work, even when the auto-polling effect is
    // currently disabled (the UI's "show on click" pattern relies on
    // it). We set up the ref unconditionally so a manual refresh
    // fires the fetcher exactly once.
    refreshRef.current = () => {
      if (timer) clearTimeout(timer);
      void tick();
    };

    if (!enabled) {
      return () => {
        cancelled = true;
        if (timer) clearTimeout(timer);
        if (typeof document !== "undefined") {
          document.removeEventListener("visibilitychange", () => undefined);
        }
      };
    }

    void tick();
    const onVisibility = () => {
      if (!isHidden() && refreshRef.current) refreshRef.current();
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibility);
    }
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibility);
      }
    };
  }, [enabled, activeIntervalMs, idleIntervalMs]);

  return {
    data,
    error,
    status,
    refresh: () => refreshRef.current(),
  };
}