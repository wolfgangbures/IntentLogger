"""Rotating, local-only JSONL storage; all methods run in an executor."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

_LOG_NAME = re.compile(r"intent_logger-(\d{8}T\d{12})-[0-9a-f]{32}\.jsonl")


class LogStore:
    """Keep a bounded set of chronological log segments in a private directory."""

    def __init__(
        self, directory: Path, retention_days: int, max_file_mb: int, max_files: int
    ) -> None:
        """Configure retention and storage bounds."""
        self.directory = directory
        self.retention_days = retention_days
        self.max_bytes = max_file_mb * 1024 * 1024
        self.max_files = max_files
        self._active: Path | None = None

    def prepare(self) -> None:
        """Create the directory and enforce retention at startup."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cleanup()

    def cleanup(self) -> None:
        """Expire only segments owned by this integration, including idle logs."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self.retention_days)
        segments: list[Path] = []
        for path in self.directory.iterdir():
            if not (match := _LOG_NAME.fullmatch(path.name)) or path.is_symlink():
                continue
            created = datetime.strptime(match[1], "%Y%m%dT%H%M%S%f").replace(tzinfo=UTC)
            if created <= cutoff:
                path.unlink()
                if path == self._active:
                    self._active = None
            else:
                segments.append(path)
        segments.sort()
        for path in segments[: max(0, len(segments) - self.max_files)]:
            path.unlink()
            if path == self._active:
                self._active = None

    def append(self, record: dict[str, Any]) -> None:
        """Append one complete JSON record, rotating before crossing the limit."""
        self.cleanup()
        line = (json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n").encode()
        if len(line) > self.max_bytes:
            raise ValueError("Intent Logger record exceeds the configured segment size")
        if self._active is None or (
            self._active.stat().st_size + len(line) > self.max_bytes
        ):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            self._active = self.directory / f"intent_logger-{stamp}-{uuid4().hex}.jsonl"
            self._active.touch(mode=0o600, exist_ok=False)
            self.cleanup()
        with self._active.open("ab") as stream:
            stream.write(line)
