"""Commit service: write immutable memory objects + commit manifest,
atomically swap HEAD, then SQL finalize.

Crash-recovery protocol (v0.1):

  * ``prepare_commit`` writes the immutable memory files + manifest +
    a DB intent with ``phase='prepared'``. It is the *only* step that
    touches disk before HEAD has an answer; HEAD still points at the
    parent. It writes *no* memory_projection rows, *no*
    conflict_projection rows, and never finalises a batch. A
    prepared-but-unpublished intent is therefore unreachable from the
    outside.

  * ``publish`` first verifies HEAD still names the intent's parent.
    If HEAD has already advanced past parent (e.g. caller crashed and
    another path published a competing commit) publish() refuses to
    swap HEAD and raises ``CommitError``; the caller surfaces this as
    ``recovery_required``. On success, HEAD.json is replaced
    atomically and the intent's phase is bumped to ``published``.

  * ``finalize`` runs only after HEAD names the published commit and
    the manifest is readable + matches the intent's commit_id. It
    materialises ``memory_projection`` rows AND ``conflict_projection``
    rows together in a single ``BEGIN IMMEDIATE`` SQL transaction. The
    batch's terminal status is updated in the same transaction.

  * ``recover_unfinished`` reconciles intent phases against HEAD.
    Three HEAD cases are recognised for ``prepared`` intents:

      - HEAD == parent: caller crashed between prepare and publish.
        Abort the intent; the batch stays ``ready`` so a future
        ``maintain`` can re-plan it.

      - HEAD == intent.commit_id: the HEAD-before-DB crash window.
        ``publish()`` swapped HEAD atomically but the caller crashed
        before the DB phase bump landed. Validate the manifest, then
        advance prepared -> published -> finalized exactly once.

      - HEAD neither: another path published a competing commit (or
        HEAD was rewritten externally). Surface as
        ``recovery_required`` for the operator.

    Published intents: finalize() materialises the projection rows
    when HEAD == commit and the manifest is readable. Missing /
    corrupt manifests or HEAD pointing elsewhere stay as
    ``recovery_required``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_agent.application.ports import MemoryCommitPort
from memory_agent.contracts.memory import MemoryAtom
from memory_agent.domain.lifecycle import assert_valid_status_transition
from memory_agent.infrastructure.files import (
    VaultLayout,
    atomic_write_json,
    atomic_write_text,
)
from memory_agent.infrastructure.sqlite import transaction
from memory_agent.infrastructure.sqlite.repositories import (
    CommitIntentRepo,
    ConflictRepo,
    MemoryProjectionRepo,
)


class CommitError(RuntimeError):
    """A non-recoverable error inside the commit service."""


class CommitService(MemoryCommitPort):
    def __init__(self, *, vault: VaultLayout, conn):
        self.vault = vault
        self.conn = conn
        self.intents = CommitIntentRepo(conn)
        self.memory_proj = MemoryProjectionRepo(conn)
        self.conflict_repo = ConflictRepo(conn)

    # ------------------------------------------------------------------
    # prepare_commit
    # ------------------------------------------------------------------
    def prepare_commit(
        self,
        *,
        proposed_memories: list[MemoryAtom],
        pending_proposals: list[dict[str, Any]],
        base_commit_id: str,
        privacy_epoch: str,
        batch_ids: list[str],
        target_unit_ids: list[str],
    ) -> dict[str, Any]:
        """Write immutable files + DB intent with phase='prepared'.

        HEAD is *not* advanced. No projection rows (memory or
        conflict) are written here. No batch terminal decision is made
        here. The caller is expected to ``publish()`` (which atomically
        swaps HEAD) and then ``finalize()`` (which materialises the
        projection rows in one SQL transaction).
        """
        ts = datetime.now(timezone.utc).isoformat()
        commit_id = "commit_" + hashlib.sha256(
            f"{base_commit_id}|{ts}|{batch_ids}|{sorted(target_unit_ids)}".encode()
        ).hexdigest()[:12]
        commit_dir = self.vault.commits_dir / commit_id
        commit_dir.mkdir(parents=True, exist_ok=True)

        # 1. Write each memory object to its immutable path.
        memory_paths: list[Path] = []
        for mem in proposed_memories:
            self._validate_status_transition(mem.status, "active")
            mem_path = self.vault.memory_revision_path(mem.memory_id, mem.revision)
            mem_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(mem_path, self._render_memory_md(mem))
            memory_paths.append(mem_path)

        # 2. Build the manifest. Pending proposals are referenced by
        #    file path under <vault>/objects/conflicts/<id>.json; the
        #    file itself is also written here as an immutable artifact,
        #    but the corresponding ``conflict_projection`` row is *not*
        #    written until finalize() succeeds on a HEAD-advanced
        #    commit. This is the only place the conflict JSON lives.
        pending_artifacts: list[dict[str, str]] = []
        for prop in pending_proposals:
            prop_id = prop.get("candidate_id") or "prop"
            path = self.vault.objects_dir / "conflicts" / f"{prop_id}.json"
            atomic_write_json(path, prop)
            pending_artifacts.append({
                "candidate_id": prop_id,
                "file": str(path),
            })

        # 3. Write the manifest.
        manifest = {
            "commit_id": commit_id,
            "parent_id": base_commit_id,
            "created_at": ts,
            "privacy_epoch": privacy_epoch,
            "applied_batch_ids": list(batch_ids),
            "target_unit_ids": list(target_unit_ids),
            "memories": [
                {"memory_id": m.memory_id, "revision": m.revision,
                 "path": str(self.vault.memory_revision_path(m.memory_id, m.revision)),
                 # The committed scope travels in the manifest so
                 # finalize() projects the *authoritative* scope
                 # instead of inventing an empty one. Recall's
                 # project/domain filters read these columns.
                 "scope": m.scope.model_dump(mode="json"),
                 "project_ids": list(m.scope.project_ids),
                 "domains": list(m.domains),
                 "primary_domain": m.primary_domain}
                for m in proposed_memories
            ],
            "pending_proposals": pending_artifacts,
        }
        manifest_path = commit_dir / "manifest.json"
        atomic_write_json(manifest_path, manifest)

        # 4. Register the intent in the DB.
        self.intents.insert({
            "commit_id": commit_id,
            "parent_id": base_commit_id,
            "manifest_ref": str(manifest_path),
            "batch_ids": list(batch_ids),
            "target_unit_ids": list(target_unit_ids),
            "expected_revisions": [m.memory_id for m in proposed_memories],
            "privacy_epoch": privacy_epoch,
            "phase": "prepared",
        })
        return {
            "commit_id": commit_id,
            "parent_id": base_commit_id,
            "phase": "prepared",
            "manifest_ref": str(manifest_path),
        }

    # ------------------------------------------------------------------
    # publish
    # ------------------------------------------------------------------
    def publish(self, commit_id: str) -> dict[str, Any]:
        """Atomically swap HEAD.json and bump phase to 'published'.

        Pre-conditions enforced here:
          * the intent's phase is 'prepared' or 'published' (idempotent
            for 'published');
          * HEAD.json names the intent's parent_id. If HEAD has already
            advanced (another path published a competing commit while
            we were preparing), ``CommitError`` is raised and HEAD is
            not swapped. The intent stays 'prepared' so the operator
            can decide via ``doctor``.
          * the manifest file is readable and parses as JSON. If the
            manifest is missing or corrupt, ``CommitError`` is raised
            and HEAD is not swapped.

        On success: HEAD.json is replaced atomically and the intent's
        phase is set to 'published'. Projection rows are still NOT
        written; that is the job of ``finalize``.
        """
        intent = self.intents.get(commit_id)
        if intent is None:
            raise KeyError(f"unknown commit_id {commit_id}")
        if intent["phase"] == "published":
            return {"commit_id": commit_id, "phase": "published"}
        if intent["phase"] not in {"prepared", "published"}:
            return {"commit_id": commit_id, "phase": intent["phase"]}

        # HEAD invariant: current HEAD must name the intent's parent.
        # The initial commit is allowed when there is no HEAD on disk
        # yet and the intent's parent_id is "root".
        # We also refuse if the current HEAD's commit has a missing
        # or corrupt manifest (the vault is in a recovery_required
        # state and new writes must be blocked).
        current_head = self._read_head_commit()
        parent = intent.get("parent_id")
        if current_head != parent:
            if not (current_head is None and parent == "root"):
                raise CommitError(
                    f"HEAD has advanced past the intent's parent for commit "
                    f"{commit_id!r}: current HEAD commit_id={current_head!r}, "
                    f"expected parent_id={parent!r}; refusing to publish"
                )
        # Block new writes if the current HEAD's intent has a missing
        # or corrupt manifest (recovery_required state).
        if current_head is not None:
            head_intent = self.intents.get(current_head)
            if head_intent is not None and not self._manifest_is_readable(head_intent):
                raise CommitError(
                    f"HEAD points at commit {current_head!r} whose manifest "
                    "is missing or corrupt; recovery_required — refusing to "
                    "publish new writes until operator resolves"
                )

        # Manifest must be readable. Missing or corrupt manifests block
        # the publish path; recovery surfaces them as recovery_required.
        if not self._manifest_is_readable(intent):
            raise CommitError(
                f"manifest for commit {commit_id!r} is missing or corrupt; "
                "recovery_required — refusing to publish"
            )

        # Atomic HEAD swap.
        head_tmp = self.vault.root / "HEAD.json.tmp"
        atomic_write_json(
            head_tmp,
            {"commit_id": commit_id, "parent_id": parent},
        )
        head_tmp.replace(self.vault.head_path)
        # Mark phase=published; finalize is a separate SQL transaction.
        self.intents.update_phase(commit_id, "published")
        return {"commit_id": commit_id, "phase": "published"}

    def abort(self, commit_id: str) -> None:
        """Mark a prepared intent as aborted.

        Used by recovery when a prepared intent with HEAD at parent
        must be discarded (caller is gone; intent is unreachable). Also
        used by callers that want to throw away a prepared intent
        before publishing. Aborted intents never have projection rows.
        """
        intent = self.intents.get(commit_id)
        if intent is None:
            return
        if intent["phase"] == "finalized":
            return
        self.intents.update_phase(commit_id, "aborted")

    # ------------------------------------------------------------------
    # recover_unfinished
    # ------------------------------------------------------------------
    def recover_unfinished(self) -> dict[str, Any]:
        """Reconcile intent phases against HEAD at startup.

        Prepared intents: three mutually-exclusive HEAD cases.

          * HEAD names the intent's ``commit_id`` (HEAD already swapped
            to this intent, DB phase still ``prepared``). This is the
            **HEAD-before-DB crash window**: HEAD.json was replaced
            atomically by ``publish()`` but the DB phase update was
            lost before it committed. Recovery validates the on-disk
            manifest, then advances the DB phase through
            ``published`` to ``finalized`` exactly once. HEAD is
            untouched. After recovery the intent is indistinguishable
            from one that crashed between ``publish()`` and
            ``finalize()``.

          * HEAD names the intent's ``parent_id`` (HEAD not yet
            advanced). The caller likely crashed between ``prepare()``
            and ``publish()``; the caller is gone and the intent is
            unreachable. Abort the intent so it cannot auto-publish.
            The batch stays in ``ready`` so a future ``maintain``
            can re-plan it.

          * HEAD names neither the ``commit_id`` nor the
            ``parent_id``: another path published a competing commit
            while our caller was alive, OR HEAD was rewritten
            externally. Surface as ``recovery_required`` for the
            operator.

        Published intents: HEAD should already name the published
        commit; finalize() materialises the projection rows. If HEAD
        does NOT name the published commit, OR the manifest is
        missing/corrupt, surface as ``recovery_required`` rather than
        auto-finalising on the wrong base.

        Finalised intents are not touched. Missing or corrupt
        manifests surface as ``recovery_required`` rather than being
        silently auto-aborted.
        """
        prepared = self.intents.list_by_phase("prepared")
        published = self.intents.list_by_phase("published")
        finalized_count = 0
        aborted_count = 0
        advanced_to_published = 0
        recovery_required: list[str] = []
        current_head = self._read_head_commit()
        for intent in prepared:
            parent = intent.get("parent_id")
            commit_id = intent.get("commit_id")
            if not isinstance(commit_id, str) or not commit_id:
                # Should never happen — intents always carry a commit_id.
                # Surface as recovery_required rather than silently drop.
                recovery_required.append(str(commit_id) if commit_id else "<missing>")
                continue
            # The initial commit is allowed when there is no HEAD on
            # disk yet and the intent's parent_id is "root". Treat
            # that case as "HEAD at parent" so the prepared intent
            # is aborted (we cannot recover a prepared intent whose
            # caller is gone).
            head_at_parent = (
                current_head == parent
                or (current_head is None and parent == "root")
            )
            head_at_commit = current_head == commit_id
            if head_at_commit:
                # ----- HEAD-before-DB crash window -----
                # HEAD has been atomically swapped to this intent
                # already; the caller crashed between the atomic
                # HEAD replace and the DB phase bump. Validate the
                # manifest, then advance phase prepared -> published
                # -> finalized in one SQL transaction. Recovery
                # must be safe to run twice (finalize is idempotent),
                # and must NOT touch HEAD.
                if not self._manifest_is_readable(intent):
                    recovery_required.append(commit_id)
                    continue
                try:
                    self.intents.update_phase(commit_id, "published")
                    advanced_to_published += 1
                    self._finalize(commit_id)
                    finalized_count += 1
                except CommitError:
                    recovery_required.append(commit_id)
                continue
            if not head_at_parent:
                # HEAD names neither parent nor commit: another path
                # published a competing commit. The caller is gone;
                # we do not auto-publish. Surface for operator.
                recovery_required.append(commit_id)
                continue
            # HEAD still names the parent: caller likely crashed
            # between prepare and publish. The intent is unreachable
            # (the caller cannot be told to retry). Abort it so we
            # don't silently auto-publish. The batch stays in
            # 'ready' / 'retry_wait' and can be re-planned on a
            # future maintain.
            self.abort(commit_id)
            aborted_count += 1
        for intent in published:
            if not self._manifest_is_readable(intent):
                recovery_required.append(intent["commit_id"])
                continue
            if current_head != intent["commit_id"]:
                # HEAD doesn't actually contain this published commit.
                # Either the HEAD.json was lost or this intent was
                # orphaned. Surface for operator decision.
                recovery_required.append(intent["commit_id"])
                continue
            try:
                self._finalize(intent["commit_id"])
                finalized_count += 1
            except CommitError:
                recovery_required.append(intent["commit_id"])
        return {
            "prepared": len(prepared),
            "published": len(published),
            "finalized": finalized_count,
            "aborted": aborted_count,
            "advanced_to_published": advanced_to_published,
            "recovery_required": recovery_required,
        }

    # ------------------------------------------------------------------
    # finalize (internal; called by publish() via recovery, or by tests)
    # ------------------------------------------------------------------
    def finalize(self, commit_id: str) -> dict[str, Any]:
        """Materialise memory_projection AND conflict_projection rows
        for a published intent.

        Pre-conditions enforced here:
          * intent exists and phase is 'published';
          * HEAD.json names ``commit_id`` (HEAD actually contains the
            commit we're about to finalise);
          * manifest is readable and its ``commit_id`` matches.

        Projection rows are written together in one SQL transaction.
        """
        intent = self.intents.get(commit_id)
        if intent is None:
            raise CommitError(f"unknown commit_id {commit_id}")
        if intent["phase"] == "finalized":
            return {"commit_id": commit_id, "phase": "finalized"}
        if intent["phase"] != "published":
            raise CommitError(
                f"cannot finalize commit {commit_id!r} in phase "
                f"{intent['phase']!r} before publish()"
            )
        self._finalize(commit_id)
        return {"commit_id": commit_id, "phase": "finalized"}

    def _finalize(self, commit_id: str) -> None:
        intent = self.intents.get(commit_id)
        if intent is None:
            raise CommitError(f"unknown commit_id {commit_id}")
        if intent["phase"] == "finalized":
            return
        if intent["phase"] != "published":
            raise CommitError(
                f"cannot finalize commit {commit_id!r} in phase "
                f"{intent['phase']!r} before publish()"
            )
        # HEAD must actually contain this commit.
        current_head = self._read_head_commit()
        if current_head != commit_id:
            raise CommitError(
                f"HEAD does not name commit {commit_id!r}: refusing to "
                "materialise projection rows"
            )
        manifest_path = Path(intent["manifest_ref"])
        if not manifest_path.exists():
            raise CommitError(
                f"manifest missing for commit {commit_id!r}"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommitError(
                f"manifest corrupt for commit {commit_id!r}: {exc}"
            ) from exc
        if manifest.get("commit_id") != commit_id:
            raise CommitError(
                f"manifest commit_id mismatch for {commit_id!r}"
            )

        # Insert memory_projection rows. v0.1 denormalises the
        # statement into the projection so the reconciler can match
        # duplicate/conflicting candidates without re-reading files.
        mem_rows: list[dict[str, Any]] = []
        for mem_meta in manifest.get("memories", []):
            statement = ""
            mem_md = Path(mem_meta["path"])
            if mem_md.exists():
                text = mem_md.read_text(encoding="utf-8")
                if text.startswith("---"):
                    end = text.find("\n---", 3)
                    if end != -1:
                        body = text[end + 4:].lstrip()
                        statement = body.splitlines()[0] if body.strip() else ""
                else:
                    statement = text.splitlines()[0] if text.strip() else ""
            mem_rows.append({
                "memory_id": mem_meta["memory_id"],
                "revision": mem_meta["revision"],
                "status": "active",
                # Scope comes from the manifest, which prepare_commit
                # wrote from the committed MemoryAtom. Projecting an
                # empty scope here would make every memory look global
                # to recall's project/domain filters and to the
                # reconciler's cross-batch overlap check.
                "scope": mem_meta.get("scope") or {},
                "project_ids": list(mem_meta.get("project_ids") or []),
                "domains": list(mem_meta.get("domains") or []),
                "commit_id": commit_id,
                "statement": statement,
            })

        # Build conflict_projection rows for any pending proposals.
        conflict_rows: list[dict[str, Any]] = []
        for prop_meta in manifest.get("pending_proposals", []):
            cid = prop_meta["candidate_id"]
            prop_file = Path(prop_meta.get("file") or "")
            intersects: list[str] = []
            if prop_file.exists():
                try:
                    payload = json.loads(prop_file.read_text(encoding="utf-8"))
                    intersects = list(payload.get("intersects") or [])
                except json.JSONDecodeError:
                    intersects = []
            old_memory_ref = intersects[0] if intersects else None
            conflict_rows.append({
                "conflict_id": cid,
                "revision": 1,
                "status": "pending",
                "old_memory_ref": old_memory_ref,
                "new_memory_ref": None,
                "commit_id": commit_id,
            })

        # Write everything together so a crashed partial finalize cannot
        # leave the projection in a half-written state.
        with transaction(self.conn):
            for row in mem_rows:
                self.memory_proj.upsert(row)
            for row in conflict_rows:
                self.conflict_repo.open(row)
            self.intents.update_phase(commit_id, "finalized")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _manifest_is_readable(self, intent: dict[str, Any]) -> bool:
        manifest_path = Path(intent["manifest_ref"])
        if not manifest_path.exists():
            return False
        try:
            json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        return True

    def _read_head_commit(self) -> str | None:
        if not self.vault.head_path.exists():
            return None
        try:
            data = json.loads(self.vault.head_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return data.get("commit_id")

    def _validate_status_transition(self, current: str, target: str) -> None:
        assert_valid_status_transition(current, target)

    def _render_memory_md(self, mem: MemoryAtom) -> str:
        """Render the YAML frontmatter + Markdown body.

        Memory text never enters SQLite; this file is the canonical store.
        """
        fm = {
            "schema_version": mem.schema_version,
            "id": mem.memory_id,
            "revision": mem.revision,
            "status": mem.status,
            "kind": mem.kind,
            "abstraction": mem.abstraction,
            "statement": mem.statement,
            "subject": mem.subject,
            "predicate": mem.predicate,
            "value": mem.value,
            "modality": mem.modality,
            "scope": mem.scope.model_dump(),
            "domains": mem.domains,
            "primary_domain": mem.primary_domain,
            "tags": mem.tags,
            "horizon": mem.horizon,
            "valid_from": mem.valid_from,
            "valid_until": mem.valid_until,
            "asserted_at": mem.asserted_at,
            "recorded_at": mem.recorded_at,
            "evidence": [e.model_dump() for e in mem.evidence],
            "relations": mem.relations,
            "supersedes": mem.supersedes,
        }
        body_lines = [mem.statement, "", "## 来源", ""]
        for ev in mem.evidence:
            body_lines.append(f"- role={ev.role} quote={ev.quote!r} span=[{ev.span_start},{ev.span_end})")
        head = _render_frontmatter(fm)
        return f"---\n{head}---\n\n" + "\n".join(body_lines) + "\n"


def _render_frontmatter(d: dict[str, Any]) -> str:
    """Render YAML frontmatter using PyYAML when present, with a minimal
    fallback for environments where PyYAML is missing. The fallback
    covers the JSON-shaped subset we emit, so clean-venv installs
    never break at first commit."""
    try:
        import yaml  # type: ignore[import-not-found]
        return yaml.safe_dump(d, allow_unicode=True, sort_keys=False)
    except Exception:
        return _mini_yaml(d)


def _mini_yaml(d: dict[str, Any]) -> str:
    """A tiny YAML emitter for dicts of JSON-like values. Used as a fallback
    if PyYAML is unavailable. v0.1 keeps the memory frontmatter readable
    even without PyYAML."""

    def emit(v: Any, indent: int = 0) -> list[str]:
        pad = "  " * indent
        if isinstance(v, dict):
            lines: list[str] = []
            for k, val in v.items():
                if isinstance(val, (dict, list)) and val:
                    lines.append(f"{pad}{k}:")
                    lines.extend(emit(val, indent + 1))
                else:
                    lines.append(f"{pad}{k}: {_yaml_scalar(val)}")
            return lines
        if isinstance(v, list):
            lines = []
            for el in v:
                if isinstance(el, (dict, list)):
                    lines.append(f"{pad}-")
                    lines.extend(emit(el, indent + 1))
                else:
                    lines.append(f"{pad}- {_yaml_scalar(el)}")
            return lines
        return [f"{pad}{_yaml_scalar(v)}"]

    return "\n".join(emit(d)) + "\n"


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if any(ch in s for ch in ":#\n"):
        return json.dumps(s, ensure_ascii=False)
    return s


def prepare_and_publish(*, vault: VaultLayout, conn, **kwargs) -> dict[str, Any]:
    """Convenience wrapper used by the CLI.

    Calls ``prepare_commit``, ``publish``, then ``finalize`` in
    sequence. Publish is checked: if HEAD has advanced past the
    intent's parent, the helper raises ``CommitError`` and the caller
    surfaces recovery_required.
    """
    svc = CommitService(vault=vault, conn=conn)
    intent = svc.prepare_commit(**kwargs)
    svc.publish(intent["commit_id"])
    svc.finalize(intent["commit_id"])
    return intent