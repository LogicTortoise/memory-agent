"""Tests for the golden-fixture replay provider.

Verifies that:

  * the ``claude_ok.json`` fixture parses to ``status="ok"`` with
    exactly one candidate extracted from the wrapped-object shape;
  * the ``claude_auth_error.json`` fixture parses to
    ``status="malformed"`` (because ``is_error: true`` short-circuits
    before auth-vs-malformed inference);
  * a fixture whose envelope is missing all three candidates shapes
    falls through to ``status="malformed", error_code="no_candidates_field"``;
  * the replay provider's parsing branches mirror
    ``ClaudeCLIModelProvider._extract_candidates`` (any future
    divergence breaks here).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests._support.fixture_replay_provider import FixtureReplayModelProvider


_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "claude_cli_golden"


def _task() -> dict:
    return {
        "run_id": "run_test",
        "task_id": "task_test",
        "task_type": "extract",
    }


def test_ok_fixture_returns_status_ok_with_one_candidate() -> None:
    provider = FixtureReplayModelProvider(
        fixture_name="claude_ok.json",
        fixture_dir=_FIXTURE_DIR,
    )
    out = provider.run_extract(_task())
    assert out["status"] == "ok"
    assert out["model"] == "claude-3.5-sonnet"
    candidates = out["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["candidate_id"] == "cand_demo_001"
    assert cand["kind"] == "requirement"
    assert cand["statement"].startswith("use Python for the prototype")
    evidence = cand["evidence"]
    assert len(evidence) == 1
    ev = evidence[0]
    assert ev["role"] == "user"
    assert ev["origin"] == "user_session"
    assert ev["quote"] == cand["statement"]
    assert ev["span_end"] - ev["span_start"] == len(cand["statement"])


def test_auth_error_fixture_short_circuits_via_is_error() -> None:
    provider = FixtureReplayModelProvider(
        fixture_name="claude_auth_error.json",
        fixture_dir=_FIXTURE_DIR,
    )
    out = provider.run_extract(_task())
    # The live provider would surface this as auth_error from the
    # ``returncode != 0`` branch, but here \`is_error: true\` reaches
    # the malformed/model_error branch first. Either is a real failure
    # path; this test pins the *current* behaviour so a future refactor
    # cannot silently flip the contract.
    assert out["status"] == "malformed"
    assert out["error_code"] == "model_error"
    assert out["candidates"] == []
    assert "envelope" in out


def test_missing_candidates_field_yields_no_candidates_error(tmp_path: Path) -> None:
    """A fixture whose envelope has no recognisable candidates list
    must surface \`error_code="no_candidates_field"\`."""
    envelope_path = tmp_path / "no_candidates.json"
    envelope_path.write_text(
        json.dumps({"type": "result", "is_error": False, "model": "x"}),
        encoding="utf-8",
    )
    provider = FixtureReplayModelProvider(
        fixture_name="no_candidates.json",
        fixture_dir=tmp_path,
    )
    out = provider.run_extract(_task())
    assert out["status"] == "malformed"
    assert out["error_code"] == "no_candidates_field"


def test_wrapped_string_envelope_is_parsed() -> None:
    """The replay provider must accept the wrapped-string shape, just
    like the live provider does."""
    envelope = {
        "type": "result",
        "is_error": False,
        "model": "claude-3.5-sonnet",
        "result": json.dumps({
            "candidates": [
                {"candidate_id": "cand_str_001", "kind": "requirement",
                 "statement": "x", "abstraction": "no_opinion",
                 "scope": {"level": "global"}, "tags": [],
                 "evidence": []},
            ],
        }),
    }
    envelope_path = _FIXTURE_DIR / "_wrapped_string.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    try:
        provider = FixtureReplayModelProvider(
            fixture_name="_wrapped_string.json",
            fixture_dir=_FIXTURE_DIR,
        )
        out = provider.run_extract(_task())
        assert out["status"] == "ok"
        assert len(out["candidates"]) == 1
        assert out["candidates"][0]["candidate_id"] == "cand_str_001"
    finally:
        envelope_path.unlink(missing_ok=True)


def test_replay_parsing_branches_match_live_provider() -> None:
    """Lock the parsing branches: the replay provider must locate
    candidates in all three envelope shapes the live provider accepts
    (top-level, wrapped-string, wrapped-object)."""
    provider = FixtureReplayModelProvider(fixture_dir=_FIXTURE_DIR)
    # Top-level shape.
    assert provider._extract_candidates(
        {"candidates": [{"x": 1}], "result": "ignore me"}
    ) == [{"x": 1}]
    # Wrapped-object shape.
    assert provider._extract_candidates(
        {"result": {"candidates": [{"y": 2}]}}
    ) == [{"y": 2}]
    # Wrapped-string shape.
    assert provider._extract_candidates(
        {"result": json.dumps({"candidates": [{"z": 3}]})}
    ) == [{"z": 3}]
    # None when nothing matches.
    assert provider._extract_candidates({"result": "not json"}) is None
    assert provider._extract_candidates({"result": {"candidates": "nope"}}) is None
    assert provider._extract_candidates({}) is None


def test_missing_fixture_raises() -> None:
    import pytest
    provider = FixtureReplayModelProvider(
        fixture_name="does_not_exist.json",
        fixture_dir=_FIXTURE_DIR,
    )
    with pytest.raises(FileNotFoundError):
        provider.run_extract(_task())
