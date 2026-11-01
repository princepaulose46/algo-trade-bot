"""Crash-safe JSON and JSON Lines storage with atomic writes and recovery."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("reporting.json_store")


class SafeJsonStore:
    """Manages atomic JSON overwrites and crash-resilient JSONL append-only logs."""

    def __init__(self, base_dir: str = "./reports") -> None:
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        # In-memory deduplication set: (symbol, candle_timestamp_ms, event_type)
        self._seen_events: Set[Tuple[str, int, str]] = set()

    def resolve_path(self, filename: str) -> str:
        if os.path.isabs(filename):
            return filename
        return os.path.join(self.base_dir, filename)

    def is_duplicate_event(self, symbol: str, timestamp_ms: int, event_type: str) -> bool:
        key = (symbol, timestamp_ms, event_type)
        if key in self._seen_events:
            return True
        self._seen_events.add(key)
        return False

    def append_jsonl(self, filename: str, record: Dict[str, Any]) -> None:
        """Append a JSON record as a single line with immediate flush and sync."""
        filepath = self.resolve_path(filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        line = json.dumps(record, separators=(",", ":")) + "\n"
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def atomic_write_json(self, filename: str, data: Any) -> None:
        """Write JSON data to a temporary file and atomically replace destination.

        Section 11: 'Write safely: write a full temporary file, flush it,
        then atomically replace the destination.'
        """
        filepath = self.resolve_path(filename)
        dir_name = os.path.dirname(filepath)
        os.makedirs(dir_name, exist_ok=True)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_", suffix=".json")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, filepath)
        except Exception as exc:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            logger.error("Atomic write failed for %s: %s", filepath, exc)
            raise

    def read_jsonl_safely(self, filename: str) -> List[Dict[str, Any]]:
        """Read JSONL file line by line.

        Section 11: 'Validate existing reports on startup; with JSONL,
        recover from the last valid line.'
        """
        filepath = self.resolve_path(filename)
        if not os.path.exists(filepath):
            return []

        valid_records: List[Dict[str, Any]] = []
        corrupted_lines_found = False

        with open(filepath, "r", encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, start=1):
                clean_line = raw_line.strip()
                if not clean_line:
                    continue
                try:
                    obj = json.loads(clean_line)
                    if isinstance(obj, dict):
                        valid_records.append(obj)
                    else:
                        corrupted_lines_found = True
                        logger.warning("Non-dict line in %s at %d", filepath, line_no)
                except json.JSONDecodeError as exc:
                    corrupted_lines_found = True
                    logger.warning("Corrupted JSON line in %s at %d: %s. Recovering up to valid line.", filepath, line_no, exc)

        if corrupted_lines_found:
            logger.info("Auto-repairing corrupted lines in %s by rewriting %d valid records.", filepath, len(valid_records))
            self.rewrite_jsonl(filename, valid_records)

        return valid_records

    def rewrite_jsonl(self, filename: str, records: List[Dict[str, Any]]) -> None:
        """Atomically rewrite an entire JSONL file with sanitized records."""
        filepath = self.resolve_path(filename)
        dir_name = os.path.dirname(filepath)
        os.makedirs(dir_name, exist_ok=True)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_", suffix=".jsonl")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, filepath)
        except Exception as exc:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise exc

    def read_json_safely(self, filename: str, default: Any = None) -> Any:
        """Safely read JSON file returning default on missing or malformed content."""
        filepath = self.resolve_path(filename)
        if not os.path.exists(filepath):
            return default
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Failed to load JSON file %s: %s", filepath, exc)
            return default

