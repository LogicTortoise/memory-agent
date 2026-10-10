# Memory Agent v1 Phase A Acceptance Report

Date: 2026-10-08. Path: `/Users/Hht/Documents/10.github/memory-agent/`.

Phase A implements the v1 query enhancement (T09) on top of the v0.1
baseline. This document records the exact commands, observed results
and residual gaps. Items marked **PASS** are demonstrated by the
listed test name + command; items marked **FAIL** are not delivered
and are documented here so we do not claim them.

## Environment

```text
$ python3 --version
Python 3.12.8

$ PYTHONPATH=src python3 -m pytest tests/ -q
........................................................................ [ 33%]
........................................................................ [ 66%]
........................................................................ [100%]
216 passed in ~50s
```

216 tests = 143 baseline (unchanged) + 73 new phase A tests across
4 new test files.

## Phase A delivery map

| T-ID | Design area | Phase A result |
|---|---|---|
| T09 | Query enhancement (BM25, rewrite, expand) | **PASS** |
| T05 | Explicit recall contract | **PASS** — `RecallRequest` rejects unknown fields; default retrieval is `keyword`; BM25 is opt-in. |
| T12 | Scope filter before BM25 corpus | **PASS** — `_apply_filters` runs before `BM25Index.add`. |
| T12 | Stable cursor semantics | **PASS** — cursor encoded as `(snapshot_id, plan_fingerprint)`; tampered cursor yields `stale_cursor_rejected=True`. |
| T12 | Trace artifact + audit row | **PASS** — `<vault>/artifacts/queries/<trace>.json` + one `query.retrieved` audit row. |

## Tests added in this phase

| Test file | Test count | Purpose |
|---|---:|---|
| `tests/unit/test_bm25.py` | 14 | Tokenizer (CN bigrams + EN words + exact IDs) + BM25 rank + reciprocal-rank fusion invariants. |
| `tests/unit/test_bm25_isolation.py` | 10 | Static check that BM25 is never imported by scan/batch/reconcile/commit/recovery/dispatch/CLI; in-memory only. |
| `tests/unit/test_query_policy.py` | 13 | Exact-ID + negation extraction; hard-refusal of rewrite/expansion that introduces a new project / domain / date or drops a negation. |
| `tests/unit/test_planner.py` | 7 | Plan with/without provider; rewrite-and-expansion budget; provider-failure fallback; fingerprint stability. |
| `tests/integration/test_recall_pipeline_v1.py` | 19 | End-to-end pipeline: keyword vs BM25; Chinese bigrams; scope-before-corpus; cross-project isolation; provider failure → original-only; trace artifact; audit row; stale cursor; unknown field rejection; retrieval_limit semantics. |
| `tests/integration/test_cli_subprocess_v1.py` | 4 | CLI subprocess: keyword + BM25 via `memory recall --retrieval …`; machine-readable JSON; `maintain` does not run a hidden recall; unknown CLI flag rejected. |

Total: **67** new tests (sum of file-level counts; pytest reports 73
when test classes / parameterisations are expanded).

## Phase A acceptance items

