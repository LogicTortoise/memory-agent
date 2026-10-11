"""CHEATSHEET drift guard.

`docs/CHEATSHEET.md` documents ~12 day-2 recipes. This test parses every
`python -m memory_agent …` invocation out of its fenced bash blocks and
asserts each `--flag` is documented by the matching subcommand's `--help`
output. The guard catches both:

  * a recipe still references a flag that was renamed or removed
    (CLI divergence — operator will hit `unrecognized arguments`); and
  * a new flag was added to the CLI but the recipe was not updated
    (docs divergence — operator cannot use the new affordance).

It does NOT execute any mutating command. Every assertion is against
`--help` output, which is read-only and side-effect free.

Skipped recipes:
  * heredoc setup lines (cat > file <<EOF ... EOF and the lines inside);
  * leading `# ...` comments.

Line continuations (a recipe split across multiple lines with backslash)
are joined before parsing.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "CHEATSHEET.md"


def _collect_invocations() -> list[str]:
    """Extract ``python -m memory_agent`` invocations from CHEATSHEET.md."""
    text = DOC.read_text(encoding="utf-8")
    raw_lines: list[str] = []
    in_block = False
    in_heredoc = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```bash"):
            in_block = True
            continue
        if stripped == "```":
            in_block = False
            in_heredoc = False
            continue
        if not in_block:
            continue
        if in_heredoc:
            if stripped == "EOF" or stripped in {"'EOF'", '"EOF"'}:
                in_heredoc = False
            continue
        if stripped.startswith("#"):
            continue
        if "<<" in stripped and "EOF" in stripped:
            in_heredoc = True
            continue
        raw_lines.append(line)

    # Join backslash-continued lines.
    joined: list[str] = []
    buffer = ""
    for line in raw_lines:
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        if buffer:
            buffer += line
            joined.append(buffer)
            buffer = ""
        else:
            joined.append(line)
    if buffer:
        joined.append(buffer)

    invocations: list[str] = []
    for line in joined:
        if not line.lstrip().startswith("python -m memory_agent"):
            continue
        cleaned = re.sub(r"\s+#.*$", "", line).strip()
        # Strip trailing shell redirects/pipes (the CHEATSHEET uses
        # ``| head -N`` after some read-only commands). These do not
        # affect the argv that reaches the CLI; they only re-shape the
        # shell-side stdout.
        cleaned = re.split(r"[\|><]", cleaned, maxsplit=1)[0].strip()
        if cleaned:
            invocations.append(cleaned)
    return invocations


def _walk(argv: list[str]) -> list[str]:
    """Strip known global flags from argv, return the subcommand path.

    The root parser only accepts ``--vault``, ``--request-id``,
    ``--idempotency-key``, ``--json``, ``--pretty`` before the
    subcommand. The CHEATSHEET only uses ``--vault`` (always with a
    value), ``--json`` (default true, never passed explicitly), and the
    subcommand itself.
    """
    consumed: list[str] = []
    rest: list[str] = []
    i = 0
    seen_subcommand = False
    while i < len(argv):
        tok = argv[i]
        if seen_subcommand:
            rest.append(tok)
            i += 1
            continue
        if tok in {"--vault", "--request-id", "--idempotency-key"}:
            consumed.extend([tok, argv[i + 1]])
            i += 2
            continue
        if tok in {"--json", "--pretty"}:
            consumed.append(tok)
            i += 1
            continue
        # Subcommand (positional). Everything from here is subcmd args.
        seen_subcommand = True
        rest.append(tok)
        i += 1
    return rest


def _resolve_subparser(
    parser: argparse.ArgumentParser, subcmd_argv: list[str]
) -> tuple[argparse.ArgumentParser, list[str]]:
    """Drill into nested subparsers along ``subcmd_argv`` and return the
    deepest subparser + the leading subcommand path (e.g.
    ``['privacy', 'forget']``).
    """
    path: list[str] = []
    i = 0
    while i < len(subcmd_argv):
        tok = subcmd_argv[i]
        if tok.startswith("-"):
            break
        path.append(tok)
        # Find the subparser action whose choices contain ``tok``.
        matches: list[argparse.Action] = [
            action for action in parser._actions  # type: ignore[attr-defined]
            if hasattr(action, "choices")
            and action.choices
            and tok in action.choices
        ]
        if not matches:
            # No further subparsers — this token is a flag or positional
            # argument to the current parser.
            path.pop()
            break
        assert len(matches) == 1, (
            f"ambiguous subcommand match for {tok!r}: {matches}"
        )
        parser = matches[0].choices[tok]  # type: ignore[index, assignment]
        i += 1
    return parser, path


def _strip_value_tokens(
    parser: argparse.ArgumentParser, argv: list[str]
) -> list[str]:
    """Drop the *value* tokens following each flag in ``argv``, but keep
    enough so that ``--help`` can run cleanly.

    argparse's `--help` walks actions to print usage, but it still tries
    to consume the flag's value if the action is not a store-true /
    append-without-value. So we keep *placeholder* values for any flag
    whose action requires one (or more) value, using a type-appropriate
    dummy: an int action gets ``0``; a str action gets ``x``; an
    ``action="append"`` gets one ``x``.
    """
    # Build a fast lookup: ``dest -> action`` for the deepest parser.
    actions_by_opt: dict[str, argparse.Action] = {
        opt: action
        for action in parser._actions  # type: ignore[attr-defined]
        for opt in action.option_strings
    }
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if not tok.startswith("-"):
            out.append(tok)
            i += 1
            continue
        # Inline `--flag=value` form.
        if "=" in tok:
            out.append(tok)
            i += 1
            continue
        action = actions_by_opt.get(tok)
        out.append(tok)
        if action is None:
            # Unknown flag at the parser level — let argparse complain
            # via `--help` so the test fails with the real error.
            i += 1
            continue
        # store_true / store_false: no value follows.
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            i += 1
            continue
        # count / help: no value.
        if isinstance(action, (argparse._CountAction, argparse._HelpAction)):
            i += 1
            continue
        # append_const: no value.
        if isinstance(action, argparse._AppendConstAction):
            i += 1
            continue
        # append / store: emit one placeholder value.
        out.append(_placeholder_for(action))
        i += 1
    return out


def _placeholder_for(action: argparse.Action) -> str:
    """Pick a type-appropriate dummy value for ``action``."""
    t = getattr(action, "type", None)
    choices = getattr(action, "choices", None)
    if choices:
        # ``choices`` is enforced by argparse at parse time; pick the
        # first member regardless of type so the dummy is valid.
        return str(next(iter(choices)))
    if t is int:
        return "0"
    if t is float:
        return "0.0"
    if t is None:
        return "x"
    try:
        # Custom callable type — invoke with a benign string.
        coerced = t("x")
        return str(coerced)
    except Exception:
        return "x"


def _walk_subparser_help(subcmd_argv: list[str]) -> str:
    """Run ``python -m memory_agent <subcmd_argv> --help`` and return stdout."""
    cmd = [sys.executable, "-m", "memory_agent", *subcmd_argv, "--help"]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"`{' '.join(cmd)}` failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc.stdout


def _build_root_parser() -> argparse.ArgumentParser:
    from memory_agent.interfaces.cli.main import build_parser
    return build_parser()


def test_cheatsheet_invocations_are_valid() -> None:
    invocations = _collect_invocations()
    assert invocations, "CHEATSHEET must contain at least one CLI invocation"
    seen: set[tuple[str, ...]] = set()
    failures: list[str] = []
    for invocation in invocations:
        argv = shlex.split(invocation)
        # Drop the leading `python -m memory_agent`.
        assert argv[:3] == ["python", "-m", "memory_agent"], argv
        argv = argv[3:]
        subcmd_argv = _walk(argv)
        subcmd_key = tuple(subcmd_argv)
        if subcmd_key in seen:
            continue
        seen.add(subcmd_key)

        # Walk to the deepest subparser that owns the flag set used by
        # this invocation. The recipe line carries flags that may belong
        # to either the top-level subcommand or a nested one — we hand
        # ``argv`` to ``_resolve_subparser`` to figure that out.
        root = _build_root_parser()
        _subparser, subcmd_path = _resolve_subparser(root, subcmd_argv)
        # Replace the subcommand-path prefix in ``subcmd_argv`` with the
        # resolved path (they should be identical, but this is a sanity
        # check).
        assert subcmd_argv[: len(subcmd_path)] == subcmd_path, (
            invocation, subcmd_argv, subcmd_path
        )

        # Build a ``--help`` argv: subcommand path + flag tokens
        # (with type-appropriate placeholder values).
        help_argv = [*subcmd_path, *_strip_value_tokens(_subparser, subcmd_argv[len(subcmd_path):])]
        help_text = _walk_subparser_help(help_argv)

        # Validate the recipe line's flags against the help text.
        recipe_flags = [
            tok for tok in subcmd_argv
            if tok.startswith("-")
        ]
        for flag in recipe_flags:
            name = flag.split("=", 1)[0]
            if not re.search(rf"(^|\n)\s*{re.escape(name)}\b", help_text):
                failures.append(
                    f"{invocation!r}: flag {name!r} is not in "
                    f"`memory {' '.join(help_argv)} --help`"
                )
    assert not failures, "\n".join(failures)


def test_cheatsheet_is_well_formed() -> None:
    """Smoke-check that the doc still parses and contains the recipe index."""
    text = DOC.read_text(encoding="utf-8")
    assert "## 1." in text and "## 12." in text, (
        "CHEATSHEET must list recipes 1..12"
    )
    invocations = _collect_invocations()
    assert len(invocations) >= 12, (
        f"expected >=12 CLI invocations in CHEATSHEET, found {len(invocations)}"
    )
