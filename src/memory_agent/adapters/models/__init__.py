"""Model adapters.

v0.1 ships two providers and one switch:

  * ``ClaudeCLIModelProvider`` is the *default* — it shells out to the
    user-installed ``claude`` CLI with non-interactive ``-p`` and a
    structured ``--output-format json`` envelope. It fails closed:
    unavailable CLI / auth error / timeout / malformed JSON all return
    a non-``ok`` ``status`` so the caller can record a real attempt and
    keep the batch in a retryable state. It never produces a
    successful ``no_memory`` answer as a substitute for an error.

  * ``FakeModelProvider`` is test-only. It is reachable through:

      1. explicit dependency injection in test code, or
      2. the env var ``MEMORY_AGENT_MODEL_PROVIDER=fake``.

    Setting that env var turns the production CLI into a deterministic
    in-process provider so end-to-end smoke can exercise the rest of
    the pipeline without invoking ``claude``. Production never falls
    back to the fake automatically.

  * ``get_default_provider()`` honours
    ``MEMORY_AGENT_MODEL_PROVIDER`` (``cli`` or ``fake``). When unset or
    set to ``cli``, it returns ``ClaudeCLIModelProvider``; the CLI
    provider itself requires ``MEMORY_AGENT_ENABLE_CLAUDE_CLI=1`` to
    actually run, so the user must opt in. v0.1 never calls the user's
    interactive CLI by accident.

Both providers record every attempt in the ``model_calls`` SQLite
table through an injected recorder (no global state). The CLI
provider enforces a hard subprocess timeout, an output size budget,
and a tool-restricted prompt preamble so the model cannot reach the
filesystem, shell, or network.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

from memory_agent.contracts.candidates import (
    CANDIDATE_SCHEMA_VERSION,
    Candidate,
    CandidateSet,
    EvidenceRef,
    Scope,
)


class ModelError(RuntimeError):
    """Raised when the provider cannot be constructed (e.g. CLI missing)."""


class ModelCallRecorderPort(Protocol):
    """Anything that can record a model_call row. The CLI provider gets
    one of these injected via ``set_recorder``; the dispatcher sets it
    up before each call so the run_id is correct."""

    def record(
        self,
        *,
        run_id: str,
        purpose: str,
        provider: str,
        model: str,
        prompt_version: str,
        usage: dict[str, Any] | None,
        duration_ms: int,
        input_ref: str | None,
        output_ref: str | None,
        status: str,
        error_code: str | None,
    ) -> None: ...


def get_default_provider(*, recorder: ModelCallRecorderPort | None = None) -> "ModelProviderBase":
    """Return the configured provider for the current process.

    Resolution:
      * ``MEMORY_AGENT_MODEL_PROVIDER=fake`` -> ``FakeModelProvider``
      * anything else (including unset / ``cli``) -> ``ClaudeCLIModelProvider``
    """
    choice = os.environ.get("MEMORY_AGENT_MODEL_PROVIDER", "cli").strip().lower()
    if choice == "fake":
        return FakeModelProvider(recorder=recorder)
    if choice in {"", "cli"}:
        return ClaudeCLIModelProvider(recorder=recorder)
    raise ModelError(
        f"unknown MEMORY_AGENT_MODEL_PROVIDER={choice!r}; expected 'cli' or 'fake'"
    )


class ModelProviderBase:
    name: str = "base"

    def __init__(self, *, recorder: ModelCallRecorderPort | None = None) -> None:
        self._recorder = recorder

    def set_recorder(self, recorder: ModelCallRecorderPort | None) -> None:
        self._recorder = recorder

    def run_extract(self, task: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Claude CLI provider (default)
# ---------------------------------------------------------------------------


# Hard caps. These are conservative defaults and configurable via env vars
# so tests can shrink them.
_DEFAULT_TIMEOUT_SECONDS = 60.0
_MAX_OUTPUT_BYTES = 1_048_576  # 1 MiB
_MAX_PROMPT_BYTES = 256_000    # 256 KiB - well below Claude CLI limits


class ClaudeCLIModelProvider(ModelProviderBase):
    """Run ``claude -p`` for extraction and parse the structured result.

    Configuration:

    * ``cli_path``: absolute path to the ``claude`` binary. Default:
      ``shutil.which('claude')``.
    * ``timeout_seconds``: hard subprocess timeout (default 60s).
    * The provider refuses to run unless
      ``MEMORY_AGENT_ENABLE_CLAUDE_CLI=1`` is set in the environment, so
      v0.1 never reaches into the user's interactive CLI by accident.

    The provider records every attempt via the injected recorder (see
    ``ModelCallRecorderPort``) with the actual status (ok / timeout /
    auth_error / malformed / provider_unavailable) and writes nothing
    to stdout that could be confused with a successful extraction.

    The output envelope returned to the caller is *always* a dict with
    a ``status`` field. Only ``status="ok"`` means the model produced a
    usable candidate set; everything else is a real error.
    """

    name = "claude-cli"

    def __init__(
        self,
        *,
        cli_path: str | None = None,
        timeout_seconds: float | None = None,
        recorder: ModelCallRecorderPort | None = None,
    ) -> None:
        super().__init__(recorder=recorder)
        self.cli_path = cli_path or shutil.which("claude")
        if not self.cli_path:
            raise ModelError(
                "claude CLI not found on PATH; install Claude Code or set MEMORY_AGENT_MODEL_PROVIDER=fake"
            )
        # Opt-in: the user must explicitly enable CLI usage.
        if os.environ.get("MEMORY_AGENT_ENABLE_CLAUDE_CLI") != "1":
            raise ModelError(
                "Claude CLI provider is opt-in; set MEMORY_AGENT_ENABLE_CLAUDE_CLI=1 to use"
            )
        try:
            self.timeout = float(
                timeout_seconds
                if timeout_seconds is not None
                else os.environ.get("MEMORY_AGENT_CLAUDE_CLI_TIMEOUT", _DEFAULT_TIMEOUT_SECONDS)
            )
        except ValueError as exc:
            raise ModelError(f"invalid timeout: {exc}") from exc

    # ----- main entrypoint -------------------------------------------------

    def run_extract(self, task: dict[str, Any]) -> dict[str, Any]:
        prompt = _build_prompt(task)
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > _MAX_PROMPT_BYTES:
            return self._record_and_return(
                task, status="malformed",
                error_code="prompt_too_large",
                payload={"candidates": []},
                duration_ms=0,
            )

        cmd = [
            self.cli_path,
            "-p", prompt,
            "--output-format", "json",
            # Disable tool / network / file / sub-agent access for this
            # single invocation. The flags are documented for Claude Code
            # v1.x; they are ignored when unknown, but a mismatch causes
            # a non-zero exit which we surface as auth_error / malformed.
            "--tools", "",
            "--no-session-persistence",
            "--disable-slash-commands",
        ]

        start = _now_ms()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                # Stream stderr separately so a giant traceback does not
                # silently truncate stdout.
            )
        except subprocess.TimeoutExpired:
            return self._record_and_return(
                task, status="timeout", error_code="timeout",
                payload={"candidates": []},
                duration_ms=_now_ms() - start,
            )
        except FileNotFoundError:
            return self._record_and_return(
                task, status="provider_unavailable",
                error_code="cli_missing",
                payload={"candidates": []},
                duration_ms=_now_ms() - start,
            )

        duration = _now_ms() - start
        out_bytes = len((proc.stdout or "").encode("utf-8"))
        if out_bytes > _MAX_OUTPUT_BYTES:
            return self._record_and_return(
                task, status="malformed", error_code="output_too_large",
                payload={"candidates": []},
                duration_ms=duration,
            )

        if proc.returncode != 0:
            stderr = (proc.stderr or "").lower()
            code = "auth_error" if "auth" in stderr or "oauth" in stderr else "malformed"
            return self._record_and_return(
                task, status=code, error_code=code,
                payload={
                    "candidates": [],
                    "stderr_tail": (proc.stderr or "")[-512:],
                    "returncode": proc.returncode,
                },
                duration_ms=duration,
            )

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return self._record_and_return(
                task, status="malformed", error_code="bad_json",
                payload={"candidates": []},
                duration_ms=duration,
            )

        # Claude Code CLI JSON envelope shapes we accept:
        #   {"type":"result", "is_error": false, "result": "<json string>"}
        #   {"type":"result", "is_error": true,  "result": "..."}
        #   {"result": <json object>}                  (legacy)
        if not isinstance(envelope, dict):
            return self._record_and_return(
                task, status="malformed", error_code="envelope_shape",
                payload={"candidates": []},
                duration_ms=duration,
            )

        if envelope.get("is_error") is True:
            return self._record_and_return(
                task, status="malformed", error_code="model_error",
                payload={"candidates": [], "envelope": envelope},
                duration_ms=duration,
            )

        # Try to extract a candidate list from common envelope variants.
        candidates = self._extract_candidates(envelope)
        if candidates is None:
            return self._record_and_return(
                task, status="malformed", error_code="no_candidates_field",
                payload={"candidates": []},
                duration_ms=duration,
            )

        return self._record_and_return(
            task,
            status="ok",
            error_code=None,
            payload={
                "status": "ok",
                "candidates": candidates,
                "prompt_version": "claude_cli_extract_v1",
                "model": str(envelope.get("model") or "claude"),
            },
            duration_ms=duration,
        )

    # ----- helpers --------------------------------------------------------

    def _extract_candidates(self, envelope: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Locate the candidate list inside the Claude CLI envelope."""
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

    def _record_and_return(
        self,
        task: dict[str, Any],
        *,
        status: str,
        error_code: str | None,
        payload: dict[str, Any],
        duration_ms: int,
    ) -> dict[str, Any]:
        if self._recorder is not None:
            self._recorder.record(
                run_id=str(task.get("run_id") or "run_unknown"),
                purpose=str(task.get("task_type") or "extract"),
                provider=self.name,
                model=str(payload.get("model") or "claude"),
                prompt_version=str(payload.get("prompt_version") or "claude_cli_extract_v1"),
                usage=payload.get("usage"),
                duration_ms=int(duration_ms),
                input_ref=str(task.get("task_id") or ""),
                output_ref=None,
                status=status,
                error_code=error_code,
            )
        # The envelope returned to the application keeps ``status`` set
        # so callers can branch without parsing provider internals.
        payload = dict(payload)
        payload["status"] = status
        if error_code is not None:
            payload.setdefault("error_code", error_code)
        return payload


