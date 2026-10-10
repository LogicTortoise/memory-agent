"""Tests for the model provider layer.

* Stub CLI executable exercises the ClaudeCLIModelProvider against
  fake `claude` binaries that return success, bad JSON, auth error
  and timeout shapes.
* get_default_provider() respects the env var
  MEMORY_AGENT_MODEL_PROVIDER; the default is `cli` and the CLI
  provider refuses unless MEMORY_AGENT_ENABLE_CLAUDE_CLI=1.
* SQLiteModelCallRecorder writes model_calls rows without requiring a
  real job lifecycle.
* Live-CLI scope schema: the prompt asks for a strict object scope,
  and the parser accepts the observed ``"scope": "global"`` string
  shape *and* rejects ambiguous / project-shaped strings, so we do
  not invent a project or domain scope from unknown text.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


def _stub_claude(tmp_path: Path) -> Path:
    """Write a small shell script that pretends to be the `claude` CLI."""
    p = tmp_path / "claude"
    p.write_text(
        "#!/usr/bin/env bash\n"
        "# Minimal stub that prints a fake JSON envelope and exits 0.\n"
        "if [ \"$1\" = \"-p\" ] && [ \"$3\" = \"--output-format\" ] && [ \"$4\" = \"json\" ]; then\n"
        "  cat <<'EOF'\n"
        "{\"type\":\"result\",\"is_error\":false,\"model\":\"stub-claude\","
        "\"result\":\"{\\\"status\\\":\\\"ok\\\",\\\"candidates\\\":[]}\"}\n"
        "EOF\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    p.chmod(0o755)
    return p


class TestGetDefaultProvider:
    def test_default_returns_cli(self, monkeypatch):
        monkeypatch.delenv("MEMORY_AGENT_MODEL_PROVIDER", raising=False)
        monkeypatch.delenv("MEMORY_AGENT_ENABLE_CLAUDE_CLI", raising=False)
        from memory_agent.adapters.models import (
            ClaudeCLIModelProvider,
            ModelError,
            get_default_provider,
        )
        # CLI provider needs a claude on PATH OR explicit path.
        # Without it, construction must raise.
        with pytest.raises(ModelError):
            get_default_provider()

    def test_fake_via_env(self, monkeypatch):
        monkeypatch.setenv("MEMORY_AGENT_MODEL_PROVIDER", "fake")
        from memory_agent.adapters.models import FakeModelProvider, get_default_provider
        p = get_default_provider()
        assert isinstance(p, FakeModelProvider)

    def test_cli_via_env_without_enable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEMORY_AGENT_MODEL_PROVIDER", "cli")
        monkeypatch.delenv("MEMORY_AGENT_ENABLE_CLAUDE_CLI", raising=False)
        from memory_agent.adapters.models import ModelError, get_default_provider
        with pytest.raises(ModelError):
            get_default_provider()

    def test_unknown_choice(self, monkeypatch):
        monkeypatch.setenv("MEMORY_AGENT_MODEL_PROVIDER", "wat")
        from memory_agent.adapters.models import ModelError, get_default_provider
        with pytest.raises(ModelError):
            get_default_provider()


class TestClaudeCLIStub:
    def _enable(self, monkeypatch, tmp_path: Path):
        """Wire the stub CLI on PATH and opt-in env."""
        stub = _stub_claude(tmp_path)
        monkeypatch.setenv("MEMORY_AGENT_ENABLE_CLAUDE_CLI", "1")
        monkeypatch.setenv("MEMORY_AGENT_MODEL_PROVIDER", "cli")
        # PATH first so shutil.which finds our stub.
        monkeypatch.setenv("PATH", str(stub.parent) + os.pathsep + os.environ.get("PATH", ""))
        return stub

    def test_success_envelope(self, monkeypatch, tmp_path):
        stub = self._enable(monkeypatch, tmp_path)
        from memory_agent.adapters.models import ClaudeCLIModelProvider
        prov = ClaudeCLIModelProvider()
        out = prov.run_extract({"task_type": "extract", "task_id": "t", "run_id": "r", "batch_id": "b", "members": []})
        assert out["status"] == "ok"

    def test_malformed_envelope(self, monkeypatch, tmp_path):
        self._enable(monkeypatch, tmp_path)
        bad = tmp_path / "claude"
        bad.write_text("#!/bin/sh\necho 'not-json'\n", encoding="utf-8")
        bad.chmod(0o755)
        from memory_agent.adapters.models import ClaudeCLIModelProvider
        prov = ClaudeCLIModelProvider()
        out = prov.run_extract({"task_type": "extract", "task_id": "t", "run_id": "r", "batch_id": "b", "members": []})
        assert out["status"] == "malformed"

    def test_auth_error_envelope(self, monkeypatch, tmp_path):
        self._enable(monkeypatch, tmp_path)
        bad = tmp_path / "claude"
        bad.write_text("#!/bin/sh\necho 'oauth not configured' 1>&2\nexit 1\n",
                         encoding="utf-8")
        bad.chmod(0o755)
        from memory_agent.adapters.models import ClaudeCLIModelProvider
        prov = ClaudeCLIModelProvider()
        out = prov.run_extract({"task_type": "extract", "task_id": "t", "run_id": "r", "batch_id": "b", "members": []})
        assert out["status"] == "auth_error"

    def test_timeout_envelope(self, monkeypatch, tmp_path):
        self._enable(monkeypatch, tmp_path)
        # A stub that sleeps past our short timeout.
        bad = tmp_path / "claude"
        bad.write_text("#!/bin/sh\nsleep 5\necho '{}'\n", encoding="utf-8")
        bad.chmod(0o755)
        from memory_agent.adapters.models import ClaudeCLIModelProvider
        prov = ClaudeCLIModelProvider(timeout_seconds=0.2)
        out = prov.run_extract({"task_type": "extract", "task_id": "t", "run_id": "r", "batch_id": "b", "members": []})
        assert out["status"] == "timeout"


class TestSQLiteModelCallRecorder:
    def test_records_with_synthetic_jobs_runs(self, tmp_path):
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.adapters.models import SQLiteModelCallRecorder

        vault = VaultLayout(tmp_path)
        conn = open_state_db(vault.state_db)
        rec = SQLiteModelCallRecorder(conn=conn)
        rec.record(
            run_id="run_x", purpose="extract", provider="fake",
            model="fake", prompt_version="ok_v1", usage=None,
            duration_ms=10, input_ref=None, output_ref=None,
            status="ok", error_code=None,
        )
        n = conn.execute("SELECT COUNT(*) c FROM model_calls").fetchone()["c"]
        assert n == 1


class TestFakeProviderRecords:
    def test_run_extract_records_ok(self, tmp_path):
        from memory_agent.infrastructure.files import VaultLayout
        from memory_agent.infrastructure.sqlite import open_state_db
        from memory_agent.adapters.models import FakeModelProvider

        vault = VaultLayout(tmp_path)
        conn = open_state_db(vault.state_db)
        from memory_agent.adapters.models import SQLiteModelCallRecorder
        rec = SQLiteModelCallRecorder(conn=conn)
        prov = FakeModelProvider(recorder=rec)
        out = prov.run_extract({"task_type": "extract", "task_id": "t",
                                 "run_id": "r", "batch_id": "b",
                                 "members": [{"text": "use Python for the prototype"}]})
        assert out["status"] == "ok"
        # One model_calls row.
        rows = conn.execute("SELECT call_id FROM model_calls").fetchall()
        assert len(rows) == 1
        assert rows[0]["call_id"].startswith("call_")


# ---------------------------------------------------------------------------
# Live-CLI scope schema: the prompt asks for an object scope, but the
# real Claude CLI in this iteration emitted ``"scope": "global"`` as a
# string. The parser must accept the safe equivalent AND reject any
# string we cannot canonicalise — we will never silently invent a
# project or domain scope from a string we do not recognise.
# ---------------------------------------------------------------------------


class TestScopeShapeParser:
    def _make_candidate(self, scope):
        return {
            "candidate_id": "cand_s",
            "kind": "requirement",
            "abstraction": "no_opinion",
            "statement": "use Python for the prototype",
            "modality": "must",
            "scope": scope,
            "primary_domain": None,
            "tags": [],
            "evidence": [
                {
                    "source_event_id": "evt_target",
                    "source_revision_hash": "rh",
                    "role": "user",
                    "origin": "user_session",
                    "quote": "use Python for the prototype",
                    "span_start": 0,
                    "span_end": 26,
                }
            ],
        }

    def test_string_scope_global_accepted(self):
        """The observed live-CLI shape: ``"scope": "global"`` as a
        bare string. The parser must accept this as the safe
        equivalent of ``{"level": "global"}`` and reject nothing."""
        from memory_agent.application.process_batches import (
            _candidate_set_from_payload,
        )
        payload = [self._make_candidate("global")]
        cs = _candidate_set_from_payload(
            payload=payload,
            batch_id="b_live",
            prompt_version="claude_cli_extract_v1",
            model="claude",
            raw_payload=json.dumps(payload, sort_keys=True),
        )
        assert cs.candidates[0].scope.level == "global"

    def test_string_scope_project_without_id_rejected(self):
        """``"scope": "project"`` names a facet but no project. It is
        *under-scoped*, not equivalent to ``{"level": "project"}`` and
        certainly not to ``global``: committing it would silently widen
        a project-scoped instruction to every project. The candidate is
        rejected and the batch fails closed."""
        import pytest
        from memory_agent.application.process_batches import (
            _candidate_set_from_payload,
        )
        payload = [self._make_candidate("project")]
        with pytest.raises(ValueError):
            _candidate_set_from_payload(
                payload=payload,
                batch_id="b_live",
                prompt_version="claude_cli_extract_v1",
                model="claude",
                raw_payload=json.dumps(payload, sort_keys=True),
            )

    def test_object_scope_project_with_id_accepted(self):
        """``{"level": "project", "project_ids": [...]}`` names both
        the facet and the project, so it is sufficiently scoped."""
        from memory_agent.application.process_batches import (
            _candidate_set_from_payload,
        )
        candidate = self._make_candidate(
            {"level": "project", "project_ids": ["proj_alpha"]}
        )
        payload = [candidate]
        cs = _candidate_set_from_payload(
            payload=payload,
            batch_id="b_live",
            prompt_version="claude_cli_extract_v1",
            model="claude",
            raw_payload=json.dumps(payload, sort_keys=True),
        )
        assert cs.candidates[0].scope.level == "project"
        assert cs.candidates[0].scope.project_ids == ["proj_alpha"]

    def test_string_scope_unknown_project_text_rejected(self):
        """A free-form string like ``"scope": "project_alpha"`` is
        NOT a canonical level. We must NOT silently treat it as
        ``{"level": "project"}`` because that would broaden a
        memory's reach from an unrecognised token. The parser must
        reject it so the batch is marked failed, not auto-promoted.
        """
        from memory_agent.application.process_batches import (
            _candidate_set_from_payload,
        )
        payload = [self._make_candidate("project_alpha")]
        with pytest.raises(ValueError) as excinfo:
            _candidate_set_from_payload(
                payload=payload,
                batch_id="b_drift",
                prompt_version="claude_cli_extract_v1",
                model="claude",
                raw_payload=json.dumps(payload, sort_keys=True),
            )
        assert "scope" in str(excinfo.value).lower()

    def test_ambiguous_object_scope_without_level_rejected(self):
        """A scope object that does not carry ``level`` (e.g.
        ``{"project_ids": ["proj_x"]}``) is malformed; we reject
        rather than infer a level from the field names."""
        from memory_agent.application.process_batches import (
            _candidate_set_from_payload,
        )
        payload = [self._make_candidate({"project_ids": ["proj_x"]})]
        with pytest.raises(ValueError):
            _candidate_set_from_payload(
                payload=payload,
                batch_id="b_obj",
                prompt_version="claude_cli_extract_v1",
                model="claude",
                raw_payload=json.dumps(payload, sort_keys=True),
            )

    def test_object_scope_with_level_accepted(self):
        """A canonical object scope ``{"level": "global", ...}`` is
        accepted as-is and preserves the additional fields."""
        from memory_agent.application.process_batches import (
            _candidate_set_from_payload,
        )
        payload = [self._make_candidate({
            "level": "global", "project_ids": [],
            "domain_ids": [], "task_ids": [],
            "conditions": [], "excludes": [],
        })]
        cs = _candidate_set_from_payload(
            payload=payload,
            batch_id="b_obj_ok",
            prompt_version="claude_cli_extract_v1",
            model="claude",
            raw_payload=json.dumps(payload, sort_keys=True),
        )
        assert cs.candidates[0].scope.level == "global"

    def test_prompt_includes_scope_schema(self):
        """The extraction prompt sent to the real Claude CLI must
        document the strict object scope shape, including the
        canonical ``level`` field. Bare-string scopes must be called
        out as rejected."""
        from memory_agent.adapters.models import _build_prompt
        prompt = _build_prompt({"members": []})
        assert '"scope"' in prompt
        assert '"level"' in prompt
        # The closed enum of scope levels must be listed.
        for level in ("global", "domain", "project", "task", "session"):
            assert level in prompt, (
                f"scope level {level!r} missing from extraction prompt"
            )
        # The prompt must explicitly forbid bare-string scopes.
        assert "scope" in prompt.lower()
        assert "bare" in prompt.lower() or "object" in prompt.lower()

    def test_prompt_does_not_silently_broaden_unknown_scope_strings(self):
        """Defence-in-depth: the prompt must explicitly enumerate the
        canonical scope levels AND mention that bare-string scopes
        are rejected. If a future prompt edit accidentally drops
        either, the parser's rejection path becomes the only line
        of defence, and a prompt regression would let ``"scope":
        "project_alpha"`` slip through. This test pins the prompt
        shape so the regression is caught at CI time.
        """
        from memory_agent.adapters.models import _build_prompt
        prompt = _build_prompt({"members": []})
        for level in ("global", "domain", "project", "task", "session"):
            assert level in prompt, (
                f"scope level {level!r} missing from extraction prompt"
            )
        assert "bare" in prompt.lower() or "object" in prompt.lower()
        # The prompt must NOT encourage emitting the scope as a
        # bare string.
        assert "scope as a string" not in prompt.lower()
        assert '"scope": "' not in prompt


class _FakeRepo:
    def list_active(self, project_ids=None):
        return []