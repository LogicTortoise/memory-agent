import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

import { act, renderHook } from "@testing-library/react";

import { usePolling } from "./usePolling";

// The polling hook drives state updates from `setTimeout`-scheduled
// ticks. To exercise the hook without leaking "not wrapped in act"
// warnings, we drive every tick through fake timers, advance them
// inside `act(async () => …)`, and flush the resulting microtasks in
// the same act block. `globalThis.IS_REACT_ACT_ENVIRONMENT = true` is
// configured in `src/test/setup.ts` so React recognises the
// environment as async-act capable.

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

/** Advance fake timers AND flush the resulting microtask queue inside `act`. */
async function advanceAndFlush(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function makeFlake<T>(values: T[], error?: Error) {
  let i = 0;
  return async () => {
    if (error && i === values.length) throw error;
    const v = values[i % values.length];
    i++;
    return v;
  };
}

describe("usePolling", () => {
  it("updates data when fetcher resolves to a new value", async () => {
    const fetcher = makeFlake([1, 2, 3]);
    const { result, unmount } = renderHook(() =>
      usePolling(fetcher, { activeIntervalMs: 10 }),
    );
    // Fire one polling tick: setTimeout(10) -> tick() -> setStatus("loading")
    // -> await fetcher() -> setData(next) -> setTimeout(10).
    await advanceAndFlush(10);
    expect(result.current.data).toBeGreaterThan(0);
    unmount();
  });

  it("captures errors and keeps polling", async () => {
    let calls = 0;
    const fetcher = async () => {
      calls++;
      // Always fail so the error state sticks (the real hook clears
      // it on the next success — that's intentional).
      throw new Error("boom-" + calls);
    };
    const { result, unmount } = renderHook(() =>
      usePolling(fetcher, { activeIntervalMs: 10, idleIntervalMs: 10 }),
    );
    // First tick fires the error; the hook then schedules the next
    // tick at idleIntervalMs (10) and we advance again to confirm
    // polling continues.
    await advanceAndFlush(10);
    expect(result.current.error).toBeInstanceOf(Error);
    const callsAfterFirst = calls;
    await advanceAndFlush(10);
    expect(calls).toBeGreaterThan(callsAfterFirst);
    expect(result.current.status).toBe("error");
    unmount();
  });

  it("keeps the fetcher polling when responses are shallow-equal", async () => {
    const fetcher = vi.fn(async () => ({ a: 1, b: 2 }));
    const { unmount } = renderHook(() =>
      usePolling(fetcher, { activeIntervalMs: 10 }),
    );
    // Two polling ticks (initial mount fires the first; advanceTimersByTime
    // fires the second at 10ms).
    await advanceAndFlush(10);
    await advanceAndFlush(10);
    expect(fetcher.mock.calls.length).toBeGreaterThanOrEqual(2);
    const calls = fetcher.mock.calls.length;
    await advanceAndFlush(30);
    expect(fetcher.mock.calls.length).toBeGreaterThan(calls);
    unmount();
  });

  it("refresh() forces an immediate re-poll", async () => {
    let calls = 0;
    const fetcher = async () => ++calls;
    const { result, unmount } = renderHook(() =>
      usePolling(fetcher, { activeIntervalMs: 500 }),
    );
    // Let the initial tick fire so the hook is "warmed up".
    await advanceAndFlush(10);
    const before = calls;
    // Manual refresh — fires the tick immediately, bypassing the
    // 500ms active interval. Wrap the refresh + microtask drain in
    // `act` so React's "wrapped in act" check passes.
    await act(async () => {
      result.current.refresh();
    });
    expect(calls).toBeGreaterThan(before);
    unmount();
  });
});