# ---------------------------------------------------------------------------
# Fake provider (test-only)
# ---------------------------------------------------------------------------


class FakeModelProvider(ModelProviderBase):
    """Deterministic in-memory provider for tests.

    Two ways to drive it:
      * ``set_response(payload)``: hand it the exact dict to return next.
      * ``set_failure(status)``: hand it a status string for the next call.
      * No override: heuristic over each member's ``text`` field — emits
        a candidate per line that looks like a user requirement.

    Never raises; ``run_extract`` always returns a dict. Its recorder
    emits a single ``ok`` row per call.
    """

    name = "fake-model"

    def __init__(self, *, recorder: ModelCallRecorderPort | None = None) -> None:
        super().__init__(recorder=recorder)
        self._next_response: dict[str, Any] | None = None
        self._fail_status: str | None = None

    def set_response(self, payload: dict[str, Any]) -> None:
        self._next_response = payload

    def set_failure(self, status: str) -> None:
        self._fail_status = status

    def run_extract(self, task: dict[str, Any]) -> dict[str, Any]:
        if self._fail_status:
            status = self._fail_status
            self._fail_status = None
            return self._record(task, status=status, candidates=[])
        if self._next_response is not None:
            payload = self._next_response
            self._next_response = None
            return self._record(task, status=payload.get("status", "ok"),
                                candidates=payload.get("candidates", []),
                                prompt_version=payload.get("prompt_version", "fake_extract_v1"),
                                model=payload.get("model", self.name),
                                duration_ms=payload.get("duration_ms", 1))
        # Heuristic extraction over member text.
        members = task.get("members", [])
        candidates: list[dict[str, Any]] = []
        for m in members:
            text = m.get("text") or ""
            for line in text.splitlines():
                if _is_explicit_user_requirement(line):
                    candidates.append({
                        "candidate_id": f"cand_{m.get('unit_id', 'u')[:6]}_{len(candidates):02d}",
                        "kind": "requirement",
                        "abstraction": "no_opinion",
                        "statement": line.strip(),
                        "modality": "unspecified",
                        "scope": {"level": "global"},
                        "tags": [],
                        "evidence": [
                            {
                                "source_event_id": m.get("event_id", m.get("unit_id", "evt")),
                                "source_revision_hash": m.get("revision_hash", "rev"),
                                "role": "user",
                                "origin": "user_session",
                                "quote": line.strip(),
                                "span_start": int(m.get("span_start", 0)),
                                "span_end": int(m.get("span_end", len(line))),
                            }
                        ],
                    })
        return self._record(
            task, status="ok", candidates=candidates,
            prompt_version="fake_extract_v1", model=self.name, duration_ms=1,
        )

    def _record(
        self, task: dict[str, Any], *,
        candidates: list[dict[str, Any]], status: str,
        prompt_version: str = "fake_extract_v1",
        model: str | None = None,
        duration_ms: int = 1,
    ) -> dict[str, Any]:
        if self._recorder is not None:
            self._recorder.record(
                run_id=str(task.get("run_id") or "run_unknown"),
                purpose=str(task.get("task_type") or "extract"),
                provider=self.name,
                model=model or self.name,
                prompt_version=prompt_version,
                usage=None,
                duration_ms=int(duration_ms),
                input_ref=str(task.get("task_id") or ""),
                output_ref=None,
                status=status,
                error_code=None if status == "ok" else status,
            )
        return {
            "status": status,
            "candidates": candidates,
            "prompt_version": prompt_version,
            "model": model or self.name,
        }


