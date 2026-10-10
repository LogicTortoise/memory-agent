"""P0 v0.1 gates: real source evidence on recall, honest scope
projection, strict scope normalisation, and no arbitrary winner for
same-batch contradictions.

Every test here is a falsifiable claim about behaviour the previous
implementation got wrong:

  * recall returned a slice of the *memory Markdown* labelled as if it
    were source bytes;
  * the memory projection was written with ``scope={}`` so recall's
    project/domain filters were no-ops;
  * ``_normalise_scope(None)`` silently broadened to ``global``;
  * same-batch contradictions published whichever candidate came first.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_agent.application.commit import CommitService
from memory_agent.application.dispatch import _evidence_text_lookup
from memory_agent.application.plan_batches import plan_batches_for_session
from memory_agent.application.process_batches import (
    BatchProcessingError,
    _candidate_set_from_payload,
    _normalise_scope,
    process_batch,
)
from memory_agent.application.recall import RecallService
from memory_agent.application.reconcile import Reconciler
from memory_agent.application.scan import default_adapters, scan_once
from memory_agent.domain.batching import BatchPolicyInput
from memory_agent.infrastructure.files import VaultLayout
from memory_agent.infrastructure.sqlite import open_state_db
from memory_agent.infrastructure.sqlite.repositories import MemoryProjectionRepo

from memory_agent.application.dispatch import _event_from_row_with_text
from memory_agent.contracts.candidates import CandidateSet, Scope
from memory_agent.contracts.memory import MemoryAtom


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _StubProvider:
    """Provider that returns exactly one hand-built candidate whose
    statement paraphrases the source and whose quote is a *middle*
    substring of the source event text."""

    name = "stub-model"

    def __init__(self, *, statement: str, quote: str, scope: dict | str,
                 role: str = "user", origin: str = "user_session"):
        self.statement = statement
        self.quote = quote
        self.scope = scope
        self.role = role
        self.origin = origin

    def run_extract(self, task: dict) -> dict:
        members = [m for m in task.get("members", [])
                   if int(m.get("active_target", 0)) == 1]
        assert members, "batch has no active target"
        m = next((x for x in members if self.quote in (x.get("text") or "")), None)
        assert m is not None, "no active target carries the quote"
        text = m["text"]
        idx = text.index(self.quote)
        return {
            "status": "ok",
            "prompt_version": "stub_v1",
            "model": self.name,
            "candidates": [
                {
                    "candidate_id": "cand_stub_00",
                    "kind": "requirement",
                    "abstraction": "no_opinion",
                    "statement": self.statement,
                    "modality": "must",
                    "scope": self.scope,
                    "tags": [],
                    "evidence": [
                        {
                            "source_event_id": m["event_id"],
                            "source_revision_hash": m["revision_hash"],
                            "role": self.role,
                            "origin": self.origin,
                            "quote": self.quote,
                            "span_start": idx,
                            "span_end": idx + len(self.quote),
                        }
                    ],
                }
            ],
        }


def _write_jsonl(path: Path, records: list[dict]) -> bytes:
    raw = b"".join(
        json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n"
        for r in records
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _user(uuid_: str, content: str, ts: str, session: str, parent=None) -> dict:
    return {
        "type": "user", "uuid": uuid_,
        "message": {"role": "user", "content": content},
        "timestamp": ts, "sessionId": session, "parentUuid": parent,
    }


def _assistant(uuid_: str, content: str, ts: str, session: str, parent) -> dict:
    return {
        "type": "assistant", "uuid": uuid_,
        "message": {"role": "assistant", "content": content},
        "timestamp": ts, "sessionId": session, "parentUuid": parent,
    }


def _scan(vault, conn, root: Path, session: str) -> None:
    scan_once(vault=vault, conn=conn, adapters=default_adapters(),
              roots={"claude-code": str(root)})


def _events_for(conn, session: str) -> list:
    rows = conn.execute(
        "SELECT * FROM source_events WHERE session_id = ? ORDER BY source_time",
        (session,),
    ).fetchall()
    return [_event_from_row_with_text(conn, r) for r in rows]


def _first_batch(conn, session: str = "sess_scope"):
    events = _events_for(conn, session)
    planned = plan_batches_for_session(
        session_id=session, branch_id="main", events=events,
        policy=BatchPolicyInput(), conn=conn,
    )
    assert planned, "planner produced no batches"
    return planned[0]


def _evidence_for_event(event) -> list[dict]:
    """A whole-text evidence ref for a scanned event, so committed
    memories carry real, verifiable source evidence (recall omits
    items whose evidence cannot be resolved)."""
    return [{
        "source_event_id": event.event_id,
        "source_revision_hash": event.revision_hash,
        "role": event.role,
        "origin": event.origin,
        "quote": event.text,
        "span_start": 0,
        "span_end": len(event.text),
    }]


def _seeded_vault(tmp_path: Path, *, text: str = "always log decisions to vault/audit"):
    """A scanned vault with one user event, ready for direct commits."""
    root = tmp_path / "claude"
    sess = root / "projects" / "proj_seed" / "sess_seed.jsonl"
    _write_jsonl(sess, [_user("u01", text, "2026-10-08T10:00:00Z", "sess_seed")])
    vault = VaultLayout(tmp_path / "vault")
    conn = open_state_db(vault.state_db)
    _scan(vault, conn, root, "sess_seed")
    event = next(e for e in _events_for(conn, "sess_seed"))
    return vault, conn, event


def _memory(*, memory_id: str, statement: str, scope: Scope,
            domains: list[str], evidence=()) -> MemoryAtom:
    now = "2026-10-08T10:00:00+00:00"
    return MemoryAtom(
        schema_version=1, memory_id=memory_id, revision=1, status="active",
        kind="requirement", abstraction="no_opinion", statement=statement,
        subject=scope.project_ids[0] if scope.project_ids else "global",
        predicate=None, value=None, modality="must", scope=scope,
        primary_domain=None, domains=domains, tags=[], horizon="unknown",
        asserted_at=now, recorded_at=now, evidence=list(evidence),
    )


def _commit(vault, conn, mem: MemoryAtom) -> str:
    svc = CommitService(vault=vault, conn=conn)
    # Chain onto the current HEAD, exactly like the dispatcher loop does.
    head = "root"
    if vault.head_path.exists():
        head = json.loads(vault.head_path.read_text(encoding="utf-8"))["commit_id"]
    intent = svc.prepare_commit(
        proposed_memories=[mem], pending_proposals=[],
        base_commit_id=head, privacy_epoch="epoch-0",
        batch_ids=[f"b_{mem.memory_id}"], target_unit_ids=["u_x"],
    )
    svc.publish(intent["commit_id"])
    svc.finalize(intent["commit_id"])
    return intent["commit_id"]


def _recall(vault, conn, query: str, *, projects=(), domains=()) -> dict:
    return RecallService(
        vault=vault, memory_projection_repo=MemoryProjectionRepo(conn)
    ).recall(
        query=query, project_ids=list(projects), domain_ids=list(domains),
        include_archived=False, limit=20,
    )


# ---------------------------------------------------------------------------
# 1. recall evidence must be real source bytes, not Markdown offsets
# ---------------------------------------------------------------------------


class TestRecallSourceEvidence:
    def test_quote_is_exact_source_slice_and_locator_is_jsonl_record(
        self, tmp_path
    ):
        """The memory statement *paraphrases* the source; the returned
        quote is a middle substring of the *source event text*; the
        locator is the byte range of the correct JSONL record (the
        second one), not a Markdown offset and not record 1."""
        root = tmp_path / "claude"
        sess = root / "projects" / "proj_ev" / "sess_ev.jsonl"
        long_text = "从现在起，所有决策都必须记录到 vault/audit 目录里，方便复盘。"
        raw = _write_jsonl(sess, [
            _assistant("a00", "一段助手上下文记录", "2026-10-08T09:59:00Z", "sess_ev", None),
            _user("u01", "第一句无关闲聊内容", "2026-10-08T10:00:00Z", "sess_ev"),
            _assistant("a01", "好的。", "2026-10-08T10:00:01Z", "sess_ev", "u01"),
            _user("u02", long_text, "2026-10-08T10:01:00Z", "sess_ev", "a01"),
        ])
        lines = raw.split(b"\n")
        record4_start = sum(len(line) + 1 for line in lines[:3])
        # byte_end includes the record's terminating newline (the
        # reader's documented convention).
        record4_end = sum(len(line) + 1 for line in lines[:4])

        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        _scan(vault, conn, root, "sess_ev")
        planned = _first_batch(conn, "sess_ev")

        quote = "所有决策都必须记录到 vault/audit 目录里"
        provider = _StubProvider(
            statement="用户要求把决策写入审计目录以便复盘",
            quote=quote,
            scope={"level": "global"},
        )
        process_batch(
            batch_id=planned["batch"]["batch_id"], vault=vault, conn=conn,
            provider=provider,
            reconciler=Reconciler(memory_projection_repo=MemoryProjectionRepo(conn)),
            committer=CommitService(vault=vault, conn=conn),
            evidence_lookup=lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault),
            base_commit_id="root", privacy_epoch="epoch-0", run_id="run_ev",
        )

        result = _recall(vault, conn, "决策 审计")
        assert result["items"], f"recall returned no items: {result}"
        item = result["items"][0]

        # 1. The quote is the exact source substring, not a paraphrase
        #    and not a slice of the memory Markdown.
        assert item["quote"] == quote
        assert item["quote"] in long_text
        assert item["statement"] != quote  # statement is a paraphrase
        # the quote is a *middle* substring of the source event text
        assert item["quote"] != long_text
        assert item["quote"] not in item["statement"]

        # 2. The locator is the source JSONL record's byte range.
        assert item["locator"] == {
            "byte_start": record4_start,
            "byte_end": record4_end,
            "record_ordinal": 4,
        }
        # The bytes at that range really are the source record.
        raw_at_locator = raw[record4_start:record4_end].decode("utf-8")
        assert json.loads(raw_at_locator)["message"]["content"] == long_text

    def test_item_omitted_when_source_evidence_is_missing(self, tmp_path):
        """If the segment backing the memory's evidence disappears,
        recall must omit the item and warn — never fabricate a quote
        or a zero locator."""
        root = tmp_path / "claude"
        sess = root / "projects" / "proj_gone" / "sess_gone.jsonl"
        text = "always log every decision to vault/audit"
        _write_jsonl(sess, [
            _user("u01", text, "2026-10-08T10:00:00Z", "sess_gone"),
        ])
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        _scan(vault, conn, root, "sess_gone")
        planned = _first_batch(conn, "sess_gone")

        process_batch(
            batch_id=planned["batch"]["batch_id"], vault=vault, conn=conn,
            provider=_StubProvider(
                statement="user wants decisions logged", quote=text,
                scope={"level": "global"},
            ),
            reconciler=Reconciler(memory_projection_repo=MemoryProjectionRepo(conn)),
            committer=CommitService(vault=vault, conn=conn),
            evidence_lookup=lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault),
            base_commit_id="root", privacy_epoch="epoch-0", run_id="run_gone",
        )
        assert _recall(vault, conn, "decisions logged")["items"]

        # Corrupt the evidence: delete every segment file.
        for seg in vault.segments_dir.glob("*.json"):
            seg.unlink()

        result = _recall(vault, conn, "decisions logged")
        assert result["items"] == [], "recall must omit items with no resolvable evidence"
        assert result["warnings"], "recall must explain why the item was omitted"

    def test_item_omitted_when_quote_does_not_match_source(self, tmp_path):
        """Evidence whose span no longer slices the source text is
        corrupt evidence, not a licence to quote the Markdown."""
        root = tmp_path / "claude"
        sess = root / "projects" / "proj_bad" / "sess_bad.jsonl"
        text = "always log every decision to vault/audit"
        _write_jsonl(sess, [_user("u01", text, "2026-10-08T10:00:00Z", "sess_bad")])
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        _scan(vault, conn, root, "sess_bad")
        planned = _first_batch(conn, "sess_bad")

        process_batch(
            batch_id=planned["batch"]["batch_id"], vault=vault, conn=conn,
            provider=_StubProvider(
                statement="user wants decisions logged", quote=text,
                scope={"level": "global"},
            ),
            reconciler=Reconciler(memory_projection_repo=MemoryProjectionRepo(conn)),
            committer=CommitService(vault=vault, conn=conn),
            evidence_lookup=lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault),
            base_commit_id="root", privacy_epoch="epoch-0", run_id="run_bad",
        )

        # Rewrite the committed memory's evidence quote so it no longer
        # matches the source slice (simulates a tampered/corrupt object).
        md_dir = vault.memories_dir
        md_files = list(md_dir.glob("*/r*.md"))
        assert md_files
        md = md_files[0]
        md.write_text(
            md.read_text(encoding="utf-8").replace(text, "TAMPERED QUOTE"),
            encoding="utf-8",
        )

        result = _recall(vault, conn, "decisions logged")
        assert result["items"] == []
        assert result["warnings"]


# ---------------------------------------------------------------------------
# 2. memory_projection must carry the committed scope
# ---------------------------------------------------------------------------


class TestProjectionScopeFilters:
    def test_projection_row_carries_committed_scope(self, tmp_path):
        vault, conn, event = _seeded_vault(tmp_path)
        _commit(vault, conn, _memory(
            memory_id="m_proj", statement="project scoped rule",
            scope=Scope(level="project", project_ids=["proj_a"]),
            domains=["dom_x"], evidence=_evidence_for_event(event),
        ))
        row = conn.execute(
            "SELECT * FROM memory_projection WHERE memory_id = 'm_proj'"
        ).fetchone()
        assert json.loads(row["scope_json"])["level"] == "project"
        assert json.loads(row["project_ids_json"]) == ["proj_a"]
        assert json.loads(row["domains_json"]) == ["dom_x"]

    def test_project_memory_returned_for_its_project_only(self, tmp_path):
        vault, conn, event = _seeded_vault(tmp_path)
        ev = _evidence_for_event(event)
        _commit(vault, conn, _memory(
            memory_id="m_pa", statement="use Python in project alpha",
            scope=Scope(level="project", project_ids=["proj_alpha"]),
            domains=[], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_pb", statement="use Rust in project beta",
            scope=Scope(level="project", project_ids=["proj_beta"]),
            domains=[], evidence=ev,
        ))

        alpha = _recall(vault, conn, "use project", projects=["proj_alpha"])
        ids = {i["memory_id"] for i in alpha["items"]}
        assert "m_pa" in ids
        assert "m_pb" not in ids

        beta = _recall(vault, conn, "use project", projects=["proj_beta"])
        ids = {i["memory_id"] for i in beta["items"]}
        assert "m_pb" in ids
        assert "m_pa" not in ids

    def test_global_memory_visible_under_any_project_filter(self, tmp_path):
        vault, conn, event = _seeded_vault(tmp_path)
        ev = _evidence_for_event(event)
        _commit(vault, conn, _memory(
            memory_id="m_g", statement="always log decisions globally",
            scope=Scope(level="global"), domains=[], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_p", statement="always log decisions in proj alpha",
            scope=Scope(level="project", project_ids=["proj_alpha"]),
            domains=[], evidence=ev,
        ))
        ids = {i["memory_id"]
               for i in _recall(vault, conn, "log decisions", projects=["proj_alpha"])["items"]}
        assert "m_g" in ids, "global memory must survive a project filter"
        assert "m_p" in ids

        ids_beta = {i["memory_id"]
                    for i in _recall(vault, conn, "log decisions", projects=["proj_beta"])["items"]}
        assert "m_g" in ids_beta
        assert "m_p" not in ids_beta

    def test_domain_filter_is_honoured_not_ignored(self, tmp_path):
        vault, conn, event = _seeded_vault(tmp_path)
        ev = _evidence_for_event(event)
        _commit(vault, conn, _memory(
            memory_id="m_dx", statement="testing rule for domain x",
            scope=Scope(level="domain", domain_ids=["dom_x"]), domains=["dom_x"],
            evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_dy", statement="testing rule for domain y",
            scope=Scope(level="domain", domain_ids=["dom_y"]), domains=["dom_y"],
            evidence=ev,
        ))
        ids_x = {i["memory_id"]
                 for i in _recall(vault, conn, "testing rule", domains=["dom_x"])["items"]}
        assert "m_dx" in ids_x
        assert "m_dy" not in ids_x
        ids_y = {i["memory_id"]
                 for i in _recall(vault, conn, "testing rule", domains=["dom_y"])["items"]}
        assert "m_dy" in ids_y
        assert "m_dx" not in ids_y


# ---------------------------------------------------------------------------
# 2b. recall filter boundary — unscoped query, invalid scope, combined facets
# ---------------------------------------------------------------------------


class TestRecallFilterBoundary:
    """Recall's projection filter must never silently broaden scope.

    Three P0 defects are pinned here, against
    ``src/memory_agent/application/recall.py``:

      1. An unscoped query (no project, no domain) must default to
         *global + domain* rules. Project / task / session rows are
         hidden — cross-project browsing must be explicit
         (docs/04 §"召回").
      2. A row whose ``scope_json.level`` is missing or unknown is
         excluded with a warning. An unknown projection is never
         widened to global.
      3. When the caller names both project and domain facets, both
         apply to the same memory. A project row with an explicit
         domain tag is only returned when *both* match.
    """

    def test_unscoped_query_hides_project_task_session_rows(self, tmp_path):
        """Default unscoped recall = global + domain. Project / task /
        session rows must not leak in."""
        vault, conn, event = _seeded_vault(tmp_path)
        ev = _evidence_for_event(event)
        _commit(vault, conn, _memory(
            memory_id="m_g", statement="global rule for unscoped recall",
            scope=Scope(level="global"), domains=[], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_p", statement="project rule must not leak into unscoped",
            scope=Scope(level="project", project_ids=["proj_a"]),
            domains=[], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_t", statement="task rule must not leak into unscoped",
            scope=Scope(level="task", task_ids=["t1"]),
            domains=[], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_s", statement="session rule must not leak into unscoped",
            scope=Scope(level="session"),
            domains=[], evidence=ev,
        ))

        result = _recall(vault, conn, "unscoped recall rule")
        ids = {i["memory_id"] for i in result["items"]}
        assert ids == {"m_g"}, (
            "unscoped recall must default to global only; "
            f"got {ids} with warnings={result['warnings']}"
        )

    def test_unscoped_query_keeps_global_and_domain_rules(self, tmp_path):
        """An unscoped query must keep both global and domain rules."""
        vault, conn, event = _seeded_vault(tmp_path)
        ev = _evidence_for_event(event)
        _commit(vault, conn, _memory(
            memory_id="m_g", statement="global rule",
            scope=Scope(level="global"), domains=[], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_dx", statement="domain rule for dom_x",
            scope=Scope(level="domain", domain_ids=["dom_x"]),
            domains=["dom_x"], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_dy", statement="domain rule for dom_y",
            scope=Scope(level="domain", domain_ids=["dom_y"]),
            domains=["dom_y"], evidence=ev,
        ))

        result = _recall(vault, conn, "rule for unscoped")
        ids = {i["memory_id"] for i in result["items"]}
        assert ids == {"m_g", "m_dx", "m_dy"}, (
            "unscoped recall must keep global + domain rules; "
            f"got {ids} with warnings={result['warnings']}"
        )

    def test_unknown_scope_level_row_excluded_not_widened(self, tmp_path):
        """A projection with an unknown level must not be silently
        widened to global. Recall omits the row and warns."""
        vault, conn, event = _seeded_vault(tmp_path)
        ev = _evidence_for_event(event)
        _commit(vault, conn, _memory(
            memory_id="m_g", statement="global rule",
            scope=Scope(level="global"), domains=[], evidence=ev,
        ))

        # Hand-tamper the projection to invent an unknown level.
        conn.execute(
            "UPDATE memory_projection SET scope_json = ? WHERE memory_id = ?",
            (json.dumps({"level": "workspace"}), "m_g"),
        )
        # And inject a second row whose scope_json is missing.
        _commit(vault, conn, _memory(
            memory_id="m_real_g", statement="real global rule",
            scope=Scope(level="global"), domains=[], evidence=ev,
        ))
        conn.execute(
            "UPDATE memory_projection SET scope_json = ? WHERE memory_id = ?",
            ("", "m_real_g"),
        )

        result = _recall(vault, conn, "rule")
        ids = {i["memory_id"] for i in result["items"]}
        assert "m_g" not in ids, (
            "an unknown scope_json.level must not be widened to global"
        )
        assert "m_real_g" not in ids, (
            "a missing scope_json.level must not be widened to global"
        )
        joined = "\n".join(result["warnings"])
        assert "invalid_scope_level:" in joined, result["warnings"]
        assert "m_g" in joined
        assert "m_real_g" in joined
    def test_project_row_with_explicit_domain_matches_both_facets(
        self, tmp_path
    ):
        """A project row tagged ``proj_a + dom_x`` is returned only when
        both facets match the query."""
        vault, conn, event = _seeded_vault(tmp_path)
        ev = _evidence_for_event(event)
        _commit(vault, conn, _memory(
            memory_id="m_px", statement="rule for proj_a and dom_x",
            scope=Scope(level="project", project_ids=["proj_a"],
                        domain_ids=["dom_x"]),
            domains=["dom_x"], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_py", statement="rule for proj_a and dom_y",
            scope=Scope(level="project", project_ids=["proj_a"],
                        domain_ids=["dom_y"]),
            domains=["dom_y"], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_g", statement="global rule",
            scope=Scope(level="global"), domains=[], evidence=ev,
        ))

        # Matching query: both facets hit the m_px row.
        ids = {i["memory_id"]
               for i in _recall(
                   vault, conn, "rule",
                   projects=["proj_a"], domains=["dom_x"],
               )["items"]}
        assert ids == {"m_px", "m_g"}, (
            "matching project + domain must include the row + global; "
            f"got {ids}"
        )

        # Wrong project, same domain → excluded.
        ids_b = {i["memory_id"]
                 for i in _recall(
                     vault, conn, "rule",
                     projects=["proj_b"], domains=["dom_x"],
                 )["items"]}
        assert "m_px" not in ids_b
        assert "m_py" not in ids_b
        assert "m_g" in ids_b, "global rows survive any filter combination"

        # Right project, wrong domain → excluded.
        ids_y = {i["memory_id"]
                 for i in _recall(
                     vault, conn, "rule",
                     projects=["proj_a"], domains=["dom_y"],
                 )["items"]}
        assert ids_y == {"m_py", "m_g"}, (
            f"expected only m_py + global; got {ids_y}"
        )

    def test_project_row_with_explicit_domain_excluded_when_domain_filter_absent(
        self, tmp_path
    ):
        """An explicit ``domain_ids`` tag on a project row must be
        satisfied by the caller's domain filter. A query that names
        the project but no domain is conservative — explicit facets
        are never loosened, so the row is excluded."""
        vault, conn, event = _seeded_vault(tmp_path)
        ev = _evidence_for_event(event)
        _commit(vault, conn, _memory(
            memory_id="m_px", statement="proj_a rule limited to dom_x",
            scope=Scope(level="project", project_ids=["proj_a"],
                        domain_ids=["dom_x"]),
            domains=["dom_x"], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_p", statement="proj_a rule with no domain limit",
            scope=Scope(level="project", project_ids=["proj_a"]),
            domains=[], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_g", statement="global rule",
            scope=Scope(level="global"), domains=[], evidence=ev,
        ))

        # Project-only filter: explicit domain tag is unsatisfied
        # and the row is excluded; the domain-less project row is
        # included because it has no explicit domain facet.
        ids = {i["memory_id"]
               for i in _recall(
                   vault, conn, "rule", projects=["proj_a"],
               )["items"]}
        assert "m_p" in ids, "project row without explicit domain matches project filter"
        assert "m_px" not in ids, (
            "explicit domain tag must not be loosened by an open query"
        )
        assert "m_g" in ids

    def test_task_and_session_rows_hidden_under_project_or_domain_filter(
        self, tmp_path
    ):
        """Task / session rows cannot answer 'which project / domain?',
        so they are excluded under any project or domain filter (and
        under no filter at all)."""
        vault, conn, event = _seeded_vault(tmp_path)
        ev = _evidence_for_event(event)
        _commit(vault, conn, _memory(
            memory_id="m_t", statement="task rule",
            scope=Scope(level="task", task_ids=["t1"]),
            domains=[], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_s", statement="session rule",
            scope=Scope(level="session"),
            domains=[], evidence=ev,
        ))
        _commit(vault, conn, _memory(
            memory_id="m_g", statement="global rule",
            scope=Scope(level="global"), domains=[], evidence=ev,
        ))

        # Project filter alone.
        ids_p = {i["memory_id"]
                 for i in _recall(
                     vault, conn, "rule", projects=["proj_a"],
                 )["items"]}
        assert ids_p == {"m_g"}, (
            "task/session rows must not survive a project filter; "
            f"got {ids_p}"
        )

        # Domain filter alone.
        ids_d = {i["memory_id"]
                 for i in _recall(
                     vault, conn, "rule", domains=["dom_x"],
                 )["items"]}
        assert "m_t" not in ids_d and "m_s" not in ids_d

        # Project + domain filter.
        ids_pd = {i["memory_id"]
                  for i in _recall(
                      vault, conn, "rule",
                      projects=["proj_a"], domains=["dom_x"],
                  )["items"]}
        assert "m_t" not in ids_pd and "m_s" not in ids_pd


# ---------------------------------------------------------------------------
# 3. scope normalisation must fail closed
# ---------------------------------------------------------------------------


class TestScopeNormalisation:
    @pytest.mark.parametrize("raw", [None, {}, [], 7, ""])
    def test_missing_or_nonsense_scope_rejected(self, raw):
        with pytest.raises(ValueError):
            _normalise_scope(raw)

    def test_explicit_global_accepted_in_both_shapes(self):
        assert _normalise_scope("global").level == "global"
        assert _normalise_scope({"level": "global"}).level == "global"
        assert _normalise_scope({"level": "global", "project_ids": []}).level == "global"

    @pytest.mark.parametrize("level", ["project", "domain", "task"])
    def test_bare_level_string_without_ids_rejected(self, level):
        """A bare ``"project"`` names a scope but no project — it is not
        sufficiently scoped, and must not become global."""
        with pytest.raises(ValueError):
            _normalise_scope(level)

    @pytest.mark.parametrize("level,field", [
        ("project", "project_ids"),
        ("domain", "domain_ids"),
        ("task", "task_ids"),
    ])
    def test_object_scope_missing_its_ids_rejected(self, level, field):
        with pytest.raises(ValueError):
            _normalise_scope({"level": level})

    @pytest.mark.parametrize("raw", [
        {"level": "global", "project_ids": ["proj_a"]},
        {"level": "global", "domain_ids": ["dom_a"]},
        {"level": "project", "project_ids": ["p"], "domain_ids": ["d"]},
        {"level": "domain", "domain_ids": ["d"], "task_ids": ["t"]},
    ])
    def test_inconsistent_level_and_ids_rejected(self, raw):
        with pytest.raises(ValueError):
            _normalise_scope(raw)

    def test_valid_scoped_objects_accepted(self):
        s = _normalise_scope({"level": "project", "project_ids": ["proj_a"]})
        assert s.level == "project" and s.project_ids == ["proj_a"]
        s = _normalise_scope({"level": "domain", "domain_ids": ["dom_a"]})
        assert s.domain_ids == ["dom_a"]

    def test_unknown_level_string_rejected(self):
        with pytest.raises(ValueError):
            _normalise_scope({"level": "workspace"})

    def test_missing_scope_fails_the_batch_closed(self, tmp_path):
        """A memory-eligible candidate with no scope must not be
        committed; the batch fails instead."""
        root = tmp_path / "claude"
        sess = root / "projects" / "proj_nosc" / "sess_nosc.jsonl"
        text = "always log decisions"
        _write_jsonl(sess, [_user("u01", text, "2026-10-08T10:00:00Z", "sess_nosc")])
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        _scan(vault, conn, root, "sess_nosc")
        planned = _first_batch(conn, "sess_nosc")
        batch_id = planned["batch"]["batch_id"]

        class _NoScopeProvider(_StubProvider):
            def run_extract(self, task):
                out = super().run_extract(task)
                del out["candidates"][0]["scope"]
                return out

        with pytest.raises(BatchProcessingError):
            process_batch(
                batch_id=batch_id, vault=vault, conn=conn,
                provider=_NoScopeProvider(statement="no scope", quote=text,
                                          scope={"level": "global"}),
                reconciler=Reconciler(memory_projection_repo=MemoryProjectionRepo(conn)),
                committer=CommitService(vault=vault, conn=conn),
                evidence_lookup=lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault),
                base_commit_id="root", privacy_epoch="epoch-0", run_id="run_ns",
                max_attempts=1,
            )
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM memory_projection"
        ).fetchone()
        assert rows["c"] == 0, "no memory may be committed from an unscoped candidate"


# ---------------------------------------------------------------------------
# 4. same-batch contradiction: both pending, neither published
# ---------------------------------------------------------------------------


def _candidate(cid: str, statement: str, *, scope: Scope | None = None) -> dict:
    return {
        "candidate_id": cid, "kind": "requirement", "abstraction": "no_opinion",
        "statement": statement, "modality": "must",
        "scope": scope or Scope(level="global"),
        "tags": [], "evidence": [],
    }


class TestSameBatchContradiction:
    def test_reconciler_puts_both_sides_pending(self):
        cs = _candidate_set_from_payload(
            payload=[
                _candidate("cand_a", "use Python for the prototype"),
                _candidate("cand_b", "use TypeScript for the prototype"),
            ],
            batch_id="b_x", prompt_version="p", model="m", raw_payload="{}",
        )
        out = Reconciler(memory_projection_repo=_EmptyRepo()).reconcile(
            cs, evidence_lookup=lambda e, h: "")
        assert out.to_publish == [], "neither side of a same-batch contradiction may publish"
        pending_ids = {p["candidate_id"] for p in out.pending}
        assert pending_ids == {"cand_a", "cand_b"}
        assert all(p["reason"] == "same_batch_contradiction" for p in out.pending)

    def test_non_overlapping_scopes_still_both_publish(self):
        cs = _candidate_set_from_payload(
            payload=[
                _candidate("cand_a", "use Python here",
                           scope=Scope(level="project", project_ids=["proj_a"])),
                _candidate("cand_b", "use TypeScript there",
                           scope=Scope(level="project", project_ids=["proj_b"])),
            ],
            batch_id="b_x", prompt_version="p", model="m", raw_payload="{}",
        )
        out = Reconciler(memory_projection_repo=_EmptyRepo()).reconcile(
            cs, evidence_lookup=lambda e, h: "")
        assert len(out.to_publish) == 2
        assert out.pending == []

    def test_process_batch_commits_no_active_memory_for_contradiction(
        self, tmp_path
    ):
        root = tmp_path / "claude"
        sess = root / "projects" / "proj_contra" / "sess_contra.jsonl"
        text = "always use Python for the prototype and never TypeScript"
        _write_jsonl(sess, [_user("u01", text, "2026-10-08T10:00:00Z", "sess_contra")])
        vault = VaultLayout(tmp_path / "vault")
        conn = open_state_db(vault.state_db)
        _scan(vault, conn, root, "sess_contra")
        planned = _first_batch(conn, "sess_contra")
        event = next(e for e in _events_for(conn, "sess_contra"))

        class _ContradictoryProvider:
            name = "contra"

            def run_extract(self, task):
                idx = text.index("use Python")
                idx2 = text.index("never TypeScript")
                return {
                    "status": "ok", "prompt_version": "p", "model": "contra",
                    "candidates": [
                        {
                            "candidate_id": "cand_py", "kind": "requirement",
                            "abstraction": "no_opinion",
                            "statement": "use Python for the prototype",
                            "modality": "must", "scope": {"level": "global"},
                            "tags": [],
                            "evidence": [{
                                "source_event_id": event.event_id,
                                "source_revision_hash": event.revision_hash,
                                "role": "user", "origin": "user_session",
                                "quote": text[idx:idx + len("use Python")],
                                "span_start": idx, "span_end": idx + len("use Python"),
                            }],
                        },
                        {
                            "candidate_id": "cand_ts", "kind": "requirement",
                            "abstraction": "no_opinion",
                            "statement": "never use TypeScript for the prototype",
                            "modality": "must", "scope": {"level": "global"},
                            "tags": [],
                            "evidence": [{
                                "source_event_id": event.event_id,
                                "source_revision_hash": event.revision_hash,
                                "role": "user", "origin": "user_session",
                                "quote": text[idx2:idx2 + len("never TypeScript")],
                                "span_start": idx2,
                                "span_end": idx2 + len("never TypeScript"),
                            }],
                        },
                    ],
                }

        out = process_batch(
            batch_id=planned["batch"]["batch_id"], vault=vault, conn=conn,
            provider=_ContradictoryProvider(),
            reconciler=Reconciler(memory_projection_repo=MemoryProjectionRepo(conn)),
            committer=CommitService(vault=vault, conn=conn),
            evidence_lookup=lambda eid, rh: _evidence_text_lookup(conn, eid, rh, vault),
            base_commit_id="root", privacy_epoch="epoch-0", run_id="run_contra",
        )
        assert out["to_publish"] == 0, out
        assert out["pending"] == 2, out
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM memory_projection WHERE status='active'"
        ).fetchone()["c"] == 0
        conflicts = conn.execute(
            "SELECT conflict_id FROM conflict_projection"
        ).fetchall()
        assert {c["conflict_id"] for c in conflicts} == {"cand_py", "cand_ts"}


class _EmptyRepo:
    def list_active(self, project_ids=None):
        return []


# ---------------------------------------------------------------------------
# 5. pyproject must not advertise a remote that does not exist
# ---------------------------------------------------------------------------


def test_pyproject_advertises_no_unverifiable_urls():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.urls]" not in text
    assert "github.com/hungrytingus/memory-agent" not in text
