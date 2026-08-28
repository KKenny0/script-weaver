"""Immutable content-addressed media ingestion."""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from .sqlite import default_data_dir


class MediaStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_data_dir() / "media"
        self.staging = self.root / "staging"
        self.objects = self.root / "objects"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(parents=True, exist_ok=True)

    def ingest(self, staged_path: str | Path) -> tuple[str, str]:
        source = Path(staged_path).resolve()
        staging = self.staging.resolve()
        if staging not in source.parents or not source.is_file():
            raise ValueError("media must be a staging file")
        digest = self._hash_file(source)
        target = self.objects / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # An object already claims this digest: verify it, never trust exists().
            existing = self._hash_file(target)
            if existing != digest:
                raise ValueError(f"content-addressed object is corrupt: {target}")
            return str(target), digest
        self._publish_atomically(source, target, digest)
        return str(target), digest

    @staticmethod
    def _hash_file(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        return hasher.hexdigest()

    def _publish_atomically(self, source: Path, target: Path, digest: str) -> None:
        """Write to a random sibling temp file, fsync, then atomically move into place.

        A concurrent publish of the same digest lands on the same final path with a
        complete object either way because os.replace is atomic.
        """
        temp = target.with_name(f".{digest}.{secrets.token_hex(8)}.tmp")
        try:
            with source.open("rb") as reader, temp.open("wb") as writer:
                for block in iter(lambda: reader.read(1024 * 1024), b""):
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temp, target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        if self._hash_file(target) != digest:
            raise ValueError(f"published object does not match its digest: {target}")
