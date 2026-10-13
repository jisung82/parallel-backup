"""Bounded, persistent, thread-safe diagnostic logs for Parallel Backup."""
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

MAX_LOG_FILES = 20
MAX_LOG_BYTES = 32 * 1024 * 1024
MAX_SINGLE_LOG_BYTES = 8 * 1024 * 1024


def diagnostic_directory():
    base = os.environ.get("LOCALAPPDATA")
    if base:
        location = Path(base) / "ParallelBackup" / "logs"
    else:
        location = Path.home() / ".parallel-backup" / "logs"
    location.mkdir(parents=True, exist_ok=True)
    return location


class DiagnosticLogger:
    """Best-effort logs: logging errors must never interrupt a backup."""

    def __init__(self):
        self._lock = threading.Lock()
        self.path = None

    def start(self, operation):
        with self._lock:
            try:
                folder = diagnostic_directory()
                self._prune(folder)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                safe_type = "".join(c for c in operation if c.isalnum() or c in "-_")[:30] or "job"
                self.path = folder / f"{safe_type}_{stamp}.log"
                self.path.touch(exist_ok=False)
                self._write_unlocked(f"=== {operation} started {datetime.now().isoformat()} ===")
                self._write_unlocked(f"python={sys.version.split()[0]} executable={sys.executable} pid={os.getpid()}")
            except OSError:
                self.path = None
            return self.path

    def _write_unlocked(self, message):
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write(
                f"{datetime.now().isoformat(timespec='milliseconds')} "
                f"[{threading.current_thread().name}] {message}\n"
            )

    def write(self, message):
        with self._lock:
            try:
                if self.path is not None and self.path.stat().st_size < MAX_SINGLE_LOG_BYTES:
                    self._write_unlocked(str(message))
            except OSError:
                pass

    @staticmethod
    def _prune(folder):
        logs = sorted(
            (p for p in folder.glob("*.log") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        total = 0
        for index, path in enumerate(logs):
            try:
                total += path.stat().st_size
                if index >= MAX_LOG_FILES or total > MAX_LOG_BYTES:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
