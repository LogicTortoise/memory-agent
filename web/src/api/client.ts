// Typed HTTP client for the memory-agent audit API.
//
// Responsibilities:
//   * Wrap every endpoint as a typed Promise so the UI cannot
//     accidentally call a method that does not exist.
//   * Build a CommandRequest envelope (api_version + request_id) and
//     forward POST commands through /api/v1/commands with the session
//     token and Origin headers (per docs/10 + docs/11).
//   * Surface typed errors (HttpError, StaleRevisionError) so the UI
//     can show the right banner. NEVER swallow them.
//
// The client is intentionally metadata-first: it never calls a model
// just to refresh a list. POST commands are the only way a write can
// reach the server, and they go through the same dispatch path the CLI
// uses.

import type {
  AuditEventDTO,
  BatchDTO,
  BatchDetailDTO,
  BatchEvidenceDTO,
  CommandRequest,
  CommandResponse,
  ConflictDTO,
  EnvelopeHeader,
  JobListItemDTO,
  ListAuditArgs,
  ListBatchesArgs,
  ListConflictsArgs,
  ListJobsArgs,
  ListMemoriesArgs,
  ListQueryTracesArgs,
  ListSessionTurnsArgs,
  ListSourcesArgs,
  MemoryHistoryDTO,
  MemoryListItemDTO,
  OverviewDTO,
  PagedResponse,
  QueryTraceDTO,
  QueryTraceDetailDTO,
  SessionTurnDTO,
  SourceDTO,
  SourceDetailDTO,
} from "./contracts";

export interface ClientConfig {
  /** Origin used for write methods. Must be in the server allowlist. */
  origin: string;
  /** Shared secret required for POST /api/v1/commands. */
  sessionToken: string | null;
  /** Optional fetch override for tests. */
  fetchImpl?: typeof fetch;
}

export class HttpError extends Error {
  status: number;
  code?: string;
  body?: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

/**
 * StaleRevisionError is raised when the server returns 409 because the
 * proposal revision the UI cached is no longer current. The UI must
 * refresh the conflict before allowing another submit (docs/11).
 */
export class StaleRevisionError extends HttpError {
  constructor(message: string, body?: unknown) {
    super(409, message, body);
    this.code = "STALE_REVISION";
  }
}

export class CommandRejectedError extends HttpError {
  code: string;
  constructor(code: string, message: string, status: number) {
    super(status, message);
    this.code = code;
  }
}

let _reqCounter = 0;
function nextRequestId(prefix = "ui"): string {
  _reqCounter += 1;
  return `${prefix}_${Date.now().toString(36)}_${_reqCounter.toString(36)}`;
}

/**
 * MemoryAgentClient is the single network surface the UI uses.
 *
 * It is intentionally a thin wrapper — no retries, no caching, no
 * background refetch. Polling lives in the React side so the UI can
 * pause on hidden tabs.
 */
export class MemoryAgentClient {
  readonly origin: string;
  readonly sessionToken: string | null;
  private readonly fetchImpl: typeof fetch;

  constructor(config: ClientConfig) {
    this.origin = config.origin;
    this.sessionToken = config.sessionToken;
    this.fetchImpl = config.fetchImpl ?? fetch.bind(globalThis);
  }

  // ---------- read endpoints ------------------------------------------------

  overview(): Promise<OverviewDTO> {
    return this.getJson<OverviewDTO>("/api/v1/overview");
  }

  listSources(args: ListSourcesArgs = {}): Promise<PagedResponse<SourceDTO>> {
    const qs = new URLSearchParams();
    if (args.cursor) qs.set("cursor", args.cursor);
    if (args.page_size) qs.set("page_size", String(args.page_size));
    if (args.connector) qs.set("connector", args.connector);
    return this.getJson<PagedResponse<SourceDTO>>(`/api/v1/sources?${qs}`);
  }

  sourceDetail(sourceFileId: string): Promise<SourceDetailDTO> {
    return this.getJson<SourceDetailDTO>(
      `/api/v1/sources/${encodeURIComponent(sourceFileId)}`,
    );
  }

