"""BM25 isolation: BM25 is never imported / called from scan, batch,
reconcile, or maintain.

The v1 invariant is:

  * The recall path is the only path that may import or use
    ``memory_agent.application.search.bm25``.

This file enforces that invariant by static inspection of the modules
listed below. It does NOT depend on import-time side effects (so it
catches the *intent* even if some of those modules currently happen
not to import bm25).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# tests/unit/test_bm25_isolation.py -> ROOT/tests/unit/.. = ROOT (memory-agent)
# We want SRC = ROOT/src
SRC = ROOT / "src"

NON_RECALL_MODULES = [
    "memory_agent.application.scan",
    "memory_agent.application.plan_batches",
    "memory_agent.application.process_batches",
    "memory_agent.application.commit",
    "memory_agent.application.reconcile",
    "memory_agent.application.recovery",
    "memory_agent.application.dispatch",
    "memory_agent.interfaces.cli.main",
]


@pytest.mark.parametrize("module_path", NON_RECALL_MODULES)
def test_module_does_not_import_bm25(module_path: str) -> None:
    """Each non-recall module source must not import the BM25 module.

    Allowed: import via re-export from ``recall`` is fine, but bm25
    itself must not appear in the AST.
    """
    # ``memory_agent.application.scan`` -> ``src/memory_agent/application/scan.py``
    parts = module_path.split(".")
    file_path = SRC.joinpath(*parts).with_suffix(".py")
    if not file_path.exists():
        pytest.skip(f"module file not found at {file_path}")
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith(".bm25") or alias.name == "memory_agent.application.search.bm25":
                    offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module.endswith(".bm25")
                                or node.module == "memory_agent.application.search.bm25"):
                offenders.append(f"from {node.module} import …")
    assert not offenders, (
        f"{module_path} must not import BM25; offenders: {offenders}. "
        "BM25 is opt-in at recall time only."
    )


def test_recall_path_is_the_only_import_point() -> None:
    """Confirm ``application.recall`` is the single import point of
    ``application.search.bm25``."""
    recall_file = SRC / "memory_agent" / "application" / "recall.py"
    text = recall_file.read_text(encoding="utf-8")
    assert "bm25" in text, "recall.py should reference the BM25 module"


def test_bm25_does_not_persist_state() -> None:
    """BM25 must not write to SQLite, the vault, or any persistent store.

    The module is in-memory only. If a future revision adds persistence,
    this test must fail first so the design review catches it.
    """
    bm25_file = SRC / "memory_agent" / "application" / "search" / "bm25.py"
    text = bm25_file.read_text(encoding="utf-8")
    forbidden = ["sqlite3", "open(", "open_state_db", "atomic_write", ".sqlite"]
    hits = [t for t in forbidden if t in text]
    assert not hits, (
        f"bm25.py must be in-memory only; found forbidden tokens: {hits}"
    )