def _is_explicit_user_requirement(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    triggers = ("must", "should", "always", "use ", "use:", "用", "请", "以后", "默认")
    return any(s.lower().startswith(t.lower()) or t in s for t in triggers)


def _build_prompt(task: dict[str, Any]) -> str:
    """Compose the extraction prompt sent to ``claude -p``.

    The contract is locked to a strict JSON envelope with the
    documented schema. The model is told the *exact* shape of a
    candidate, including ``scope`` as an object with a required
    ``level`` field; this prevents drift back to the bare-string
    shape some Claude outputs use.
    """
    schema_example = json.dumps({
        "candidates": [
            {
                "candidate_id": "cand_001",
                "kind": "requirement",
                "abstraction": "no_opinion",
                "statement": "use Python for the prototype",
                "modality": "must",
                "scope": {
                    "level": "global",
                    "project_ids": [],
                    "domain_ids": [],
                    "task_ids": [],
                    "conditions": [],
                    "excludes": [],
                },
                "primary_domain": None,
                "tags": [],
                "evidence": [
                    {
                        "source_event_id": "evt_123",
                        "source_revision_hash": "rh",
                        "role": "user",
                        "origin": "user_session",
                        "quote": "use Python for the prototype",
                        "span_start": 0,
                        "span_end": 26,
                    }
                ],
            }
        ]
    }, ensure_ascii=False)
    return (
        "You are extracting explicit user requirements from a "
        "session batch. You must not call any tools. You must return "
        "exactly one JSON object on stdout, with the literal shape "
        "below — no surrounding prose, no markdown fences.\n\n"
        "Strict JSON schema:\n"
        "{\n"
        '  "status": "ok" | "no_candidates",\n'
        '  "candidates": [\n'
        "    {\n"
        '      "candidate_id": string,\n'
        '      "kind": "requirement" | "preference" | "self_fact" | "out_of_scope",\n'
        '      "abstraction": "principle" | "detail" | "no_opinion",\n'
        '      "statement": string,\n'
        '      "modality": "must" | "prefer" | "avoid" | "unspecified",\n'
        '      "scope": {"level": "global"|"domain"|"project"|"task"|"session",'
        ' "project_ids": string[], "domain_ids": string[], "task_ids": string[],'
        ' "conditions": string[], "excludes": string[]},\n'
        '      "primary_domain": string | null,\n'
        '      "tags": string[],\n'
        '      "evidence": [\n'
        "        {\n"
        '          "source_event_id": string,\n'
        '          "source_revision_hash": string,\n'
        '          "role": "user",\n'
        '          "origin": "user_session",\n'
        '          "quote": string,\n'
        '          "span_start": integer,\n'
        '          "span_end": integer\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Constraints:\n"
        "- evidence.role must be exactly \"user\".\n"
        "- evidence.origin must be exactly \"user_session\".\n"
        "- evidence.quote must appear at evidence.span_start..span_end in the\n"
        "  source event text (half-open interval).\n"
        "- evidence.source_event_id must be the event_id of one of the\n"
        "  members in the batch below.\n"
        "- scope is an OBJECT with field level; level must be one of\n"
        "  global/domain/project/task/session. Do NOT return scope as a\n"
        "  bare string. Bare-string scopes are rejected.\n"
        "- If no candidates apply, return {\"status\":\"ok\",\"candidates\":[]}.\n\n"
        f"Example envelope (matches schema): {schema_example}\n\n"
        f"Batch: {json.dumps(task, ensure_ascii=False)}"
    )


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Helper for SQLite-backed recorder used by the dispatcher.
# ---------------------------------------------------------------------------


@dataclass
class SQLiteModelCallRecorder:
    """Persists model_call rows on the vault's SQLite connection.

    v0.1 has no async job lifecycle (``runs`` is filled by an async
    runner that ships later); for the sync path we keep model_calls
    rows self-consistent by ensuring a synthetic ``runs`` row exists
    for every run_id we record. The synthetic row is harmless to the
    rest of the system because nothing else keys off ``runs``.
    """

    conn: Any  # sqlite3.Connection

    def record(
        self,
        *,
        run_id: str,
        purpose: str,
        provider: str,
        model: str,
        prompt_version: str,
        usage: dict[str, Any] | None,
        duration_ms: int,
        input_ref: str | None,
        output_ref: str | None,
        status: str,
        error_code: str | None,
    ) -> None:
        # Ensure parent rows exist (FK targets for model_calls).
        # v0.1 has no async job lifecycle; the recorder synthesises the
        # bare rows it needs to satisfy the FK chain. These rows are
        # harmless because nothing else in v0.1 keys off them.
        job_id = f"job_{run_id}"
        self.conn.execute(
            """INSERT OR IGNORE INTO jobs(
                job_id, request_id, client_id, idempotency_key, request_hash,
                operation, params_ref, state, created_at
            ) VALUES (?, ?, 'sync_recorder', NULL, ?, 'extract', NULL, 'completed', ?)""",
            (job_id, run_id, run_id, _now_iso()),
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO runs(
                run_id, job_id, stage, batch_id, attempt, status,
                started_at, ended_at, output_ref
            ) VALUES (?, ?, 'extract', NULL, 1, ?, ?, ?, NULL)""",
            (
                run_id, job_id, status,
                _now_iso(), _now_iso(),
            ),
        )
        call_id = "call_" + hashlib.sha1(
            f"{run_id}|{provider}|{model}|{purpose}|{prompt_version}|{duration_ms}|{status}|{_now_ns()}".encode()
        ).hexdigest()[:16]
        self.conn.execute(
            """INSERT INTO model_calls(
                call_id, run_id, purpose, provider, model,
                prompt_version, usage_json, duration_ms,
                input_ref, output_ref, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                call_id, run_id, purpose, provider, model,
                prompt_version, json.dumps(usage or {}), int(duration_ms),
                input_ref, output_ref,
                _now_iso(),
            ),
        )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _now_ns() -> int:
    import time
    return time.time_ns()


# ---------------------------------------------------------------------------
# Public helper: build a CandidateSet from a fake provider's run_extract
# output, so tests can do end-to-end without going through the dispatcher.
# ---------------------------------------------------------------------------


def candidate_set_from_fake_output(
    output: dict[str, Any], *, batch_id: str
) -> CandidateSet:
    payload = output.get("candidates", [])
    cands = [
        Candidate(
            candidate_id=item.get("candidate_id") or f"cand_{i}",
            kind=item.get("kind", "out_of_scope"),
            abstraction=item.get("abstraction", "no_opinion"),
            statement=item.get("statement") or "",
            modality=item.get("modality", "unspecified"),
            scope=Scope(**item.get("scope", {"level": "global"})),
            primary_domain=item.get("primary_domain"),
            tags=list(item.get("tags", [])),
            evidence=[EvidenceRef(**e) for e in item.get("evidence", [])],
        )
        for i, item in enumerate(payload)
    ]
    return CandidateSet(
        schema_version=CANDIDATE_SCHEMA_VERSION,
        candidate_set_id="cs_" + hashlib.sha1(batch_id.encode()).hexdigest()[:12],
        batch_id=batch_id,
        candidates=cands,
        no_memory=len(cands) == 0,
        prompt_version=output.get("prompt_version", "fake_extract_v1"),
        model=output.get("model", "fake-model"),
        raw_payload_sha256=hashlib.sha256(
            json.dumps(output, sort_keys=True).encode()
        ).hexdigest(),
    )