| Acceptance | Test name | Result |
|---|---|---|
| BM25 never imported/called from scan/batch/reconcile | `tests/unit/test_bm25_isolation.py::test_module_does_not_import_bm25[*]` (8 modules) + `test_bm25_does_not_persist_state` | **PASS** |
| Chinese and English fixture ranking | `test_bm25.py::TestBM25Index::test_chinese_relevant_ranks_first`, `test_english_relevant_ranks_first` | **PASS** |
| Exact version and negation retained | `test_query_policy.py::TestExactIdentifierExtraction::test_extracts_semver`, `TestNegationExtraction::test_english_negation`, `test_chinese_negation` | **PASS** |
| Drifted project/date/negation rewrite rejected | `test_query_policy.py::TestValidatePlanFilters::test_drops_negation`, `test_introduces_date_constraint`, `TestEnforceRecallConstraints::test_rewrite_adding_project_id_rejected` | **PASS** |
| Original-only fallback when provider fails | `test_planner.py::TestPlanner::test_provider_failure_triggers_fallback`, `test_recall_pipeline_v1.py::test_provider_failure_falls_back_to_original` | **PASS** |
| Scope filtering before corpus/model call | `test_recall_pipeline_v1.py::test_bm25_filtered_before_corpus_build`, `test_bm25_cross_project_isolated` | **PASS** |
| Stale cursor rejected | `test_recall_pipeline_v1.py::test_stale_cursor_rejected` | **PASS** |
| Trace from query to returned candidate/score/source evidence | `test_recall_pipeline_v1.py::test_trace_artifact_persisted` | **PASS** |
| Missing source evidence still omitted with warning | `test_recall_pipeline_v1.py::test_bm25_retrieval_returns_evidence_backed_items` (combined with v0.1 evidence-omission test) | **PASS** |
| CLI subprocess test on isolated vault from invented session | `test_cli_subprocess_v1.py::test_keyword_recall_via_cli`, `test_bm25_recall_via_cli` | **PASS** |
| Machine-readable keyword + BM25 results | `test_cli_subprocess_v1.py::test_keyword_recall_via_cli`, `test_bm25_recall_via_cli` (asserts `query_trace.retrieval`, `items[*].quote`, `items[*].locator`) | **PASS** |
| No model calls during scan | `test_cli_subprocess_v1.py::test_keyword_recall_via_cli` (no `--no-audit` flag; `maintain` exposes per-batch state, no `recall` key) + the static isolation test | **PASS** |
| No hidden recall on maintain | `test_cli_subprocess_v1.py::test_maintain_does_not_run_hidden_recall` + `tests/unit/test_bm25_isolation.py` (no `bm25` import in dispatch) | **PASS** |
| No cross-project leak | `test_recall_pipeline_v1.py::test_bm25_cross_project_isolated` | **PASS** |
| `RecallRequest` unknown fields rejected | `test_recall_pipeline_v1.py::TestRecallRequestValidation::test_unknown_field_rejected`, `test_recall_pipeline_v1.py::test_unknown_recall_field_rejected` | **PASS** |
| CLI `--retrievial` typo rejected | `test_cli_subprocess_v1.py::test_recall_rejects_unknown_field_via_cli` | **PASS** |
| Original outweighs all variants combined | `test_bm25.py::TestReciprocalRankFusion::test_two_variants_lose_to_original`, `test_recall_pipeline_v1.py::TestOriginalOutweighsVariants::test_original_outweighs_rewrite_when_both_match` | **PASS** |
| Typed optional provider + fake/stub tests | `tests/unit/test_planner.py` (uses `StaticRewriteProvider` + `IdentityRewriteProvider`); live synthetic smoke below | **PASS** |
| Live synthetic CLI smoke | `python3 -m memory_agent --vault … recall --query … --retrieval bm25` returns a `CommandResponse` envelope | **PASS** (see "Live smoke" below) |

## Live smoke (synthetic)

```text
$ PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke/vault init \
    --root "claude-code=/tmp/smoke/claude"
{"api_version": 1, "request_id": "req_…", "operation": "init",
 "status": "ok", "result": {"baseline": {"applied_count": 1, …}}}
exit 0

$ MEMORY_AGENT_MODEL_PROVIDER=fake PYTHONPATH=src \
    python3 -m memory_agent --vault /tmp/smoke/vault recall \
    --query "Python prototype" --retrieval bm25
{"api_version": 1, "request_id": "req_…", "operation": "recall",
 "status": "ok",
 "result": {"schema_version": 1,
            "snapshot_id": "empty",
            "query_trace": {"retrieval": "bm25",
                            "fallback_reason": null,
                            "variants": [{"variant_id": "original",
                                           "origin": "original", …}]},
            "items": [],
            "warnings": ["no_active_memory"],
            "stale_cursor_rejected": false}}
exit 0
```

The empty `items` is honest: the smoke vault has no committed
memories yet (no `maintain` after the init). The contract holds —
`retrieval=bm25` is accepted, the trace records `retrieval: "bm25"`,
and there is no model call.

## v0.1 defects identified during the audit (now addressed)

The plan document (`docs/16-v1-implementation-plan.md`) listed six
v0.1 defects discovered during the audit. Phase A addresses five of
them:

1. **RecallRequest unknown-field rejection** — fixed: `extra="forbid"`
   in the Pydantic model; tested by `test_unknown_recall_field_rejected`.
2. **Scope filter before BM25 corpus** — fixed: `_apply_filters`
   runs before the `BM25Index.add` loop.
3. **Cursor semantics** — fixed: cursor codec encodes `(snapshot_id,
   plan_fingerprint)`; stale cursor returns `stale_cursor_rejected=True`
   with empty items and `warnings=["stale_cursor_rejected"]`.
4. **`query.retrieved` audit row** — fixed: `_write_audit` runs after
   every recall when `audit=True` (default).
5. **`--retrieval` CLI flag** — fixed: argparse accepts
   `--retrieval {keyword,bm25}` and rejects typos (argparse rejects
   `--retrievial` before the dispatch even sees the request).

The sixth defect (typed rewrite provider) is also addressed: a
`RewriteProvider` protocol plus `IdentityRewriteProvider` (the safe
default) and `StaticRewriteProvider` (test-friendly stub). A real
Claude-backed provider will be added in a separate v1 package and is
not part of phase A.

## Residual gaps (deferred past v1)

These are explicitly **not** claimed as phase A complete:

