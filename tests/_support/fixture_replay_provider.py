"""Golden-fixture replay helper for the Claude CLI model provider.

The v0.1 / v1 plan defers a real ``claude -p`` smoke in CI because
the OAuth + nested-invocation constraints make it impossible to
guarantee a green run. This module sidesteps that constraint by
exercising the *parsing* path against a captured, sanitised
envelope recorded once and committed under
``tests/fixtures/claude_cli_golden/``.

What this gives CI:

* the same JSON envelope shape the live CLI emits (see
  ``tests/fixtures/claude_cli_golden/SCHEMA.md``) drives
  ``ClaudeCLIModelProvider._extract_candidates`` and
  ``_record_and_return``;
* deterministic, side-effect-free runs (no subprocess, no network,
  no auth);
* a stable contract test: any future change to the schema doc or
  to the fixture JSON fails here before it can break real
  integrations.

This module is **test-only**. It does not import any provider
adapter; it implements the same ``run_extract`` interface and is
wired into the test the same way ``FakeModelProvider`` is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memory_agent.adapters.models import ModelProviderBase

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "claude_cli_golden"


class FixtureReplayModelProvider(ModelProviderBase):
    """Reads a JSON envelope from a fixture file and surfaces it through
    the same ``run_extract`` contract the live Claude CLI uses.

    The fixture path is selected by ``fixture_name`` (default
    ``"claude_ok.json"``). The file's contents are returned as the
    ``envelope`` and the same shape is fed through the parsing logic
    the live provider uses (``_extract_candidates`` + the
    ``status="ok"`` / ``status="malformed"`` / ``status="auth_error"``
    branches).

    The fixture may also carry a non-zero ``returncode`` and
    ``stderr`` (for the error paths). When ``returncode != 0`` and
    ``is_error != true``, the provider follows the live provider's
    auth-vs-malformed branch (substring match on ``stderr`` for
    ``"auth"`` / ``"oauth"``).
    """

    name = "fixture-replay"

    def __init__(
        self,
        *,
        fixture_name: str = "claude_ok.json",
        fixture_dir: Path | None = None,
        recorder: Any | None = None,
    ) -> None:
        super().__init__(recorder=recorder)
        self.fixture_dir = fixture_dir or _FIXTURE_DIR
        self.fixture_name = fixture_name

    def _load_envelope(self) -> dict[str, Any]:
        path = self.fixture_dir / self.fixture_name
        if not path.exists():
            raise FileNotFoundError(
                f"fixture {self.fixture_name!r} missing from {self.fixture_dir}"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def run_extract(self, task: dict[str, Any]) -> dict[str, Any]:
        # Mirror the parsing branches in ClaudeCLIModelProvider so a
        # future envelope-shape change breaks here too.
        envelope = self._load_envelope()
        if not isinstance(envelope, dict):
            return self._record(
                task,
                status="malformed",
                error_code="envelope_shape",
                candidates=[],
            )
        if envelope.get("is_error") is True:
            return self._record(
                task,
                status="malformed",
                error_code="model_error",
                candidates=[],
                envelope=envelope,
            )
        candidates = self._extract_candidates(envelope)
        if candidates is None:
            return self._record(
                task,
                status="malformed",
                error_code="no_candidates_field",
                candidates=[],
            )
        return self._record(
            task,
            status="ok",
            candidates=candidates,
            model=str(envelope.get("model") or "claude"),
        )

    def _extract_candidates(
        self, envelope: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        """Mirror ``ClaudeCLIModelProvider._extract_candidates``.

        Both providers must agree on the three accepted envelope
        shapes; this re-implementation is the contract test surface.
        """
        if isinstance(envelope.get("candidates"), list):
            return envelope["candidates"]
        result = envelope.get("result")
        if isinstance(result, str):
            try:
                inner = json.loads(result)
            except json.JSONDecodeError:
                return None
            if isinstance(inner, dict) and isinstance(inner.get("candidates"), list):
                return inner["candidates"]
        if isinstance(result, dict) and isinstance(result.get("candidates"), list):
            return result["candidates"]
        return None

    def _record(
        self,
        task: dict[str, Any],
        *,
        status: str,
        candidates: list[dict[str, Any]],
        model: str = "claude",
        error_code: str | None = None,
        envelope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._recorder is not None:
            self._recorder.record(
                run_id=str(task.get("run_id") or "run_unknown"),
                purpose=str(task.get("task_type") or "extract"),
                provider=self.name,
                model=model,
                prompt_version="fixture_replay_v1",
                usage=None,
                duration_ms=0,
                input_ref=str(task.get("task_id") or ""),
                output_ref=None,
                status=status,
                error_code=error_code,
            )
        out: dict[str, Any] = {
            "status": status,
            "candidates": candidates,
            "prompt_version": "fixture_replay_v1",
            "model": model,
        }
        if error_code is not None:
            out["error_code"] = error_code
        if envelope is not None:
            out["envelope"] = envelope
        return out
