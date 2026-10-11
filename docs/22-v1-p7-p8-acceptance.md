# P7/P8 acceptance: v1 audit UI (T15 + T16)

Date: 2026-10-09. Path: `/Users/Hht/Documents/10.github/memory-agent/`.
Active development root: this repository (no remote). Default branch: `main`.

This report covers the **P7 (T15 — React/TypeScript/Vite UI) + P8 (T16 —
real API + decision integration and browser tests)** work packages of the v1
plan (`docs/16-v1-implementation-plan.md`). v1 is **not** complete; this
report does **not** claim T17 (P9 — privacy deletion, backup/restore,
packaging) is done. T12, T13, T14 were owned by separate Claude tasks and
shipped before this report.

## Owned paths

| Path | Notes |
|---|---|
| `web/` (new) | React 18 + TypeScript + Vite source tree (`src/`), Vitest config, Playwright config, fixture seeder, e2e tests. |
| `web/src/api/contracts.ts` | Typed wire shapes mirroring `src/memory_agent/contracts/audit.py` + `contracts/commands.py`. |
| `web/src/api/client.ts` | Typed HTTP client (GETs + `postCommand`); maps server-side `INVALID_REQUEST`/stale-revision errors into typed exceptions. |
| `web/src/utils/usePolling.ts` | 2s active / 10s idle polling hook with shallow-equal re-render suppression, hidden-tab pause, manual `refresh()`. |
| `web/src/pages/{Overview,Sources,Batches,Memories,Queries,Audit}Page.tsx` | One page per audit section. |
| `web/src/components/AppShell.tsx` | App shell, navigation, theme switcher, projection-state pill. |
| `web/src/styles/theme.css` | Light/dark tokens matching `docs/11` palette as CSS custom properties. |
| `web/tests-e2e/fixture.py` + `_setup.ts` + `audit-ui.e2e.test.ts` | Fixture seeder + Playwright tests. |
| `src/memory_agent/interfaces/cli/main.py` | Added `memory ui` subcommand (P7 wiring into the existing HTTP app). |
| `src/memory_agent/interfaces/http/app.py` | **Minimal fix**: mount the static UI before the API routers, serve `/` + SPA fallback, leave the placeholder router only when no dist is present. |
| `src/memory_agent/infrastructure/sqlite/__init__.py` | **Minimal fix**: `open_state_db` now uses `check_same_thread=False` so the uvicorn worker pool can issue concurrent reads. |

## Exclusions

No edits to the owned-by-other-task backend files:

* `src/memory_agent/application/worker.py`
* `src/memory_agent/application/source_correction.py`
* `src/memory_agent/application/recall.py`
* `src/memory_agent/application/dispatch.py`
* Phase C/A tests under `tests/`

The HTTP audit code (`audit_routes.py`, `command_routes.py`, `local_auth.py`) is
read-only here. The two integration fixes in `app.py` + `sqlite/__init__.py`
are documented inline with "P7/P8 integration fix" comments.

## Acceptance — what v1 P7/P8 ships

| # | Behaviour | Evidence |
|---|---|---|
| 1 | `npm install` succeeds | `cd web && npm install` (129 packages, no errors). |
| 2 | `npm run build` succeeds | `vite build` produces `web/dist/{index.html,assets/index-*.{js,css}}`. |
| 3 | `npm run typecheck` succeeds | `tsc --noEmit` is clean. |
| 4 | `npm test` (Vitest) passes | **15 unit tests pass** (`src/api/client.test.ts` + `src/utils/usePolling.test.ts`). |
| 5 | Playwright e2e against real FastAPI + fixture vault | **16 e2e tests pass** (8 desktop + 8 narrow viewport). |
| 6 | Navigate source → batch evidence → memory → conflict | `tests-e2e/audit-ui.e2e.test.ts:50` |
| 7 | Evidence is metadata-only until the user clicks "展开原话/候选对照" | `BatchesPage.tsx` lazy evidence + `data-testid="expand-evidence"`; test asserts the span locator (`text=locator: bytes`) appears after the click. |
| 8 | One decide flow | `tests-e2e/audit-ui.e2e.test.ts:89`; verifies conflict is decided, list refreshes to 0 items. |
| 9 | Double click submits only one commit | `MemoriesPage.tsx` sets `pending` on first click and `disabled={pending}` on the buttons; idempotency key generated per click; same key replays return the original outcome. |
| 10 | Stale second action is blocked and the UI refreshes | `tests-e2e/audit-ui.e2e.test.ts:118`; bumps DB revision, intercepts `/conflicts` so the UI holds revision=1, click triggers 409, UI shows `data-testid="stale-banner"` and disables buttons. |
| 11 | Trace shows original query + rewrite provenance | `tests-e2e/audit-ui.e2e.test.ts:158`; navigates to `/queries`, selects the fixture's recall trace, asserts the detail view contains `trace_id`, the accepted variant list, and the rejected variants list. |
| 12 | No model calls on GET/refresh | `tests-e2e/audit-ui.e2e.test.ts:186`; listens on every `/api/v1/*` request and asserts no `/api/v1/recall` URL appears. |
| 13 | Narrow viewport + keyboard navigation | `tests-e2e/audit-ui.e2e.test.ts:222`; runs at 480×800, tabs into the nav, reaches "会话与来源", presses Enter, asserts the heading appears. |
| 14 | Dark theme tokens render | `tests-e2e/audit-ui.e2e.test.ts:252`; sets `data-theme="dark"`, asserts `--ma-canvas` / `--ma-ink` / `--ma-accent` are non-empty. |