  listSessionTurns(args: ListSessionTurnsArgs): Promise<PagedResponse<SessionTurnDTO>> {
    const qs = new URLSearchParams();
    if (args.cursor) qs.set("cursor", args.cursor);
    if (args.page_size) qs.set("page_size", String(args.page_size));
    qs.set("branch_id", args.branch_id ?? "main");
    return this.getJson<PagedResponse<SessionTurnDTO>>(
      `/api/v1/sessions/${encodeURIComponent(args.session_id)}/turns?${qs}`,
    );
  }

  listBatches(args: ListBatchesArgs = {}): Promise<PagedResponse<BatchDTO>> {
    const qs = new URLSearchParams();
    if (args.cursor) qs.set("cursor", args.cursor);
    if (args.page_size) qs.set("page_size", String(args.page_size));
    if (args.status) qs.set("status", args.status);
    return this.getJson<PagedResponse<BatchDTO>>(`/api/v1/batches?${qs}`);
  }

  batchDetail(batchId: string): Promise<BatchDetailDTO> {
    return this.getJson<BatchDetailDTO>(
      `/api/v1/batches/${encodeURIComponent(batchId)}`,
    );
  }

  /**
   * batchEvidence surfaces raw text. The UI MUST only call this on
   * explicit user action — overview / lists stay metadata-only.
   */
  batchEvidence(batchId: string): Promise<BatchEvidenceDTO> {
    return this.getJson<BatchEvidenceDTO>(
      `/api/v1/batches/${encodeURIComponent(batchId)}/evidence`,
    );
  }

  memoryHistory(memoryId: string): Promise<MemoryHistoryDTO> {
    return this.getJson<MemoryHistoryDTO>(
      `/api/v1/memories/${encodeURIComponent(memoryId)}/history`,
    );
  }

  listConflicts(args: ListConflictsArgs = {}): Promise<PagedResponse<ConflictDTO>> {
    const qs = new URLSearchParams();
    if (args.cursor) qs.set("cursor", args.cursor);
    if (args.page_size) qs.set("page_size", String(args.page_size));
    if (args.include_decided) qs.set("include_decided", "true");
    return this.getJson<PagedResponse<ConflictDTO>>(`/api/v1/conflicts?${qs}`);
  }

  listMemories(args: ListMemoriesArgs = {}): Promise<PagedResponse<MemoryListItemDTO>> {
    const qs = new URLSearchParams();
    if (args.cursor) qs.set("cursor", args.cursor);
    if (args.page_size) qs.set("page_size", String(args.page_size));
    if (args.include_archived) qs.set("include_archived", "true");
    if (args.q) qs.set("q", args.q);
    return this.getJson<PagedResponse<MemoryListItemDTO>>(`/api/v1/memories?${qs}`);
  }

  listJobs(args: ListJobsArgs = {}): Promise<PagedResponse<JobListItemDTO>> {
    const qs = new URLSearchParams();
    if (args.cursor) qs.set("cursor", args.cursor);
    if (args.page_size) qs.set("page_size", String(args.page_size));
    if (args.state) qs.set("state", args.state);
    if (args.q) qs.set("q", args.q);
    return this.getJson<PagedResponse<JobListItemDTO>>(`/api/v1/jobs?${qs}`);
  }

  listAuditEvents(args: ListAuditArgs = {}): Promise<PagedResponse<AuditEventDTO>> {
    const qs = new URLSearchParams();
    if (args.cursor) qs.set("cursor", args.cursor);
    if (args.page_size) qs.set("page_size", String(args.page_size));
    if (args.action) qs.set("action", args.action);
    if (args.object_type) qs.set("object_type", args.object_type);
    if (args.object_id) qs.set("object_id", args.object_id);
    if (args.seq_min != null) qs.set("seq_min", String(args.seq_min));
    if (args.seq_max != null) qs.set("seq_max", String(args.seq_max));
    if (args.q) qs.set("q", args.q);
    return this.getJson<PagedResponse<AuditEventDTO>>(`/api/v1/audit?${qs}`);
  }

  auditEventDetail(auditId: string): Promise<AuditEventDTO> {
    return this.getJson<AuditEventDTO>(`/api/v1/audit/${encodeURIComponent(auditId)}`);
  }

