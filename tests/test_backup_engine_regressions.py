from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "parallel_backup_launcher.py"
BAT = ROOT / "ParallelBackup.bat"


def test_large_backup_engine_does_not_stage_in_system_temp():
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "tempfile" not in source
    assert "mkdtemp" not in source
    assert "mkstemp" not in source
    assert "parallel-backup-stage-" not in source
    assert "parallel-backup-master-" not in source
    assert ".parallel-backup-build-" in source


def test_launcher_is_used_by_bat():
    source = BAT.read_text(encoding="utf-8")
    assert "parallel_backup_launcher.py" in source
    assert 'py -3 "%~dp0app.py"' not in source
    assert 'python "%~dp0app.py"' not in source


def test_partial_cleanup_is_explicit():
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "archive_path.unlink(missing_ok=True)" in source
    assert "partial.unlink(missing_ok=True)" in source
    assert "os.replace(master_archive, target)" in source


def test_zip_verification_checks_missing_and_extra_members():
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "missing = expected_names - actual_names" in source
    assert "extra = actual_names - expected_names" in source
    assert "archive.testzip()" in source