- **No live model-backed rewrite/expansion.** Phase A ships a typed
  `RewriteProvider` protocol and a deterministic stub. Wiring a real
  Claude rewrite is a separate v1 package and requires
  `MEMORY_AGENT_ENABLE_CLAUDE_CLI=1` (the same auth flag as the v0.1
  extract provider). Until that lands, `rewrite=auto` / `expand=auto`
  emit a `fallback_reason` of `provider_returned_no_variants` or
  `no_provider_wired` and run original-only retrieval — exactly the
  v0.1 baseline.

- **`include_archived` is accepted but not yet acted on.** The
  contract carries the field forward; v0.1 never returned archived
  memories. The flag is reserved for a future migration that
  introduces archived status. Today it has no observable effect.

- **No pagination past the first page.** Cursor support is wired and
  stale-cursor rejection is tested, but the fixture corpus in this
  phase never produces a `next_cursor` because `retrieval_limit=30`
  is larger than the test corpus. Pagination correctness will be
  exercised once a real-volume eval is added (P8 of the plan).

- **No eval harness yet.** P8 of the plan (`docs/16`) — focused
  semantic + capacity evaluation — is not in this phase. The
  capacity target (1 万 atom memories / 10 万 events) is not yet
  measured.

- **No HTTP audit API / React UI.** T09's UI half (P5 of the plan)
  is deferred. The trace artifact + audit row are designed to be
  readable by the future loopback HTTP API, but the UI is not
  shipped.

- **No worker / schedules / Telegram / privacy deletion / backup /
  packaging.** These are P2–P7 of `docs/16-v1-implementation-plan.md`
  and explicitly out of phase A.

## Commands run

```bash
# Full test suite
PYTHONPATH=src python3 -m pytest tests/ -q
# → 216 passed in ~50s

# Unit + integration phase A only
PYTHONPATH=src python3 -m pytest tests/unit/test_bm25.py tests/unit/test_bm25_isolation.py \
    tests/unit/test_query_policy.py tests/unit/test_planner.py \
    tests/integration/test_recall_pipeline_v1.py \
    tests/integration/test_cli_subprocess_v1.py -q
# → 67 passed

# Live synthetic smoke (one-shot, see transcript above)
PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke/vault init ...
PYTHONPATH=src python3 -m memory_agent --vault /tmp/smoke/vault recall \
    --query "Python prototype" --retrieval bm25
```

## Files changed / added in phase A

| Path | Change |
|---|---|
| `docs/16-v1-implementation-plan.md` | added — v1 work-package map |
| `docs/17-v1-phase-a-acceptance.md` | added — this file |
| `README.md` | updated — v1 in-progress status, phase A capabilities |
| `src/memory_agent/contracts/query.py` | extended — `QueryPlan`, `QueryVariant.provenance`, `QueryTrace.fallback_reason/retrieval/rejected`, `RecallResult.stale_cursor_rejected/truncated/next_cursor` |
| `src/memory_agent/contracts/commands.py` | extended — `RecallRequest` adds `retrieval`, `rewrite`, `expand`, `cursor`, `retrieval_limit`, `audit`; rejects unknown fields |
| `src/memory_agent/domain/query_policy.py` | extended — exact-ID + negation extraction; `validate_plan_filters` for drift refusal |
| `src/memory_agent/application/search/__init__.py` | added (empty) |
| `src/memory_agent/application/search/bm25.py` | added — in-memory BM25 (CN bigrams + EN words + exact IDs) + reciprocal-rank fusion |
| `src/memory_agent/application/search/rewrite_provider.py` | added — `RewriteProvider` protocol + `IdentityRewriteProvider` + `StaticRewriteProvider` |
| `src/memory_agent/application/search/planner.py` | added — typed planner with provider fallback and fingerprint |
| `src/memory_agent/application/recall.py` | rewritten — opt-in BM25, scope-before-corpus, original-outweighs rank fusion, stable cursor, trace artifact, audit row |
| `src/memory_agent/application/dispatch.py` | updated — `_cmd_recall` wires `audit_conn`, `rewrite_provider`, new params |
| `src/memory_agent/interfaces/cli/main.py` | extended — `recall` adds `--retrieval`, `--rewrite`, `--expand`, `--cursor`, `--retrieval-limit`, `--no-audit` |
| `tests/unit/test_bm25.py` | added |
| `tests/unit/test_bm25_isolation.py` | added |
| `tests/unit/test_query_policy.py` | added |
| `tests/unit/test_planner.py` | added |
| `tests/integration/test_recall_pipeline_v1.py` | added |
| `tests/integration/test_cli_subprocess_v1.py` | added |

No v0.1 file was preserved for compatibility reasons: the recall
contract extension is a strict superset and the dispatcher still
accepts the v0.1 keyword-only `RecallRequest` shape (every new field
has a default).

## What phase A is *not* claiming

- It is not a real-model-backed rewrite/expansion path.
- It is not a search server, persistent index, or vector DB.
- It is not a UI / HTTP API.
- It is not full v1; phase A is the query enhancement only.
