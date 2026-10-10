"""File-based infrastructure: segments, immutable objects, commits, locks.

All file writes go through here. The atomic pattern is:
    write to <name>.tmp -> fsync -> rename to <name>
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from memory_agent.contracts.events import ConversationEvent

__all__ = [
    "atomic_write_json",
    "atomic_write_text",
    "write_segment_file",
    "VaultLayout",
]


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of the directory so the rename is visible."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def atomic_write_json(target: Path, obj: Any) -> None:
    text = json.dumps(obj, ensure_ascii=False, sort_keys=False, indent=2)
    atomic_write_text(target, text)


def write_segment_file(
    *,
    target_dir: Path,
    source_file_id: str,
    generation: int,
    start_offset: int,
    end_offset: int,
    events: Iterable[ConversationEvent],
    salt: str | None = None,
) -> tuple[str, str, Path]:
    """Write an immutable segment file. Returns (segment_id, sha256, path).

    Layout: <vault>/sources/segments/<source_file_id>__gen<generation>__<start>_<end>.json

    ``salt`` disambiguates segments that would otherwise share an id
    even though they cover different ranges (most importantly:
    backfill iterates ``max_bytes_per_file`` windows over the same
    file, each producing a segment that may have the same offset
    span as a previous run's segment). The salt is mixed into the
    ``segment_id`` derivation only; the on-disk ``sha256`` stays
    content-addressed so two runs over the same content yield the
    same content fingerprint.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    seg_name = f"{source_file_id}__gen{generation}__{start_offset}_{end_offset}.json"
    target = target_dir / seg_name
    payload = {
        "segment_version": 1,
        "source_file_id": source_file_id,
        "generation": generation,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "events": [event.model_dump(mode="json") for event in events],
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=False, indent=2)
    atomic_write_text(target, text)
    import hashlib
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if salt:
        segment_id = hashlib.sha256(
            (salt + "|" + sha).encode("utf-8")
        ).hexdigest()[:16]
    else:
        segment_id = sha[:16]
    return segment_id, sha, target


class VaultLayout:
    """Strongly-typed paths inside a vault. v0.1 only."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config_path = root / "config.toml"
        self.state_dir = root / "state"
        self.state_db = self.state_dir / "state.sqlite"
        self.segments_dir = root / "sources" / "segments"
        self.objects_dir = root / "objects"
        self.memories_dir = self.objects_dir / "memories"
        self.commits_dir = root / "commits"
        self.head_path = root / "HEAD.json"
        self.artifacts_dir = root / "artifacts"
        self.candidates_dir = self.artifacts_dir / "candidates"
        self.batches_dir = self.artifacts_dir / "batches"
        self.privacy_head = root / "privacy" / "HEAD.json"
        for d in (
            self.state_dir, self.segments_dir, self.memories_dir,
            self.commits_dir, self.candidates_dir, self.batches_dir,
            self.objects_dir / "privacy",
        ):
            d.mkdir(parents=True, exist_ok=True)

    def memory_revision_path(self, memory_id: str, revision: int) -> Path:
        return self.memories_dir / memory_id / f"r{revision:04d}.md"

    def commit_manifest_path(self, commit_id: str) -> Path:
        return self.commits_dir / commit_id / "manifest.json"