## Backend integration gaps found and fixed

| Gap | File | Fix |
|---|---|---|
| Static dist mount was shadowed by the placeholder `/` route; `memory ui` would never serve the React app in production. | `src/memory_agent/interfaces/http/app.py` | Mount `/assets/<hash>.{js,css}` *before* the routers, register a `GET /` that returns `dist/index.html`, register a `GET /{full_path:path}` that returns the SPA shell. Only register the placeholder router when no static dist exists. The SPA catch-all fires AFTER the API routers so `/api/v1/*` always wins. |
| SQLite connection default `check_same_thread=True` crashed the first read on a uvicorn worker thread. | `src/memory_agent/infrastructure/sqlite/__init__.py` | `sqlite3.connect(path, isolation_level=None, check_same_thread=False)`. The `transaction` context manager already serialises writes via `BEGIN IMMEDIATE`; WAL handles concurrent reads. |

The fixture seeder's first run hit two more integration points which are now
documented in the React app rather than the backend:

* `conflict.decide` is not a registered operation in `dispatch.py`; the React
  app uses `decide` (the CLI canonical name).
* `DecideParams` (and `ConflictDecideParams`) forbid extra fields, so the
  `idempotency_key` is set on the **envelope**, not on `params` (matching
  `_cmd_decide` which reads it from the envelope anyway).
* The server returns a stale-revision mismatch as `INVALID_REQUEST` (HTTP
  422) rather than a distinct error code. The React app detects
  `proposal_revision … is stale` in the message body and surfaces the
  dedicated "stale" banner + disables the action buttons.
* The conflict list filter accepts `status IN ('open', 'pending')`; the
  reconciler actually emits `'pending'`. The React app accepts both as
  "open" for the purposes of enabling decision buttons.

## How to run

```bash
cd /Users/Hht/Documents/10.github/memory-agent
python3 -m pip install -e ".[dev]"
cd web
npm install
npm run build          # produces web/dist
npx playwright install --with-deps chromium   # one-time
npx playwright test                          # runs the e2e suite
npm test                                      # vitest unit tests
```

To launch the UI against a real vault:

```bash
cd /Users/Hht/Documents/10.github/memory-agent
python3 -m memory_agent --vault /tmp/my_vault ui \
  --session-token "dev-token" --ui-dist "$(pwd)/web/dist"
# → http://127.0.0.1:8440/   (default — clear of Octopus on 8420;
#   override with `--port`.)
```

## Not claimed

* **T17 (P9)** — privacy deletion barrier, consistent SQLite+files
  backup/restore with a doctor-driven restore check, independent
  packaging/runbook. Untouched here; that is the final v1 work package.
* **Source-correction queue end-to-end** — the worker that drains
  `source.corrected` audit rows into `decide_proposal` calls (NEW, surfaced
  by the phase B source-integrity fix in `docs/18`). The UI surface for
  correction proposals is NOT implemented; this P7/P8 work surfaces the
  `memory_projection.source_corrections` field as a metadata-only list and
  leaves any "decide correction" button for T17.
* **Worker / Telegram / live Claude CLI** — all owned by separate tasks.
* **Vector recall / semantic embeddings / persistent search services** —
  still deferred past v1.

The README status line is updated to **v1 phase A + phase B + phase C +
P7 (UI) shipped; P8 (UI integration) shipped; P9 (privacy/backup/packaging)
remaining** once this report is committed.