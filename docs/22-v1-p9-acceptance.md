# P9 / T17 acceptance — privacy deletion, consistent backup/restore, doctor, packaging

Date: 2026-10-09. Owner: T17. Path:
`/Users/Hht/Documents/10.github/memory-agent/`. No remote; no commits /
pushes were issued in this revision. Everything was exercised
against **invented fixtures** with a **clean venv**, an in-process
doctor probe, and an explicit fake privacy HEAD. No real session
/ Telegram / deployment was touched.

Status: **P9 / T17 complete on the four named properties** — durable
privacy manifest + atomic privacy HEAD, query-time double-read of the
deletion version, source-level re-ingest guard, consistent SQLite +
files backup/restore via the standard-library backup API, extended
doctor, reproducible install / run / uninstall scripts (uninstall never
deletes the vault), and the final **packaging fix** that wires the
built React bundle into the Python wheel so a clean ``pip install``
ships the real SPA, not the T14 placeholder page. The v9 privacy HEAD
durability fix (init atomically establishes a v0 HEAD;
`read_live_privacy_ledger` refuses a missing HEAD so an un-initialised
`--vault` cannot be silently bootstrapped) is documented in
§7 below. The v10 **DB backstop** (ensure_privacy_head also refuses
when the state DB records any privacy_deletions rows;
read_live_privacy_ledger cross-checks every DB receipt against the
on-disk manifest so a forged same-version bundle cannot pass) is
documented in §8 below. P9 did **not** author UI code; the SPA comes
from the `web/dist/` artefact that the now-merged P7 (T15) + P8 (T16)
work produced and that is reported in
[`docs/22-v1-p7-p8-acceptance.md`](22-v1-p7-p8-acceptance.md).

---

## 1. What was built

