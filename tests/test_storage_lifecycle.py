"""Real disk-backed lifecycle tests: small data, real ZIPs, no Windows TEMP staging."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import app
import backup_engine as engine
from diagnostics import DiagnosticLogger


class FakeApp:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.events = []
        self.lock = threading.Lock()

    def write_log(self, text):
        with self.lock:
            self.events.append(str(text))

    def _set_operation(self, *args):
        pass

    def _set_timeline_stage(self, *args, **kwargs):
        pass

    def _set_eta_stage(self, *args):
        pass

    def _advance_eta_work(self, *args):
        pass

    def advance_progress(self):
        pass


def prepare(tmp_path, deep_scan=False, count=12):
    source = tmp_path / "source"
    source.mkdir()
    for i in range(count):
        (source / f"file_{i:04d}.txt").write_bytes((f"file-{i}-" * 100).encode())
    target_dirs = [tmp_path / name for name in ("first", "second", "third")]
    for directory in target_dirs:
        directory.mkdir()
    data = app.build_source_manifest(
        source, deep_scan=deep_scan, exclude_patterns=[],
        cancel_event=threading.Event(),
    )
    fake = FakeApp()
    master, name, _staging, manifest = engine.build_master_zip(
        fake, source, "project_20261013_160000", target_dirs, data,
        False, deep_scan, False, [],
    )
    return fake, source, target_dirs, master, name, manifest


@pytest.mark.parametrize("deep_scan", [False, True])
def test_three_targets_parallel_verified_and_no_intermediate(tmp_path, deep_scan):
    fake, src, targets, master, name, _ = prepare(tmp_path, deep_scan)
    import app as _app
    digest = _app.sha256_file(master) if deep_scan else None
    with ThreadPoolExecutor(max_workers=2) as executor:
        remote = [
            executor.submit(
                engine.copy_master_archive,
                fake, master, name, path, src, "project_", 9, deep_scan, digest,
            )
            for path in targets[1:]
        ]
        assert all(future.result()["ok"] for future in remote)
    assert master.exists()
    first = engine.copy_master_archive(
        fake, master, name, targets[0], src, "project_", 9, deep_scan, digest,
    )
    assert first["ok"] is True
    assert not master.exists()
    outputs = [directory / name for directory in targets]
    assert all(file.is_file() for file in outputs)
    assert len({file.read_bytes() for file in outputs}) == 1
    for directory in targets:
        assert not list(directory.glob(".parallel-backup-build-*.zip.partial"))
        assert not list(directory.glob(".parallel-backup.partial-*.zip"))


def test_verification_failure_never_publishes_invalid_zip(tmp_path, monkeypatch):
    fake, src, destinations, master, name, _ = prepare(tmp_path)
    original = engine._verify_target_zip

    def fail_remote(obj, target, destination, deep_scan, sha):
        if destination == destinations[1]:
            raise OSError("injected verification failure")
        return original(obj, target, destination, deep_scan, sha)

    monkeypatch.setattr(engine, "_verify_target_zip", fail_remote)
    result = engine.copy_master_archive(
        fake, master, name, destinations[1], src, "project_", 9, False, None,
    )
    assert result["ok"] is False
    assert not (destinations[1] / name).exists()
    assert not list(destinations[1].glob(".parallel-backup.partial-*.zip"))
    assert master.exists()
    master.unlink()


def test_disk_write_failure_removes_partial(tmp_path, monkeypatch):
    fake, src, destinations, master, name, _ = prepare(tmp_path)
    def fake_write(source_handle, target_handle, cancel_event, work_callback):
        target_handle.write(b"partial-broken-data")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(engine, "_copy_stream", fake_write)
    result = engine.copy_master_archive(
        fake, master, name, destinations[2], src, "project_", 9, False, None,
    )
    assert result["ok"] is False
    assert not (destinations[2] / name).exists()
    assert not list(destinations[2].glob(".parallel-backup.partial-*.zip"))
    assert any("[PARTIAL CLEANUP]" in log for log in fake.events)
    master.unlink()


def test_cancel_copy_removes_partial(tmp_path, monkeypatch):
    fake, src, destinations, master, name, _ = prepare(tmp_path)
    def cancel_during_write(source_handle, target_handle, cancel_event, work_callback):
        target_handle.write(b"partial")
        cancel_event.set()
        raise RuntimeError("백업이 취소되었습니다.")
    monkeypatch.setattr(engine, "_copy_stream", cancel_during_write)
    result = engine.copy_master_archive(
        fake, master, name, destinations[1], src, "project_", 9, False, None,
    )
    assert result["ok"] is False
    assert fake.cancel_event.is_set()
    assert not list(destinations[1].glob(".parallel-backup.partial-*.zip"))
    assert not (destinations[1] / name).exists()
    master.unlink()


def test_cancelled_master_build_does_not_leave_partial(tmp_path):
    fake, src, directories, master, name, _ = prepare(tmp_path)
    master.unlink()
    data = app.build_source_manifest(
        src, deep_scan=False, exclude_patterns=[],
        cancel_event=threading.Event(),
    )
    fake.cancel_event.set()
    with pytest.raises(RuntimeError, match="취소"):
        engine.build_master_zip(
            fake, src, "cancel_20261013_160001", directories, data,
            False, False, False, [],
        )
    assert not list(directories[0].glob(".parallel-backup-build-*.zip.partial"))


def test_stale_build_partial_cleanup_counts_reclaimed_bytes(tmp_path):
    folder = tmp_path / "stale"
    folder.mkdir()
    stale = folder / ".parallel-backup-build-obsolete.zip.partial"
    stale.write_bytes(b"Z" * 4096)
    os.utime(stale, (time.time() - 90000, time.time() - 90000))
    recent = folder / ".parallel-backup-build-recent.zip.partial"
    recent.write_bytes(b"R" * 512)
    removed, bytes_freed = engine.cleanup_build_partials(app, folder)
    assert removed == 1
    assert bytes_freed == 4096
    assert not stale.exists()
    assert recent.exists()


def test_diagnostics_file_is_persistent_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    logger = DiagnosticLogger()
    path = logger.start("backup")
    assert path.parent == tmp_path / "AppData" / "ParallelBackup" / "logs"
    logger.write("[TARGET FAIL] E:\\backup injected")
    logger.write("[MASTER CLEANUP] bytes=123")
    result = path.read_text(encoding="utf-8")
    assert "[TARGET FAIL]" in result
    assert "[MASTER CLEANUP]" in result
    assert "python=" in result


def test_multi_target_lifecycle_order_enforced_in_app():
    code = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    assert code.index("for future in as_completed(future_map):") < code.index(
        "for destination in local_destinations:"
    )
    assert "all_remote_workers_finished=True" in code


def test_master_build_uses_no_system_temp(tmp_path, monkeypatch):
    temp_location = tmp_path / "system_temp"
    temp_location.mkdir()
    monkeypatch.setenv("TMP", str(temp_location))
    monkeypatch.setenv("TEMP", str(temp_location))
    fake, _src, destinations, master, _name, _ = prepare(tmp_path)
    assert master.parent == destinations[0]
    assert not list(temp_location.iterdir())
    master.unlink()


@pytest.mark.parametrize("corruption", [False, True])
def test_atomic_restore_preserves_existing_target_on_abort_or_bad_hash(tmp_path, corruption):
    import io
    import hashlib
    from backup_engine import restore_member_atomic

    target = tmp_path / "existing.txt"
    target.write_bytes(b"ORIGINAL SAFE DATA")
    data = b"replacement data" * 5000
    expected_hash = hashlib.sha256(data).hexdigest()
    if corruption:
        expected_hash = "0" * 64

    class InterruptibleReader:
        def __init__(self, source, cancel):
            self.source = source
            self.cancel = cancel
            self.calls = 0

        def read(self, amount):
            self.calls += 1
            result = self.source.read(min(amount, 100))
            if not corruption and self.calls == 1:
                self.cancel.set()
            return result

    cancel = threading.Event()
    reader = InterruptibleReader(io.BytesIO(data), cancel)
    with pytest.raises((RuntimeError, IOError)):
        restore_member_atomic(
            reader, target,
            {"size": len(data), "sha256": expected_hash},
            cancel,
        )
    assert target.read_bytes() == b"ORIGINAL SAFE DATA"
    assert not list(tmp_path.glob(".parallel-backup-restore-*.partial"))


def test_atomic_restore_success_replaces_file(tmp_path):
    import io
    import hashlib
    from backup_engine import restore_member_atomic

    target = tmp_path / "target.txt"
    target.write_bytes(b"old")
    data = b"new-data" * 1000
    size = restore_member_atomic(
        io.BytesIO(data), target,
        {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        threading.Event(),
    )
    assert size == len(data)
    assert target.read_bytes() == data
    assert not list(tmp_path.glob(".parallel-backup-restore-*.partial"))
