// Wire types mirroring the backend Pydantic contracts in
// src/memory_agent/contracts/audit.py and contracts/commands.py.
//
// These are *server-authored* shapes. The UI MUST trust them as-is
// rather than computing counts / projection state client-side. The
// audit task reports counts via OverviewCounts and projection_state via
// EnvelopeHeader — keep them typed here so the UI cannot drift.

export interface EnvelopeHeader {
  schema_version: number;
  as_of: string;
  snapshot_id: string;
  projection_state: "aligned" | "db_lag" | "db_ahead" | "empty";
  db_lag_behind_head: number;
  db_ahead_of_head: number;
}

export interface PaginationCursor {
  cursor: string | null;
  next_cursor: string | null;
  page_size: number;
  total_estimated?: number | null;
}

export interface PagedResponse<T> {
  header: EnvelopeHeader;
  items: T[];
  pagination: PaginationCursor;
}

export interface OverviewCounts {
  sources_total: number;
  sources_unsupported: number;
  events_total: number;
  events_user: number;
  turns_total: number;
  batches_total: number;
  batches_pending: number;
  batches_running: number;
  batches_succeeded: number;
  batches_failed: number;
  candidate_sets_total: number;
  memories_active: number;
  memories_superseded: number;
  conflicts_open: number;
  audit_events_total: number;
  worker_lease_active: boolean;
  last_scan_at: string | null;
  last_commit_at: string | null;
}

export interface OverviewDTO {
  header: EnvelopeHeader;
  counts: OverviewCounts;
  pipeline_stages: Record<string, number>;
  warnings: string[];
}

export interface SourceDTO {
  source_file_id: string;
  connector: string;
  session_id: string;
  branch_id: string;
  canonical_path: string;
  generation: number;
  revision_seq: number;
  complete_offset: number;
  size: number;
  last_status: string | null;
  last_scan_at: string | null;
  last_error: string | null;
  file_present: boolean;
  pending_partial_tail: boolean;
}

export interface SourceDetailDTO extends SourceDTO {
  adapter_version: string | null;
  file_identity: string | null;
  consumed_prefix_sha256: string | null;
  manifest_reconcile: "ok" | "behind" | "ahead" | "missing_manifest" | "unknown";
}

export interface SessionTurnDTO {
  turn_id: string;
  session_id: string;
  branch_id: string;
  ordinal: number;
  revision: number;
  state: string;
  first_event_id: string | null;
  last_event_id: string | null;
  role: string | null;
  quote_preview: string | null;
  quote_redacted: boolean;
}

export interface BatchMemberDTO {
  unit_id: string;
  purpose: string;
  ord: number;
  epoch: number;
  active_target: boolean;
}

export interface BatchDTO {
  batch_id: string;
  session_id: string;
  branch_id: string;
  policy_version: string;
  status: string;
  attempt: number;
  input_hash: string;
  created_at: string | null;
  lease_until: string | null;
  member_count: number;
  candidate_set_id: string | null;
  candidate_set_ref: string | null;
}

export interface BatchDetailDTO extends BatchDTO {
  members: BatchMemberDTO[];
  run_ids: string[];
  commit_id: string | null;
}

export interface BatchEvidenceInputDTO {
  unit_id: string;
  purpose: string;
  active_target: boolean;
  epoch: number;
  event_id: string;
  role: string;
  source_time: string;
  locator: { byte_start: number; byte_end: number };
  text: string;
}

export interface BatchEvidenceDTO {
  batch_id: string;
  header: EnvelopeHeader;
  inputs: BatchEvidenceInputDTO[];
  candidates: Array<Record<string, unknown>>;
  runs: Array<Record<string, unknown>>;
  model_calls: Array<Record<string, unknown>>;
}

export interface MemoryRevisionDTO {
  memory_id: string;
  revision: number;
  status: string;
  commit_id: string | null;
  recorded_at: string | null;
  statement: string;
  scope_level: string | null;
  project_ids: string[];
  domains: string[];
  evidence_quotes: string[];
  superseded_by_revision: number | null;
}

export interface MemoryListItemDTO {
  memory_id: string;
  revision: number;
  status: string;
  commit_id: string | null;
  recorded_at: string | null;
  statement: string;
  scope_level: string | null;
}

