"""Optional model-backed rewrite/expansion provider.

v1 ships a typed protocol, a deterministic stub implementation, and a
typed opt-in Claude CLI rewrite provider. The recall pipeline asks
the provider for ``rewrite(original)`` and ``expansions(original)``
only when the caller passes ``rewrite=auto`` / ``expand=auto``. If
the provider is not wired, the planner falls back to **original-only
retrieval** and records the fallback reason in the trace.

Provider selection (phase C repair-2):

  * Default: ``IdentityRewriteProvider`` (no rewrite, no expansion).
  * Opt-in: when the caller passes ``rewrite=auto`` / ``expand=auto``
    and the provider is wired, the planner asks it for one rewrite
    plus up to two expansions. Constraints (negations, hard filters,
    exact identifiers) are preserved; rewrites that violate them are
    rejected with a machine-readable reason.
  * Failure: any provider error falls back to the original query and
    records the reason in the trace. The recall never fails on a
    model-layer hiccup.

A live Claude CLI provider is wired behind
``MEMORY_AGENT_ENABLE_CLAUDE_CLI=1`` (the same opt-in the extraction
provider uses). A test-friendly ``FakeRewriteProvider`` lets
fixtures pin the rewrite / expansion output without touching the
CLI.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Protocol


class RewriteProvider(Protocol):
    """Optional provider used by the recall planner.

    Implementations MUST:

      * Be deterministic for a given input (so test fixtures can pin
        their outputs).
      * Never see excluded memory content — only the original query and
        the user-supplied hard filters are passed in.
      * Raise ``RewriteProviderError`` for any failure (auth, timeout,
        malformed output). The planner treats any failure as a
        fallback to original-only retrieval.
    """

    name: str
    prompt_version: str

    def rewrite(self, *, original_query: str, hard_filters: dict) -> str | None:
        """Return a single equivalent rewrite, or ``None`` if no
        suitable rewrite is available.
        """
        ...

    def expansions(self, *, original_query: str, hard_filters: dict) -> list[str]:
        """Return up to 2 expansion candidates."""
        ...


class RewriteProviderError(RuntimeError):
    """Raised when a rewrite/expansion provider fails."""


@dataclass
class IdentityRewriteProvider:
    """Default stub provider: refuses to rewrite or expand.

    This is the *safe* fallback. It lets the recall pipeline emit a
    ``fallback_reason="no_provider_wired"`` and run with the original
    query only — matching the v0.1 baseline.
    """

    name: str = "identity-stub"
    prompt_version: str = "identity-v1"

    def rewrite(self, *, original_query: str, hard_filters: dict) -> str | None:
        _ = (original_query, hard_filters)
        return None

    def expansions(self, *, original_query: str, hard_filters: dict) -> list[str]:
        _ = (original_query, hard_filters)
        return []


@dataclass
class StaticRewriteProvider:
    """Provider that returns a pre-canned rewrite + expansions.

    Useful for tests and for documenting what a real provider *would*
    return. If ``raise_on_call`` is True the provider raises
    ``RewriteProviderError`` on every call, so tests can exercise the
    original-only fallback path.
    """

    name: str = "static-stub"
    prompt_version: str = "static-v1"
    rewrite_value: str | None = None
    expansion_values: tuple[str, ...] = ()
    raise_on_call: bool = False

    def rewrite(self, *, original_query: str, hard_filters: dict) -> str | None:
        _ = (original_query, hard_filters)
        if self.raise_on_call:
            raise RewriteProviderError("provider intentionally fails")
        return self.rewrite_value

    def expansions(self, *, original_query: str, hard_filters: dict) -> list[str]:
        _ = (original_query, hard_filters)
        if self.raise_on_call:
            raise RewriteProviderError("provider intentionally fails")
        return list(self.expansion_values)


class ClaudeCLIRewriteProvider:
    """Real opt-in Claude CLI rewrite provider (phase C repair-2).

    The provider is gated on ``MEMORY_AGENT_ENABLE_CLAUDE_CLI`` —
    without that env var, ``get_default_rewrite_provider`` returns
    the identity stub. When the env var is set, the provider shells
    out to ``claude -p`` with a tightly-scoped prompt that asks for
    a single rewrite and up to two expansions of the user's query.
    The provider never sees excluded memory content; the prompt is
    built from the original query + hard filters only.

    Output is validated as JSON ``{"rewrite": str|null, "expansions":
    [str, ...]}``; any failure (auth, timeout, malformed) raises
    ``RewriteProviderError`` so the planner can fall back to
    original-only retrieval.
    """

    name: str = "claude-cli"
    prompt_version: str = "rewrite-v1"

    def __init__(
        self,
        *,
        binary: str = "claude",
        timeout_seconds: float = 10.0,
        model: str = "claude-haiku-4-5",
    ) -> None:
        self._binary = binary
        self._timeout = float(timeout_seconds)
        self._model = model

    def rewrite(self, *, original_query: str, hard_filters: dict) -> str | None:
        result = self._invoke(original_query, hard_filters)
        return result.get("rewrite") if result else None

    def expansions(self, *, original_query: str, hard_filters: dict) -> list[str]:
        result = self._invoke(original_query, hard_filters)
        if not result:
            return []
        raw = result.get("expansions") or []
        if not isinstance(raw, list):
            return []
        return [str(x) for x in raw if isinstance(x, str)][:2]

    def _invoke(self, original_query: str, hard_filters: dict) -> dict:
        prompt = self._build_prompt(original_query, hard_filters)
        try:
            proc = subprocess.run(
                [self._binary, "-p", prompt, "--output-format", "json",
                 "--model", self._model],
                capture_output=True, text=True, timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RewriteProviderError(
                f"claude CLI not found at {self._binary!r}: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RewriteProviderError(
                f"claude CLI timed out after {self._timeout}s"
            ) from exc
        if proc.returncode != 0:
            raise RewriteProviderError(
                f"claude CLI exit {proc.returncode}: "
                f"{(proc.stderr or '').strip()[:200]}"
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RewriteProviderError(
                f"claude CLI returned non-JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RewriteProviderError(
                "claude CLI returned non-object payload"
            )
        return payload

    def _build_prompt(self, original_query: str, hard_filters: dict) -> str:
        filters_str = json.dumps(hard_filters, ensure_ascii=False, sort_keys=True)
        return (
            "You are a query rewriter for a personal memory store. "
            "Given the user's original query and the hard filters "
            "(project_ids, domain_ids), produce a single equivalent "
            "rewrite that preserves every negation, exact identifier, "
            "and project / domain filter. Output JSON with shape "
            "{\"rewrite\": str|null, \"expansions\": [str, str]}."
            "\n\nOriginal query: " + original_query
            + "\n\nHard filters: " + filters_str
        )


@dataclass
class FakeRewriteProvider:
    """Provider that returns configurable outputs for tests.

    Tests inject the provider through ``RewriteProviderConfig`` so
    the planner can be exercised without a real model call. Setting
    ``raise_on_call`` makes the provider raise on every call so
    fallback semantics are testable.
    """

    name: str = "fake"
    prompt_version: str = "fake-v1"
    rewrite_value: str | None = None
    expansion_values: tuple[str, ...] = ()
    raise_on_call: bool = False

    def rewrite(self, *, original_query: str, hard_filters: dict) -> str | None:
        _ = (original_query, hard_filters)
        if self.raise_on_call:
            raise RewriteProviderError("fake provider intentionally fails")
        return self.rewrite_value

    def expansions(self, *, original_query: str, hard_filters: dict) -> list[str]:
        _ = (original_query, hard_filters)
        if self.raise_on_call:
            raise RewriteProviderError("fake provider intentionally fails")
        return list(self.expansion_values)


def get_default_rewrite_provider() -> RewriteProvider:
    """Return the default rewrite provider.

    Phase C repair-2:

      * If ``MEMORY_AGENT_ENABLE_CLAUDE_CLI`` is set, return a
        real ``ClaudeCLIRewriteProvider`` so ``rewrite=auto`` /
        ``expand=auto`` actually use a model.
      * Otherwise return the identity stub (safe fallback).
    """
    if os.environ.get("MEMORY_AGENT_ENABLE_CLAUDE_CLI") == "1":
        return ClaudeCLIRewriteProvider()
    return IdentityRewriteProvider()
