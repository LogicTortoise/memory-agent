import { describe, expect, it, vi } from "vitest";

import {
  CommandRejectedError,
  HttpError,
  MemoryAgentClient,
  StaleRevisionError,
  describeProjection,
} from "./client";
import type { OverviewDTO, EnvelopeHeader } from "./contracts";

function makeJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("MemoryAgentClient GET", () => {
  it("sends Origin + Accept on GET and parses JSON", async () => {
    const fetchImpl = vi.fn(async () =>
      makeJsonResponse({ ok: true, hello: "world" }),
    );
    const client = new MemoryAgentClient({
      origin: "http://127.0.0.1:8420",
      sessionToken: "t",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    const out = await client.overview();
    expect(out).toEqual({ ok: true, hello: "world" });
    const init = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect((init as RequestInit).method).toBe("GET");
    expect(
      new Headers((init as RequestInit).headers as HeadersInit).get("Accept"),
    ).toBe("application/json");
  });

  it("raises HttpError with body for non-2xx responses", async () => {
    const fetchImpl = vi.fn(async () =>
      makeJsonResponse({ detail: "boom" }, 500),
    );
    const client = new MemoryAgentClient({
      origin: "http://127.0.0.1:8420",
      sessionToken: "t",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await expect(client.overview()).rejects.toBeInstanceOf(HttpError);
  });
});

describe("MemoryAgentClient POST commands", () => {
  it("refuses POST without a configured session token", async () => {
    const fetchImpl = vi.fn();
    const client = new MemoryAgentClient({
      origin: "http://127.0.0.1:8420",
      sessionToken: null,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await expect(
      client.postCommand({
        operation: "conflict.decide",
        mode: "sync",
        client_id: "audit-ui",
        params: {},
      }),
    ).rejects.toBeInstanceOf(CommandRejectedError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("sends X-Memory-Agent-Token + Origin on POST", async () => {
    const fetchImpl = vi.fn(async () =>
      makeJsonResponse({
        api_version: 1,
        request_id: "r",
        operation: "conflict.decide",
        status: "ok",
        result: {},
      }),
    );
    const client = new MemoryAgentClient({
      origin: "http://127.0.0.1:8420",
      sessionToken: "secret",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await client.postCommand({
      operation: "conflict.decide",
      mode: "sync",
      client_id: "audit-ui",
      params: { conflict_id: "c1", proposal_revision: 1, action: "keep_old" },
    });
    const init = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls[0][1];
    const headers = new Headers((init as RequestInit).headers as HeadersInit);
    expect(headers.get("Origin")).toBe("http://127.0.0.1:8420");
    expect(headers.get("X-Memory-Agent-Token")).toBe("secret");
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body.api_version).toBe(1);
    expect(body.operation).toBe("conflict.decide");
    expect(body.client_id).toBe("audit-ui");
  });

  it("maps 409 IDEMPOTENCY_CONFLICT to CommandRejectedError", async () => {
    const fetchImpl = vi.fn(async () =>
      makeJsonResponse(
        {
          api_version: 1,
          request_id: "r",
          operation: "conflict.decide",
          status: "error",
          error: { code: "IDEMPOTENCY_CONFLICT", message: "dup" },
        },
        409,
      ),
    );
    const client = new MemoryAgentClient({
      origin: "http://127.0.0.1:8420",
      sessionToken: "secret",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    try {
      await client.postCommand({
        operation: "conflict.decide",
        mode: "sync",
        client_id: "audit-ui",
        params: {},
      });
      throw new Error("expected rejection");
    } catch (err) {
      expect(err).toBeInstanceOf(CommandRejectedError);
      expect((err as CommandRejectedError).code).toBe("IDEMPOTENCY_CONFLICT");
    }
  });

  it("maps other 409 responses to StaleRevisionError", async () => {
    const fetchImpl = vi.fn(async () =>
      makeJsonResponse(
        {
          api_version: 1,
          request_id: "r",
          operation: "conflict.decide",
          status: "error",
          error: { code: "INVALID_REQUEST", message: "stale" },
        },
        409,
      ),
    );
    const client = new MemoryAgentClient({
      origin: "http://127.0.0.1:8420",
      sessionToken: "secret",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    try {
      await client.postCommand({
        operation: "conflict.decide",
        mode: "sync",
        client_id: "audit-ui",
        params: {},
      });
      throw new Error("expected rejection");
    } catch (err) {
      expect(err).toBeInstanceOf(StaleRevisionError);
    }
  });
});

describe("describeProjection", () => {
  const header = (s: EnvelopeHeader["projection_state"]): EnvelopeHeader => ({
    schema_version: 1,
    as_of: "2026-10-09T00:00:00Z",
    snapshot_id: "empty",
    projection_state: s,
    db_lag_behind_head: 0,
    db_ahead_of_head: 0,
  });

  it("reports aligned", () => {
    expect(describeProjection(header("aligned"))).toEqual({
      tone: "ok",
      label: "Aligned with HEAD",
    });
  });
  it("reports db_lag", () => {
    const h = header("db_lag");
    h.db_lag_behind_head = 2;
    const out = describeProjection(h);
    expect(out.tone).toBe("warn");
    expect(out.label).toContain("2");
  });
  it("reports empty", () => {
    expect(describeProjection(header("empty")).tone).toBe("warn");
  });
  it("reports db_ahead as error", () => {
    expect(describeProjection(header("db_ahead")).tone).toBe("error");
  });
});

describe("overview wiring", () => {
  it("does NOT call the model when fetching /api/v1/overview", async () => {
    const fetchImpl = vi.fn(async () =>
      makeJsonResponse({
        header: {
          schema_version: 1,
          as_of: "now",
          snapshot_id: "empty",
          projection_state: "empty",
          db_lag_behind_head: 0,
          db_ahead_of_head: 0,
        },
        counts: {} as OverviewDTO["counts"],
        pipeline_stages: {},
        warnings: [],
      }),
    );
    const client = new MemoryAgentClient({
      origin: "http://127.0.0.1:8420",
      sessionToken: null,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await client.overview();
    const url = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(String(url)).toContain("/api/v1/overview");
  });
});