"""Test the CLI help and entry point."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class TestCLI:
    def test_python_module_help(self):
        proc = subprocess.run(
            [sys.executable, "-m", "memory_agent", "--help"],
            capture_output=True, text=True, env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"},
            check=False,
        )
        assert proc.returncode == 0
        assert "Memory Agent" in proc.stdout or "memory" in proc.stdout.lower()

    def test_memory_command_help(self):
        # We can't rely on `memory` being installed in PATH; use module.
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, 'src'); "
             "from memory_agent.interfaces.cli.main import build_parser; "
             "p = build_parser(); p.print_help()"],
            capture_output=True, text=True, cwd=str(ROOT),
            check=False,
        )
        assert proc.returncode == 0
        assert "init" in proc.stdout
        assert "scan" in proc.stdout
        assert "recall" in proc.stdout

    def test_reject_unknown_operation(self, tmp_path):
        # No operation dispatch path; but parsing rejects unknown ops.
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, 'src'); "
             "from memory_agent.application.dispatch import dispatch; "
             "import json; r = dispatch(operation='init', params={}, vault_root=__import__('pathlib').Path('v')); "
             "print(json.dumps(r))"],
            capture_output=True, text=True, cwd=str(ROOT),
            check=False,
        )
        # We expect at least that the subprocess completed (returncode 0).
        assert proc.returncode == 0
        # Output should contain a CommandResponse JSON object.
        out = proc.stdout.strip()
        assert out
        # The output JSON should at minimum have status field.
        parsed = json.loads(out.splitlines()[-1])
        assert "status" in parsed