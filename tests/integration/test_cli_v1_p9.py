"""P9 / T17 — CLI subprocess tests for privacy / backup commands.

The harness drives ``python -m memory_agent --vault <tmp> privacy …
`` / ``backup …`` as a subprocess (not in-process) so we exercise
the full CLI argv path. Invented fixtures only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


def _run(args: list[str], vault: Path) -> dict[str, Any]:
    cmd = [
        sys.executable, "-m", "memory_agent",
        "--vault", str(vault),
        *args,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    # argparse errors print to stderr and produce no JSON; treat
    # them as INVALID_REQUEST (exit 2).
    stdout = (res.stdout or "").strip()
    if not stdout:
        return {
            "payload": {"error": {"code": "INVALID_REQUEST",
                                  "message": (res.stderr or "").strip()}},
            "exit": res.returncode,
            "stderr": res.stderr,
        }
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise AssertionError(
            f"non-JSON stdout: stdout={res.stdout!r} stderr={res.stderr!r}"
        ) from exc
    return {"payload": payload, "exit": res.returncode, "stderr": res.stderr}


@pytest.fixture
def init_vault(tmp_path: Path) -> Path:
    """Init a vault so the privacy/backup commands have a DB to operate on."""
    cmd = [sys.executable, "-m", "memory_agent",
           "--vault", str(tmp_path), "init"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stderr
    # Also write a HEAD.json so the vault is in a non-empty state.
    (tmp_path / "HEAD.json").write_text(
        json.dumps({"commit_id": "commit_cli"}),
        encoding="utf-8",
    )
    return tmp_path


def test_privacy_forget_list_receipt_unblock_cli(init_vault: Path):
    vault = init_vault
    # forget
    res = _run([
        "privacy", "forget",
        "--kind", "source_block",
        "--target", "src-cli-001",
        "--idempotency-key", "idem_cli_001",
        "--note", "test-only fixture",
    ], vault)
    assert res["exit"] == 0, res["payload"]
    receipt = res["payload"].get("result") or {}
    op_id = receipt.get("op_id")
    assert op_id and receipt.get("kind") == "source_block"
    assert receipt.get("targets") == ["src-cli-001"]
    # list
    res = _run(["privacy", "list", "--kind", "source_block"], vault)
    assert res["exit"] == 0, res["payload"]
    items = (res["payload"].get("result") or {}).get("receipts") or []
    assert any(r["op_id"] == op_id for r in items)
    # receipt
    res = _run(["privacy", "receipt", "--op-id", op_id], vault)
    assert res["exit"] == 0
    assert (res["payload"].get("result") or {}).get("op_id") == op_id
    # unblock
    res = _run([
        "privacy", "unblock",
        "--op-id", op_id,
        "--idempotency-key", "idem_cli_unblock_001",
    ], vault)
    assert res["exit"] == 0, res["payload"]


def test_privacy_forget_unknown_kind_is_invalid_request(init_vault: Path):
    res = _run([
        "privacy", "forget",
        "--kind", "not_a_kind",
        "--target", "x",
        "--idempotency-key", "idem_x",
    ], init_vault)
    assert res["exit"] == 2
    code = (res["payload"].get("error") or {}).get("code")
    assert code == "INVALID_REQUEST"


def test_privacy_forget_memory_unknown_op_is_not_found(init_vault: Path):
    res = _run([
        "privacy", "receipt",
        "--op-id", "del_does_not_exist",
    ], init_vault)
    assert res["exit"] == 2
    code = (res["payload"].get("error") or {}).get("code")
    assert code == "NOT_FOUND"


def test_backup_create_and_restore_cli(init_vault: Path, tmp_path: Path):
    # Create a bundle.
    out = tmp_path / "cli_bundle.tar"
    res = _run([
        "backup", "create",
        "--output", str(out),
        "--label", "cli-test",
    ], init_vault)
    assert res["exit"] == 0, res["payload"]
    result = res["payload"].get("result") or {}
    assert result.get("bundle_id", "").startswith("bk_")
    assert Path(result.get("output") or out).exists()
    # List the bundle.
    res = _run(["backup", "list", "--bundle", str(out)], init_vault)
    assert res["exit"] == 0, res["payload"]
    assert (res["payload"].get("result") or {}).get("bundle_id") == \
        result["bundle_id"]
    # Restore into a clean target.
    target = tmp_path / "cli_restored"
    res = _run([
        "backup", "restore",
        "--bundle", str(out),
        "--target", str(target),
    ], init_vault)
    assert res["exit"] == 0, res["payload"]
    assert (target / "state" / "state.sqlite").exists()
    assert (target / "privacy" / "HEAD.json").exists()


def test_backup_restore_into_existing_vault_is_rejected(
    init_vault: Path, tmp_path: Path,
):
    out = tmp_path / "cli_bundle.tar"
    _run([
        "backup", "create",
        "--output", str(out),
    ], init_vault)
    # Try to restore into the *same* vault: must be refused because
    # state/state.sqlite is already present.
    res = _run([
        "backup", "restore",
        "--bundle", str(out),
        "--target", str(init_vault),
    ], init_vault)
    assert res["exit"] in (5, 6)
    code = (res["payload"].get("error") or {}).get("code")
    assert code in ("INTERNAL", "INVALID_REQUEST")


def test_backup_restore_corrupt_bundle_is_rejected(
    init_vault: Path, tmp_path: Path,
):
    out = tmp_path / "cli_bundle.tar"
    _run([
        "backup", "create",
        "--output", str(out),
    ], init_vault)
    # Mutate the manifest inside the tar to break the sqlite sha256.
    import hashlib
    import io
    with tarfile.open(out, "r") as src_tar:
        members = [m for m in src_tar.getmembers()]
        manifest_bytes = src_tar.extractfile(
            "memory-agent-backup/manifest.json"
        ).read()
    payload = json.loads(manifest_bytes.decode("utf-8"))
    payload["sqlite_sha256"] = "0" * 64
    bad = tmp_path / "cli_bad.tar"
    with tarfile.open(out, "r") as src_tar:
        with tarfile.open(bad, "w") as dst_tar:
            for m in src_tar.getmembers():
                if m.name == "memory-agent-backup/manifest.json":
                    buf = json.dumps(payload).encode("utf-8")
                    m.size = len(buf)
                    dst_tar.addfile(m, io.BytesIO(buf))
                else:
                    dst_tar.addfile(m, src_tar.extractfile(m))
    target = tmp_path / "cli_bad_restored"
    res = _run([
        "backup", "restore",
        "--bundle", str(bad),
        "--target", str(target),
    ], init_vault)
    assert res["exit"] in (5, 6)
    code = (res["payload"].get("error") or {}).get("code")
    assert code in ("INTERNAL", "INVALID_REQUEST")


def test_privacy_rejects_forget_without_idempotency_key(init_vault: Path):
    res = _run([
        "privacy", "forget",
        "--kind", "memory",
        "--target", "mem_cli",
        # --idempotency-key omitted
    ], init_vault)
    # argparse itself rejects the missing required flag.
    assert res["exit"] in (2,)


# Imported here so the test file's imports stay tidy.
from typing import Any  # noqa: E402