| Path | Role |
|---|---|
| `src/memory_agent/application/privacy.py` | `PrivacyService.forget / list_receipts / is_target_blocked / current_version`. Immutable manifest under `<vault>/privacy/deletions/<op_id>.json`; atomic `<vault>/privacy/HEAD.json` swap; SQLite mirror; redaction of body / projection / query-trace artefacts. **`ensure_privacy_head`** is the operator-authorised safe initialiser: atomically writes a v0 HEAD when none exists and no deletion manifests are on disk; refuses when orphan manifests already exist. `_cmd_init` invokes it on every `memory init` so a freshly-initialised vault always has a durable privacy HEAD before any forget or restore. |
| `src/memory_agent/application/backup.py` | `BackupService.create / list_bundle / restore`. Uses ``sqlite3.Connection.backup`` API for a consistent SQLite snapshot; copies HEAD + privacy HEAD + immutable file content (segments / memories / commits / artifacts) with sha256; validates hashes + deletion manifest on restore; quarantines old bundles that lack the latest deletion manifest. **`read_live_privacy_ledger` refuses to synthesise a v0 ledger** when the live vault has no privacy HEAD, so an un-initialised `--vault` cannot be used as the trust root to silently mount an old v0 bundle. |
| `src/memory_agent/application/doctor.py` | Extended `run_doctor`: schema version, HEAD refs, source/cursor integrity, intent pending + recovery_required, deletion manifest / privacy HEAD consistency, recoverability probe. **No secrets; no full memory text.** |
| `src/memory_agent/application/scan.py` | Privacy re-ingest guard: source_blocks short-circuit the scanner with a ``source.privacy_blocked`` audit row. |
| `src/memory_agent/application/recall.py` | Privacy HEAD read at the *start* of every recall + a *second* read just before returning. Memory files whose ``memory_id`` is in the manifest are dropped even when the DB projection still lists them as active. |
| `src/memory_agent/application/dispatch.py` | New operations: ``privacy.forget``, ``privacy.list``, ``privacy.receipt``, ``privacy.unblock``, ``backup.create``, ``backup.list``, ``backup.restore``. Versioned contracts. |
| `src/memory_agent/contracts/privacy.py` | ``PrivacyForgetParams / PrivacyListParams / PrivacyReceiptParams / PrivacyUnblockParams``. ``extra="forbid"``. |
| `src/memory_agent/contracts/backup.py` | ``BackupCreateParams / BackupListParams / BackupRestoreParams``. ``extra="forbid"``. |
| `src/memory_agent/infrastructure/sqlite/migrations.py` | Migration 9: ``privacy_deletions`` table + ``privacy_blocks`` table + supporting indexes. Idempotent ALTER / CREATE. |
| `src/memory_agent/interfaces/cli/main.py` | New ``memory privacy forget|list|receipt|unblock`` and ``memory backup create|list|restore`` subcommands. Stable request_id per invocation so re-runs are not aliased to the same idempotency key. |
| `scripts/install.sh` | Reproducible install: creates a venv, installs the package in editable mode with ``[dev]``, writes a sample config. Idempotent (marker file). |
| `scripts/run.sh` | Activates the venv and forwards argv to ``python -m memory_agent``. Runs ``init`` if the vault is not initialised. |
| `scripts/uninstall.sh` | Uninstalls the editable install and removes the venv. **Never deletes the vault.** |
| `scripts/build_ui.sh` | Reproducible Vite build + mirror step. Runs ``npm run build`` under ``web/`` and copies the hashed artefacts into ``src/memory_agent/ui/dist`` so a wheel install ships the real React SPA. Idempotent. |
| `src/memory_agent/ui/dist/` | Package-data mirror of the Vite build (index.html + ``assets/index-*.{js,css}``). Wired into the wheel via ``[tool.setuptools.package-data]`` and into the sdist via ``MANIFEST.in``. |
| `src/memory_agent/interfaces/cli/main.py` | ``_resolve_default_ui_dist`` now falls back to ``importlib.resources.files("memory_agent.ui").joinpath("dist")`` when the developer checkout is not on disk; ``memory ui`` therefore serves the bundled SPA from a clean wheel install. |
| `src/memory_agent/interfaces/http/app.py` | ``/healthz`` is registered **before** the SPA ``{full_path:path}`` catch-all so it is not shadowed when the bundled UI is mounted. |
| `pyproject.toml` | Adds ``[tool.setuptools.package-data]`` for ``memory_agent.ui`` (dist/index.html + dist/assets/*). |
| `MANIFEST.in` | Ships the Vite artefacts inside the sdist so a clean ``pip install`` from an sdist tarball still mounts the UI. |
| `tests/integration/test_packaging_v1.py` | 3 packaging tests (3 new): packaged-dist invariant, ``importlib.resources`` resolution, **clean-wheel install + boot ``memory ui`` against an invented vault and assert ``GET /`` + hashed JS/CSS return 200 with the real React app, not the placeholder**. |
| `samples/config.toml` | Sample operator config with timezone, ``roots``, ``worker``, and ``telegram`` sections (all commented). |
| `design/launchd/com.memory-agent.worker.plist` | Pre-existing launchd template — **not loaded by this revision**, kept as the operator-facing template. |
| `tests/integration/test_privacy_v1.py` | 9 privacy tests (unchanged). |
| `tests/integration/test_backup_v1.py` | 11 backup / restore tests (unchanged). |
| `tests/integration/test_doctor_v1.py` | 7 doctor extension tests (unchanged). |
| `tests/integration/test_cli_v1_p9.py` | 7 CLI subprocess tests (privacy + backup, unchanged). |

The T11 launchd plist already exists; this revision does not modify
it. The T12 Telegram adapter is unchanged.

---

## 2. Acceptance matrix

| T17 acceptance row from docs/07 | Status | Evidence |
|---|---|---|
| explicit forget authorisation with durable independent manifest and atomic privacy HEAD | **PASS** | `test_forget_memory_writes_manifest_head_db_audit`: the manifest file is on disk, the HEAD.json is swapped atomically, the SQLite mirror is written, the audit row is appended, the body / projection / trace are redacted. The manifest contains **only metadata** — no statement, no evidence quote, no full-text payload. |
| query reads deletion version at start and just before return | **PASS** | `test_recall_filters_erased_memory_even_with_live_projection`: a memory whose DB projection still lists it as active is dropped because the privacy HEAD was read at the start of the recall. The post-return re-check (`_drop_erased_items` after items are built) closes the window between start-of-recall and return. |
| filter erased IDs even if DB projection / old backup is stale | **PASS** | `test_recall_filters_erased_memory_even_with_live_projection` (stale projection → privacy HEAD still drops it) + `test_backup_restore_after_forget_does_not_resurrect` (restore of a bundle taken *after* the forget keeps the projection redacted). |
| block re-ingest of deleted source where specified by design | **PASS** | `test_source_block_pins_cursor_and_blocks_reingest`: ``scan_once`` returns ``privacy_blocked > 0`` and ``new_events == 0`` for a source whose ``source_file_id`` is in the privacy manifest; ``privacy_blocks`` row is registered; the cursor is pinned at ``size``. |
| erase or redact related body, query artifacts/cache and audit payload as required while preserving minimal content-free deletion receipt | **PASS** | The memory file is shredded (truncated to zero bytes) then moved into ``<vault>/privacy/trash/<op_id>/``; the projection ``status`` becomes ``redacted``, ``statement`` is wiped, ``scope_json`` becomes ``{}``; query trace ``items`` carry a ``redacted: true, op_id: <op_id>`` tombstone for the erased memory; audit row ``details`` carries only counters, no full text. ``test_forget_redacts_query_trace_artefacts` proves the trace redaction. |
| fault tests at manifest/HEAD/DB windows; no reappearance after crash/restart | **PASS** | `test_crash_window_after_head_before_db_mirror` and `test_recovery_after_db_mirror_failure_via_doctor`: a torn write between manifest-write + HEAD-swap + DB-mirror is recognised by ``is_target_blocked`` (it walks the on-disk manifest) and surfaced by ``run_doctor`` (``privacy_db_behind_disk`` / ``privacy_manifest_drift``). |
| SQLite backup API under a publication/collection pause, recover unfinished intents first, then consistent HEAD/manifests/immutable file/segments/privacy snapshot with hashes | **PASS** | `BackupService.create` opens a fresh ``sqlite3.Connection`` to the bundle target and streams the live ``state.sqlite`` via ``src_conn.backup(dst_conn)`` (the standard library API; not ``cp``). Recovery runs *before* the snapshot. Each file in the bundle is content-hashed; the manifest records ``sqlite_sha256`` + ``deletion_manifest_sha256``. |
| restore only after validating hashes and merging latest independent deletion manifest | **PASS** | `_validate_bundle` recomputes the SQLite sha256 + the deletion manifest hash and refuses to mount if either is missing or mismatched. The dispatcher (`_cmd_backup_restore`) consults `read_live_privacy_ledger(vault)` which parses `<live-vault>/privacy/HEAD.json` and walks the actual `privacy/deletions/` files to compute the authoritative deletion-manifest hash; restore refuses when the bundle is older than the live ledger, when the bundle's deletion hash does not match the live ledger, or when the live ledger cannot be established (fail closed). `_post_restore_privacy_reconcile` walks the bundle's ``privacy_deletions`` table and re-redacts any ``memory_projection`` row that was forgotten in the bundle, so an old bundle's stale projection cannot resurrect erased content. |
| an old backup without latest deletion manifest must be quarantined rather than mounted | **PASS** | `test_backup_restore_quarantines_old_bundle`: when the live vault carries a *newer* privacy HEAD than the bundle, restore raises ``BackupError`` and does **not** materialise ``state/state.sqlite`. `test_backup_restore_quarantines_bundle_missing_receipts`: when the bundle claims the same version as the live vault but is missing a current deletion receipt (so version comparison alone would let it through), restore also quarantines. `test_backup_restore_cli_quarantines_old_bundle` exercises the same property via the real CLI subprocess (not just the service). The unsafe `--no-quarantine` and `--live-privacy-head-version` CLI flags have been removed entirely; argparse rejects them as unknown (`test_backup_restore_cli_rejects_no_quarantine_flag`, `test_backup_restore_cli_rejects_live_privacy_head_version_flag`). |
| direct restore into clean invented vault and run doctor/recall | **PASS** | `test_backup_restore_into_clean_vault_works`: a tar bundle is restored into a brand-new directory, then ``run_doctor`` reports ``ok=True`` and the projection rows are queryable end-to-end. The existing recall pipeline contract is unchanged because the privacy filter consults the manifest, not the DB projection status. |
| corrupt / missing file backup rejected | **PASS** | `test_backup_restore_detects_corrupt_sqlite` mutates the manifest's ``sqlite_sha256`` to zero and asserts the restore raises. ``test_backup_restore_rejects_unsafe_tar_member` proves the tar-slip guard rejects any member whose name contains ``..`` or starts with ``/``. |
| doctor checks schema, HEAD refs, source/cursor, intents, deletion manifest and recoverability without revealing secrets | **PASS** | `test_doctor_v1.py` covers every category: schema version, HEAD refs, source cursor integrity (missing file / cursor beyond size), pending intents → ``recovery_required``, privacy manifest consistency (``privacy_manifest_drift`` / ``privacy_db_ahead_of_head`` / ``privacy_db_behind_disk``), and recoverability probe. The doctor never returns full memory text or evidence quotes — only ``memory_id`` / ``op_id`` / counts / paths. |
| package Python and built web assets; reproducible install/run/uninstall (uninstall must not delete vault) | **PASS** | ``scripts/install.sh`` creates ``<repo>/.venv`` from a fresh ``python3 -m venv``, installs the package editable with ``[dev]``, and is idempotent (re-runs are no-ops via a marker file). ``scripts/run.sh`` activates the venv and forwards argv to ``python -m memory_agent``. ``scripts/uninstall.sh`` removes the editable install + venv and prints an explicit "vault was NOT deleted" line. The Vite build is wired into ``scripts/build_ui.sh`` (``npm run build`` + mirror to ``src/memory_agent/ui/dist``); the mirror ships inside both the wheel (via ``[tool.setuptools.package-data]``) and the sdist (via ``MANIFEST.in``). ``_resolve_default_ui_dist`` falls back to ``importlib.resources.files("memory_agent.ui").joinpath("dist")`` when the developer checkout is not on disk; ``memory ui`` therefore serves the real SPA from a clean wheel install with no editable / no ``web/`` checkout. ``tests/integration/test_packaging_v1.py::test_clean_wheel_install_serves_real_spa`` builds the wheel into an outside-repo temp directory, creates a fresh venv, ``pip install``s the wheel + declared runtime deps (no editable, no frontend source on disk), boots ``memory ui`` against an invented vault on a free loopback port, and asserts ``GET /`` + ``GET /assets/index-*.{js,css}`` return 200 with the real Vite output (``<div id="root"></div>``, hashed JS / CSS, no placeholder text). The bind contract is unchanged: 127.0.0.1 / localhost / ::1 only, ``/healthz`` registered before the SPA catch-all so it is not shadowed. |
| launchd template (already in ``design/launchd/``) not loaded | **PASS** | No ``launchctl load`` was performed; the template is unchanged from phase C. |
| sample config | **PASS** | ``samples/config.toml`` ships with the repo and is copied into the vault by ``scripts/install.sh`` on first run if the vault's own config is missing. |

---

## 3. Test counts

```
tests/integration/test_privacy_v1.py            9 passed
tests/integration/test_backup_v1.py            31 passed  (incl. CLI subprocess quarantine + receipt-mismatch + privacy-HEAD durability)
tests/integration/test_privacy_db_backstop_v1.py 12 passed  (P9 v10 DB backstop)
tests/integration/test_doctor_v1.py             7 passed
tests/integration/test_cli_v1_p9.py             7 passed
tests/integration/test_packaging_v1.py          3 passed  (P9 packaging fix)
                                                ---
                                                  69 P9 tests
```

Full test suite (excluding ``tests/v`` historical fixtures):

```
$ python3 -m pytest tests/ --ignore=tests/v -q
408 passed, 13 warnings in 76.92s
```

The 13 warnings are the pre-existing Starlette deprecation notice +
the Python 3.14 ``tar.extractall`` data-filter deprecation from the
backup tests; neither warning indicates a test failure. The
``tests/v`` directory contains historical research artefacts and is
explicitly excluded from the runbook.

### Browser / JS tests

This revision ships **no new JS / Playwright tests**; the T15 React
UI + T16 Playwright work is now merged in
[`docs/22-v1-p7-p8-acceptance.md`](22-v1-p7-p8-acceptance.md) (15 Vitest
+ 16 Playwright, all green). The P9 packaging fix exercises the bundled
UI from Python — ``tests/integration/test_packaging_v1.py::test_clean_wheel_install_serves_real_spa``
boots ``memory ui`` from a wheel installed into a clean outside-repo
venv against an invented vault and asserts the real SPA + hashed
JS / CSS are served with HTTP 200.

---

## 4. Commands and results

Focused run:

```
$ python3 -m pytest tests/integration/test_privacy_v1.py \
                   tests/integration/test_backup_v1.py \
                   tests/integration/test_doctor_v1.py \
                   tests/integration/test_cli_v1_p9.py -q
54 passed, 11 warnings in 12.16s
```

Clean-venv install + smoke:

```
$ python3 -m venv /tmp/mem_agent_smoke_venv
$ /tmp/mem_agent_smoke_venv/bin/pip install -e '.[dev]'
Successfully installed memory-agent-1.0.0
$ /tmp/mem_agent_smoke_venv/bin/python -m memory_agent --vault /tmp/mem_agent_smoke_vault init
{"status": "ok", "result": {"already_initialised": false, "schema_version": 1, ...}}
$ /tmp/mem_agent_smoke_venv/bin/python -m memory_agent --vault /tmp/mem_agent_smoke_vault doctor
{"status": "ok", "result": {"ok": true, "schema_version": 9, ...}}
$ /tmp/mem_agent_smoke_venv/bin/python -m memory_agent --vault /tmp/mem_agent_smoke_vault privacy forget \
    --kind memory --target mem_smoke --idempotency-key smoke01 --note "smoke-test"
{"status": "ok", "result": {"op_id": "del_0fea69217a604c72", "kind": "memory", "version": 1, ...}}
$ /tmp/mem_agent_smoke_venv/bin/python -m memory_agent --vault /tmp/mem_agent_smoke_vault backup create \
    --output /tmp/smoke_bundle.tar --label smoke
{"status": "ok", "result": {"bundle_id": "bk_bf5de8341ac5", "file_count": 5, "sqlite_sha256": "...", "privacy_head_version": 1, ...}}
$ /tmp/mem_agent_smoke_venv/bin/python -m memory_agent --vault /tmp/mem_agent_smoke_vault backup restore \
    --bundle /tmp/smoke_bundle.tar --target /tmp/smoke_restored
{"status": "ok", "result": {"target": "/tmp/smoke_restored", "bundle_id": "bk_bf5de8341ac5", ...}}
$ /tmp/mem_agent_smoke_venv/bin/python -m memory_agent --vault /tmp/smoke_restored doctor
{"status": "ok", "result": {"ok": true, "schema_version": 9, ...}}
$ /tmp/mem_agent_smoke_venv/bin/python -m memory_agent --vault /tmp/smoke_restored privacy list
{"status": "ok", "result": {"receipts": [{"op_id": "del_0fea69217a604c72", "kind": "memory", ...}], "count": 1}}
```

The restored vault is a clean invented directory (no pre-existing
``state.sqlite`` or ``privacy/HEAD.json``); the restore pass leaves
``run_doctor`` green and the privacy manifest present.

Clean-wheel UI smoke (P9 packaging fix, exercised by
``tests/integration/test_packaging_v1.py::test_clean_wheel_install_serves_real_spa``):

```
$ python3 -m build --wheel --outdir /tmp/mem_agent_wheel
Successfully built memory_agent-1.0.0-py3-none-any.whl
$ unzip -l /tmp/mem_agent_wheel/memory_agent-1.0.0-py3-none-any.whl | grep ui
      624  ...   memory_agent/ui/__init__.py
      836  ...   memory_agent/ui/dist/index.html
   204669  ...   memory_agent/ui/dist/assets/index-D0eKQFrH.js
     5380  ...   memory_agent/ui/dist/assets/index-DfyWkpWk.css
$ python3 -m venv /tmp/venv_clean
$ /tmp/venv_clean/bin/pip install --quiet /tmp/mem_agent_wheel/memory_agent-1.0.0-py3-none-any.whl \
    fastapi uvicorn pydantic PyYAML
$ ls $(/tmp/venv_clean/bin/python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")/memory_agent/ui/dist
assets  index.html
$ /tmp/venv_clean/bin/python -m memory_agent --vault /tmp/vault_clean init
{"status": "ok", "result": {"already_initialised": false, "schema_version": 1, ...}}
$ /tmp/venv_clean/bin/python -m memory_agent --vault /tmp/vault_clean ui \
    --port 18420 --host 127.0.0.1 --session-token smoke &
$ curl -sf http://127.0.0.1:18420/ | grep -q '<div id="root"></div>' && echo "real SPA served"
real SPA served
$ curl -sf -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18420/assets/index-D0eKQFrH.js
200
$ curl -sf -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18420/assets/index-DfyWkpWk.css
200
$ curl -sf http://127.0.0.1:18420/healthz | python3 -m json.tool
{ "ok": true, "service": "memory-agent-ui", "loopback_only": true,
  "write_methods_enabled": true, "session_token_configured": true }
```

The test runs in a temp directory outside the repo; the venv has no
``web/`` checkout, no editable install, no developer source. The only
UI source is the one shipped inside the wheel. The hashed filenames
match the contents of ``src/memory_agent/ui/dist/`` so a build that
fails to mirror is caught immediately by the package-side invariant
test (``test_packaged_dist_has_index_html``).

---

## 5. Blockers, deviations and known limits

1. **No real-network validation.** The CLI smoke was a clean-venv
   local install with no model provider active. Real Telegram
   behaviour / real Claude Code scanning / real OAuth are out of
   scope per the task brief ("no real session / Telegram /
   deployment").
2. **No UI / Playwright tests added in this revision.** The T15
   React UI + T16 Playwright integration tests are merged in
   `docs/22-v1-p7-p8-acceptance.md` (15 Vitest + 16 Playwright, all
   green). This P9 packaging fix exercises the bundled UI from
   Python: a fresh venv, a wheel install outside the repo, and HTTP
   smoke against an invented vault. P9 did not author UI code; the
   SPA shipped inside the wheel comes from the `web/dist/` build
   artefact that P7's `npm run build` produced and that P7 mounted
   into the loopback FastAPI (P7 also fixed the placeholder-shadow
   bug in `src/memory_agent/interfaces/http/app.py`). P8 added the
   Playwright integration tests against that mounted UI.
3. **``memory ui`` already binds 127.0.0.1 / localhost / ::1** (T14
   contract); this revision does not change that. The T14 caveat
   that "localhost alone is not described as securing writes"
   continues to apply; the new POST surface does not add any new
   authentication.
4. **In-place ``memory_projection`` redaction in restore is
   implemented for ``memory`` forgets only.** ``event`` /
   ``source_block`` receipts are honoured at the manifest and the
   blocks table, but their pre-restore projection rows are not
   re-scanned by ``_post_restore_privacy_reconcile`` because the
   schema does not store ``event_id`` or ``source_file_id`` on the
   projection. This matches the design: a memory forget is the only
   kind that touches ``memory_projection``.
5. **Tar-slip guard uses name-based checks** (``..`` segments,
   leading ``/``). Python 3.14's data-filter for ``extractall``
   raises a deprecation warning during the test run; the guard is
   kept explicit so the bundle is validated *before* ``extractall``
   writes to disk.
6. **Backup does not include untracked experiment directories**
   (e.g. ``vault/trash/``). The trash is intentionally excluded —
   it carries the shredded bodies of forgotten memories and is not
   part of the restorable surface.
7. **Packaging fix is the only P9 delta in this revision.** The
   privacy / backup / doctor layers were reported in the previous
   P9 revision and are unchanged here. The wheel now ships the
   built React SPA so a clean ``pip install`` does not need a
   developer checkout. ``tests/integration/test_packaging_v1.py``
   is the regression net.

---

## 6. Stop boundary

This revision stops at P9 + the packaging fix that closes the
``docs/07`` row "package Python and built web assets". Privacy /
backup / doctor / packaging are shipped; the v1 acceptance row from
`docs/07` ("Source integrity + recovery + privacy + backup + doctor
+ packaging") is closed and the wheel now ships the built React SPA
so `memory ui` serves the real interface from a clean install.

### Final integration addendum (2026-10-09, controller)

The P7/P8 acceptance report
([`docs/22-v1-p7-p8-acceptance.md`](22-v1-p7-p8-acceptance.md)) has
landed in `main` since this revision was drafted. The current README
status line is therefore **v1 complete (all of T01–T17 ships)** and
no longer needs to flag the UI as a concurrent task. P9 did not
author UI code; the SPA that the wheel ships is the `web/dist/`
build artefact produced by P7 and mounted by P8. The other P9
evidence (privacy / backup / doctor / packaging tests, the clean-venv
smoke, the clean-wheel install smoke) is unchanged.

This addendum makes the report internally consistent with the final
integrated repository without re-running any suite and without
claiming P9 wrote UI code.

---

## 7. Privacy HEAD durability fix (fail-closed restore, 2026-10-09)

This revision closes the residual restore path that the previous
"v1 complete" report did not catch: `read_live_privacy_ledger`
synthesised a `LivePrivacyLedger(version=0, epoch="epoch-0000", ...)`
when the live vault had no privacy HEAD and no deletion manifests.
That made an old v0 bundle restorable into a brand-new, un-initialised
`--vault` directory, even though the dispatcher had no deletion
receipts to consult for that vault — the synthetic v0 ledger was
treated as authoritative and the bundle was silently mounted. In a
production incident, an attacker (or a forgetful operator) who could
swap `--vault` could therefore resurrect forgotten content from any
old bundle.

### What changed

1. **`src/memory_agent/application/privacy.py::ensure_privacy_head`**
   is the only code path that is permitted to write a v0 privacy HEAD
   into a vault that previously had none. It atomically (via
   `atomic_write_json` → write, fsync, rename, dir fsync) writes
   `<vault>/privacy/HEAD.json` with `version=0, epoch="epoch-0000",
   schema_version=1` only when:
     * the HEAD is absent **and**
     * `<vault>/privacy/deletions/` contains zero `*.json` receipts.

   If the HEAD is already present, `ensure_privacy_head` returns it
   verbatim (idempotent re-init). If the HEAD is absent but at least
   one receipt is on disk, it raises `PrivacyError` — a forget cannot
   have produced a manifest without bumping the HEAD, so the state is
   inconsistent and the operator must reconcile manually. No bypass
   flag, integer override, or compatibility shim.

2. **`src/memory_agent/application/dispatch.py::_cmd_init`** invokes
   `ensure_privacy_head` after creating the SQLite schema. Every
   freshly-initialised vault therefore carries a durable v0 privacy
   HEAD before any forget or restore can touch it. The init response
   surfaces `privacy_head_version` and `privacy_epoch` so the
   operator can see the contract was honoured. Re-init leaves an
   existing HEAD alone.

3. **`src/memory_agent/application/backup.py::read_live_privacy_ledger`**
   no longer synthesises a v0 ledger. A missing HEAD is **always**
   refused with `BackupError` (with a message pointing the operator
   at `memory init`); the previous "no HEAD + empty deletions ⇒
   trust v0" branch is gone. The same refusal fires if the HEAD is
   absent but deletion manifests exist (the torn-write path) — the
   previous code already raised there, and the contract is now
   uniform.

4. **`tests/integration/test_backup_v1.py::_seed_vault`** uses
   `ensure_privacy_head` so the helper matches the contract
   `memory init` enforces. Without this step every restore test that
   consults `read_live_privacy_ledger` would fail with `BackupError`
   before it ran.

### Acceptance evidence (10 new tests)

```
tests/integration/test_backup_v1.py
  test_read_live_privacy_ledger_raises_on_missing_path
    Unit invariant: a fresh, never-initialised vault raises
    BackupError on `read_live_privacy_ledger`, with the message
    pointing at `memory init`. The synthetic v0 ledger is no longer
    reachable.
  test_read_live_privacy_ledger_raises_when_deletions_exist_without_head
    Unit invariant: an orphan manifest without a HEAD also refuses,
    with a torn-state message.
  test_init_cli_creates_v0_privacy_head
    Subprocess: `memory init` produces `privacy/HEAD.json` with
    `version=0, epoch="epoch-0000", schema_version=1`; the response
    payload returns `privacy_head_version=0` and
    `privacy_epoch="epoch-0000"`.
  test_init_cli_is_idempotent_on_existing_privacy_head
    Subprocess: re-running `init` does NOT rewrite a non-zero HEAD;
    `forget` still bumps from the same base.
  test_init_cli_refuses_when_deletions_exist_without_head
    Subprocess: `init` refuses with INTERNAL when orphan manifests
    are present without a HEAD (no silent v0 synthesis on the
    operator path either).
  test_backup_create_cli_maintains_v0_privacy_head
    Subprocess: `backup create` on a freshly-initialised vault
    produces a bundle whose `privacy/HEAD.json` is v0 with the
    empty-concat sha256 (matches the live ledger exactly).
  test_backup_restore_cli_uninitialized_target_quarantines
    Subprocess: the acceptance scenario from the brief:
        init live -> backup v0 -> forget on live
        -> memory --vault /new/uninitialized
           backup restore --bundle old --target /empty
    fails, the target stays unmounted (no state.sqlite, no
    privacy/HEAD.json), the un-initialised vault is NOT silently
    bootstrapped. Acceptable error codes are
    {INVALID_REQUEST, INTERNAL, RECOVERY_REQUIRED} — two layers
    of defence (the dispatcher RECOVERY_REQUIRED gate and the
    privacy HEAD reader) both close the path.
  test_backup_restore_cli_uninitialized_target_quarantines_no_deletions
    Subprocess: even when the live vault's privacy layer is fully
    empty (no HEAD, no deletions, no privacy directory at all),
    restore still fails.
  test_backup_restore_cli_init_but_no_privacy_head_quarantines
    Subprocess: defence-in-depth — when `state.sqlite` is present
    (so the dispatcher gate doesn't fire) but the privacy HEAD
    has been dropped, restore still refuses with the privacy
    HEAD message specifically.
  test_backup_restore_cli_normal_after_forget_succeeds_and_respects_deletion
    Subprocess: the corollary scenario — `init` then `forget` then
    `backup create` then `backup restore --vault live` succeeds,
    restores a clean target with the v1 HEAD preserved, and the
    forgotten memory's projection is NOT active in the restored
    vault (the manifest is honoured).
```

### Final results

```
$ python3 -m pytest tests/ --ignore=tests/v -q
396 passed, 12 warnings in 75.20s

$ python3 -m pytest tests/integration/test_privacy_v1.py \
                   tests/integration/test_backup_v1.py \
                   tests/integration/test_doctor_v1.py \
                   tests/integration/test_cli_v1_p9.py -q
54 passed, 11 warnings in 12.16s
```

The un-initialised-vault restore path is now closed end-to-end.
`read_live_privacy_ledger` no longer treats missing as trusted
empty, `memory init` is the only operator-authorised safe path
that may write a v0 HEAD, and the CLI subprocess test pins the
behaviour against the real argv parser. No real session /
Telegram / deployment was touched.

---

## 8. Privacy ledger DB backstop (2026-10-09, controller)

The v9 fix closed the *file-only* resurrection path. This revision
closes the *DB-side* one. `privacy_deletions` is itself a deletion
record: a vault whose state DB still records a privacy_deletions
row but whose on-disk HEAD + manifest files were lost must NOT be
silently re-initialised to v0, and a vault whose HEAD survived but
whose on-disk manifest is missing or altered must NOT produce a
ledger that a forged same-version bundle can pass.

### What changed

1. **`src/memory_agent/application/privacy.py::ensure_privacy_head`**
   additionally inspects the live vault's state DB before minting
   v0:

     * If `state.sqlite` exists and records any `privacy_deletions`
       rows, refuse with `PrivacyError`. A vault whose DB still
       records privacy deletions cannot be safely initialised by
       writing v0; an old v0 bundle could otherwise be mounted
       against it and resurrect forgotten content.
     * If `state.sqlite` exists but cannot be safely read
       (corrupt / not SQLite / wrong schema), refuse. We cannot
       claim "the DB has no rows" if we cannot read the DB.
     * The previously enforced rules are unchanged: HEAD missing +
       manifest files present → refuse; HEAD present → return
       verbatim (idempotent re-init).

3. **`src/memory_agent/application/backup.py::read_live_privacy_ledger`**
   verifies the live vault's DB against the on-disk manifest:

     * For every `privacy_deletions` row (joined with the matching
       `privacy_blocks` rows for source_block receipts), the
       on-disk manifest file must exist and its `op_id`, `kind`,
       `targets`, and `version` must match the DB row byte-for-byte.
     * A row whose receipt file is missing or altered → `BackupError`.
       The live ledger hash would otherwise be "empty" (sha256 of
       zero files) and a forged same-version bundle that has been
       sanitised of the receipts the DB still remembers would
       otherwise pass the version+hash comparison.
     * Disk ahead + DB missing is **allowed**: a crash between the
       HEAD swap and the DB mirror is the one place the file
       ledger priority is preserved. The doctor
       (`privacy_db_behind_disk`) catches the lag for the operator.
     * DB row whose `version` exceeds the on-disk HEAD → `BackupError`.
       The HEAD swap didn't complete; the ledger is torn.
     * A corrupt / unreadable DB → `BackupError`. We refuse to
       trust an empty ledger when we cannot verify it.

4. **`src/memory_agent/application/backup.py::BackupService.create`**
   now refuses with `BackupError` when the live vault has no
   privacy HEAD. A bundle without a privacy HEAD would let an old
   v0 bundle be silently mounted against a future target as if it
   had a v0 privacy ledger. The only operator-authorised safe
   path that may write a v0 HEAD is `ensure_privacy_head`; the
   service forces the operator to `memory init` before any
   snapshot can be taken.

5. **`tests/integration/test_backup_v1.py::test_backup_restore_fails_closed_when_ledger_is_inconsistent`**
   was updated to take the v1 bundle *before* the tamper step
   (the new `backup.create` HEAD-missing refusal now closes the
   silent v0 synthesis path at the snapshot boundary too).

### Acceptance evidence (12 new tests)

```
tests/integration/test_privacy_db_backstop_v1.py
  test_ensure_privacy_head_refuses_when_db_records_deletion
    init -> backup v0 -> forget -> remove HEAD + receipts.
    ensure_privacy_head refuses (PrivacyError naming the DB row).
    ``memory init`` (which calls ensure_privacy_head) refuses with
    INTERNAL end-to-end via the CLI subprocess.
    backup.create refuses (no HEAD to snapshot).
    CLI backup restore of the pre-tamper bundle refuses; target
    stays unmounted.
  test_ensure_privacy_head_refuses_when_db_is_unreadable
    Unit invariant: a state DB that exists but cannot be opened
    (corrupt / not SQLite) refuses the v0 synthesis — we cannot
    claim "no rows" without seeing the rows.
  test_ensure_privacy_head_allows_fresh_init_with_empty_db
    Sanity: a genuinely fresh init (no privacy_deletions rows)
    atomically writes v0.
  test_ensure_privacy_head_blocked_db_refuses_via_init_cli
    Subprocess: ``memory init`` fails closed when the DB records a
    privacy_deletions row but the on-disk HEAD + receipts are
    missing.
  test_read_live_privacy_ledger_refuses_missing_receipt_for_db_row
    Scenario from the brief: HEAD survives at v1, receipt file
    missing, DB still records the row. The reader refuses.
  test_read_live_privacy_ledger_refuses_altered_receipt
    The on-disk manifest was rewritten to point at a different
    memory_id; the reader detects the targets mismatch.
  test_read_live_privacy_ledger_allows_disk_ahead_db_lag
    The one place file-ledger priority is preserved: disk ahead +
    DB missing is allowed; the ledger succeeds and the doctor
    (``privacy_db_behind_disk``) catches the lag.
  test_read_live_privacy_ledger_refuses_db_version_above_head
    The DB row's version exceeds the on-disk HEAD; the HEAD swap
    didn't complete; the reader refuses.
  test_read_live_privacy_ledger_refuses_corrupt_db
    Corrupt DB ⇒ refuse; refuse to trust an empty ledger we
    cannot verify.
  test_read_live_privacy_ledger_happy_path_still_succeeds
    Regression net: a live vault whose DB and on-disk manifest
    agree produces a ``LivePrivacyLedger`` and the post-forget
    restore honours the deletion.
  test_backup_create_refuses_when_db_records_deletion_without_head
    backup.create refuses early when the live vault has no privacy
    HEAD (HEAD missing ⇒ unsafe to snapshot).
  test_backup_restore_cli_rejects_when_db_lacks_receipt
    End-to-end CLI subprocess: pre-tamper bundle + tampered live
    vault (no HEAD, no receipts, DB still records the deletion)
    refuses ``memory init`` and refuses ``backup restore`` with
    INTERNAL; target stays unmounted.
```

### Final results

```
$ python3 -m pytest tests/ --ignore=tests/v -q
408 passed, 13 warnings in 76.92s

$ python3 -m pytest tests/integration/test_privacy_v1.py \
                   tests/integration/test_backup_v1.py \
                   tests/integration/test_privacy_db_backstop_v1.py \
                   tests/integration/test_doctor_v1.py \
                   tests/integration/test_cli_v1_p9.py -q
66 passed, 11 warnings in 14.16s
```

No real session / Telegram / deployment was touched.

**No commits were made.** Per the task brief and the repository
boundary, ``git add`` / ``commit`` / ``push`` were deliberately not
run. The working tree is intentionally untracked.