export type ListMemoriesArgs = {
  cursor?: string | null;
  page_size?: number;
  include_archived?: boolean;
  q?: string | null;
};

export interface JobListItemDTO {
  job_id: string;
  request_id: string;
  client_id: string;
  operation: string;
  state: string;
  revision: number;
  mode: string;
  cancel_requested: boolean;
  created_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  last_heartbeat: string | null;
}

export type ListJobsArgs = {
  cursor?: string | null;
  page_size?: number;
  state?: string | null;
  q?: string | null;
};

export interface MemoryHistoryDTO {
  header: EnvelopeHeader;
  memory_id: string;
  revisions: MemoryRevisionDTO[];
  source_corrections: Array<Record<string, unknown>>;
  conflicts: Array<Record<string, unknown>>;
}

export interface ConflictDTO {
  conflict_id: string;
  revision: number;
  status: string;
  commit_id: string | null;
  opened_at: string | null;
  old_memory_ref: string | null;
  new_memory_ref: string | null;
  candidate_ref: string | null;
}

export interface AuditEventDTO {
  audit_id: string;
  seq: number;
  occurred_at: string;
  actor: string;
  action: string;
  object_type: string | null;
  object_id: string | null;
  correlation: Record<string, unknown>;
  before_after: Record<string, unknown>;
  outcome: string;
  reason_code: string | null;
  details: Record<string, unknown>;
  redacted: boolean;
}

export interface QueryTraceDTO {
  trace_id: string;
  snapshot_id: string;
  retrieval: string;
  original_query: string | null;
  accepted_variants: string[];
  rejected_variants: string[];
  fallback_reason: string | null;
  result_count: number;
  occurred_at: string | null;
}

export interface QueryTraceDetailDTO extends QueryTraceDTO {
  fingerprint: string | null;
  hard_filters: Record<string, unknown>;
  rewrite: string | null;
  expansions: string[];
  variants_full: Array<Record<string, unknown>>;
  rejected_full: Array<Record<string, unknown>>;
  provider: string | null;
  prompt_version: string | null;
  warnings: string[];
  artifact_ref: string | null;
}

// CommandRequest envelope (from contracts/commands.py).
export interface CommandRequest {
  api_version: 1;
  request_id: string;
  client_id: string;
  idempotency_key?: string | null;
  operation:
    | "init"
    | "scan"
    | "batch.plan"
    | "batch.process"
    | "maintain"
    | "recall"
    | "conflicts"
    | "decide"
    | "conflict.decide"
    | "source_correction.decide"
    | "doctor"
    | "review"
    | "capture"
    | "job.cancel"
    | "job.retry"
    | "job.status";
  mode: "sync" | "async";
  params: Record<string, unknown>;
  limits?: Record<string, unknown> | null;
  activation?: { kind: string; actor?: string | null } | null;
}

export interface CommandResponse {
  api_version: number;
  request_id: string;
  operation: string;
  status: "ok" | "queued" | "running" | "partial" | "error" | "cancelled";
  job_id?: string | null;
  job_state?: string | null;
  progress?: Record<string, number> | null;
  result?: Record<string, unknown> | null;
  error?: { code: string; message: string; retryable?: boolean } | null;
  warnings?: string[];
}

export type ListSourcesArgs = {
  cursor?: string | null;
  page_size?: number;
  connector?: string | null;
};
export type ListBatchesArgs = {
  cursor?: string | null;
  page_size?: number;
  status?: string | null;
};
export type ListSessionTurnsArgs = {
  cursor?: string | null;
  page_size?: number;
  session_id: string;
  branch_id?: string;
};
export type ListConflictsArgs = {
  cursor?: string | null;
  page_size?: number;
  include_decided?: boolean;
};
export type ListAuditArgs = {
  cursor?: string | null;
  page_size?: number;
  action?: string | null;
  object_type?: string | null;
  object_id?: string | null;
  seq_min?: number | null;
  seq_max?: number | null;
  q?: string | null;
};
export type ListQueryTracesArgs = {
  cursor?: string | null;
  page_size?: number;
  q?: string | null;
};