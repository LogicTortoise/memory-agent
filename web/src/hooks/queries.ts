// Hooks wrapping the typed HTTP client. Each hook returns polling
// state so the UI can show "loading", "error" or a stale banner
// without recomputing from scratch on every render.

import { useMemo } from "react";

import { MemoryAgentClient } from "../api/client";
import type {
  AuditEventDTO,
  BatchDTO,
  BatchDetailDTO,
  BatchEvidenceDTO,
  ConflictDTO,
  JobListItemDTO,
  MemoryHistoryDTO,
  MemoryListItemDTO,
  OverviewDTO,
  PagedResponse,
  QueryTraceDTO,
  QueryTraceDetailDTO,
  SessionTurnDTO,
  SourceDTO,
  SourceDetailDTO,
} from "../api/contracts";
import { usePolling } from "../utils/usePolling";

import { useClient } from "./useClient.tsx";

export function useOverview() {
  const client = useClient();
  const fetcher = useMemo(() => () => client.overview(), [client]);
  return usePolling<OverviewDTO>(fetcher);
}

export function useSources(args: {
  cursor?: string | null;
  page_size?: number;
  connector?: string | null;
}) {
  const client = useClient();
  const fetcher = useMemo(
    () => () => client.listSources(args),
    [client, args.cursor ?? null, args.page_size ?? 25, args.connector ?? null],
  );
  return usePolling<PagedResponse<SourceDTO>>(fetcher);
}

export function useSourceDetail(sourceFileId: string | null) {
  const client = useClient();
  const fetcher = useMemo(
    () => async () => {
      if (!sourceFileId) return null;
      return await client.sourceDetail(sourceFileId);
    },
    [client, sourceFileId],
  );
  return usePolling<SourceDetailDTO | null>(fetcher, { enabled: !!sourceFileId });
}

export function useSessionTurns(args: {
  session_id: string | null;
  branch_id?: string;
  cursor?: string | null;
  page_size?: number;
}) {
  const client = useClient();
  const fetcher = useMemo(
    () => async () => {
      if (!args.session_id) return null;
      return await client.listSessionTurns({
        session_id: args.session_id,
        branch_id: args.branch_id ?? "main",
        cursor: args.cursor ?? null,
        page_size: args.page_size ?? 25,
      });
    },
    [
      client,
      args.session_id,
      args.branch_id ?? "main",
      args.cursor ?? null,
      args.page_size ?? 25,
    ],
  );
  return usePolling<PagedResponse<SessionTurnDTO> | null>(fetcher, {
    enabled: !!args.session_id,
  });
}

export function useBatches(args: {
  cursor?: string | null;
  page_size?: number;
  status?: string | null;
}) {
  const client = useClient();
  const fetcher = useMemo(
    () => () => client.listBatches(args),
    [client, args.cursor ?? null, args.page_size ?? 25, args.status ?? null],
  );
  return usePolling<PagedResponse<BatchDTO>>(fetcher);
}

export function useBatchDetail(batchId: string | null) {
  const client = useClient();
  const fetcher = useMemo(
    () => async () => {
      if (!batchId) return null;
      return await client.batchDetail(batchId);
    },
    [client, batchId],
  );
  return usePolling<BatchDetailDTO | null>(fetcher, { enabled: !!batchId });
}

/**
 * useBatchEvidence is metadata-only by default — the actual GET fires
 * only when the operator clicks "show evidence". This is the explicit
 * user action docs/11 requires.
 */
export function useBatchEvidenceLazy(batchId: string | null) {
  const client = useClient();
  const fetcher = useMemo(
    () => async () => {
      if (!batchId) return null;
      return await client.batchEvidence(batchId);
    },
    [client, batchId],
  );
  return usePolling<BatchEvidenceDTO | null>(fetcher, { enabled: false });
}

export function useMemoryHistory(memoryId: string | null) {
  const client = useClient();
  const fetcher = useMemo(
    () => async () => {
      if (!memoryId) return null;
      return await client.memoryHistory(memoryId);
    },
    [client, memoryId],
  );
  return usePolling<MemoryHistoryDTO | null>(fetcher, { enabled: !!memoryId });
}

export function useConflicts(args: {
  cursor?: string | null;
  page_size?: number;
  include_decided?: boolean;
}) {
  const client = useClient();
  const fetcher = useMemo(
    () => () => client.listConflicts(args),
    [client, args.cursor ?? null, args.page_size ?? 25, !!args.include_decided],
  );
  return usePolling<PagedResponse<ConflictDTO>>(fetcher);
}

export function useMemories(args: {
  cursor?: string | null;
  page_size?: number;
  include_archived?: boolean;
  q?: string | null;
}) {
  const client = useClient();
  const fetcher = useMemo(
    () => () => client.listMemories(args),
    [
      client,
      args.cursor ?? null,
      args.page_size ?? 25,
      !!args.include_archived,
      args.q ?? null,
    ],
  );
  return usePolling<PagedResponse<MemoryListItemDTO>>(fetcher);
}

export function useJobs(args: {
  cursor?: string | null;
  page_size?: number;
  state?: string | null;
  q?: string | null;
}) {
  const client = useClient();
  const fetcher = useMemo(
    () => () => client.listJobs(args),
    [
      client,
      args.cursor ?? null,
      args.page_size ?? 25,
      args.state ?? null,
      args.q ?? null,
    ],
  );
  return usePolling<PagedResponse<JobListItemDTO>>(fetcher);
}

export function useAuditEvents(args: {
  cursor?: string | null;
  page_size?: number;
  action?: string | null;
  object_type?: string | null;
  object_id?: string | null;
  seq_min?: number | null;
  seq_max?: number | null;
  q?: string | null;
}) {
  const client = useClient();
  const fetcher = useMemo(
    () => () => client.listAuditEvents(args),
    [
      client,
      args.cursor ?? null,
      args.page_size ?? 25,
      args.action ?? null,
      args.object_type ?? null,
      args.object_id ?? null,
      args.seq_min ?? null,
      args.seq_max ?? null,
      args.q ?? null,
    ],
  );
  return usePolling<PagedResponse<AuditEventDTO>>(fetcher);
}

export function useQueryTraces(args: {
  cursor?: string | null;
  page_size?: number;
  q?: string | null;
}) {
  const client = useClient();
  const fetcher = useMemo(
    () => () => client.listQueryTraces(args),
    [client, args.cursor ?? null, args.page_size ?? 25, args.q ?? null],
  );
  return usePolling<PagedResponse<QueryTraceDTO>>(fetcher);
}

export function useQueryTraceDetail(traceId: string | null) {
  const client = useClient();
  const fetcher = useMemo(
    () => async () => {
      if (!traceId) return null;
      return await client.queryTraceDetail(traceId);
    },
    [client, traceId],
  );
  return usePolling<QueryTraceDetailDTO | null>(fetcher, { enabled: !!traceId });
}

// Re-export the client hook so callers can grab the client directly
// for ad-hoc commands (e.g. decision POST).
export { useClient } from "./useClient.tsx";
export type { MemoryAgentClient };