  listQueryTraces(args: ListQueryTracesArgs = {}): Promise<PagedResponse<QueryTraceDTO>> {
    const qs = new URLSearchParams();
    if (args.cursor) qs.set("cursor", args.cursor);
    if (args.page_size) qs.set("page_size", String(args.page_size));
    if (args.q) qs.set("q", args.q);
    return this.getJson<PagedResponse<QueryTraceDTO>>(`/api/v1/queries?${qs}`);
  }

  queryTraceDetail(traceId: string): Promise<QueryTraceDetailDTO> {
    return this.getJson<QueryTraceDetailDTO>(
      `/api/v1/queries/${encodeURIComponent(traceId)}`,
    );
  }

  // ---------- write endpoint ------------------------------------------------

  /**
   * postCommand sends a versioned CommandRequest through
   * POST /api/v1/commands. The server forwards it through
   * handle_call, so the same idempotency / version rules as the CLI
   * apply. A 409 with IDEMPOTENCY_CONFLICT or a stale revision raises
   * the matching typed error.
   */
  async postCommand(req: Omit<CommandRequest, "api_version" | "request_id">): Promise<CommandResponse> {
    if (!this.sessionToken) {
      throw new CommandRejectedError(
        "MISSING_SESSION_TOKEN",
        "session token not configured; refusing POST /api/v1/commands",
        503,
      );
    }
    const envelope: CommandRequest = {
      api_version: 1,
      request_id: nextRequestId(req.operation),
      ...req,
    } as CommandRequest;
    const res = await this.fetchImpl(this.urlFor("/api/v1/commands"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: this.origin,
        "X-Memory-Agent-Token": this.sessionToken,
      },
      body: JSON.stringify(envelope),
    });
    const text = await res.text();
    let body: CommandResponse | { detail: string } | null = null;
    if (text) {
      try {
        body = JSON.parse(text) as CommandResponse | { detail: string };
      } catch {
        body = { detail: text };
      }
    }
    if (!res.ok) {
      const code = (body && "error" in body && body.error?.code) || "HTTP_" + res.status;
      const message =
        (body && "error" in body && body.error?.message) ||
        (body && "detail" in body && body.detail) ||
        `POST /api/v1/commands failed (${res.status})`;
      if (res.status === 409) {
        if (code === "IDEMPOTENCY_CONFLICT") {
          throw new CommandRejectedError(code, message, res.status);
        }
        throw new StaleRevisionError(message, body);
      }
      throw new CommandRejectedError(code, message, res.status);
    }
    return body as CommandResponse;
  }

  // ---------- helpers -------------------------------------------------------

  private urlFor(path: string): string {
    // The server is bound on 127.0.0.1:<port>. We accept any base URL
    // the operator configured; the test suite passes a relative path.
    if (path.startsWith("http://") || path.startsWith("https://")) {
      return path;
    }
    return `${this.origin.replace(/\/$/, "")}${path}`;
  }

  private async getJson<T>(path: string): Promise<T> {
    const res = await this.fetchImpl(this.urlFor(path), {
      method: "GET",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) {
      let detail: unknown = null;
      try {
        detail = await res.json();
      } catch {
        detail = null;
      }
      throw new HttpError(res.status, `GET ${path} -> ${res.status}`, detail);
    }
    return (await res.json()) as T;
  }
}

/**
 * Project the envelope header onto a minimal "view state" so the UI
 * can decide whether to show the "projection_state: db_lag" banner
 * without inspecting the raw object.
 */
export function describeProjection(header: EnvelopeHeader): {
  tone: "ok" | "warn" | "error";
  label: string;
} {
  if (header.projection_state === "aligned") {
    return { tone: "ok", label: "Aligned with HEAD" };
  }
  if (header.projection_state === "empty") {
    return { tone: "warn", label: "Vault empty" };
  }
  if (header.projection_state === "db_lag") {
    return {
      tone: "warn",
      label: `State aligning with HEAD (${header.db_lag_behind_head} pending)`,
    };
  }
  return { tone: "error", label: `State ahead of HEAD (${header.db_ahead_of_head})` };
}