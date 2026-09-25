from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
ENGINE = ROOT / "backup_engine.py"
LAUNCHER = ROOT / "parallel_backup_launcher.py"
BAT = ROOT / "ParallelBackup.bat"


def test_backup_engine_does_not_use_system_temp_staging():
    source = APP.read_text(encoding="utf-8")
    engine = ENGINE.read_text(encoding="utf-8")
    assert "tempfile" not in source
    assert "tempfile" not in engine
    assert "mkdtemp" not in source
    assert "mkstemp" not in source
    assert "parallel-backup-stage-" not in source
    assert "parallel-backup-master-" not in source
    assert ".parallel-backup-build-" in engine


def test_launcher_is_thin_and_shares_app_entry():
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "from app import main" in source
    assert "cancel_event" not in source


def test_bat_uses_python_launcher():
    source = BAT.read_text(encoding="utf-8")
    assert "parallel_backup_launcher.py" in source
    assert 'py -3 "%~dp0app.py"' not in source
    assert 'python "%~dp0app.py"' not in source


def test_partial_cleanup_is_explicit():
    source = ENGINE.read_text(encoding="utf-8")
    assert "archive_path.unlink(missing_ok=True)" in source
    assert "partial.unlink(missing_ok=True)" in source
    assert "os.replace(master_archive, target)" in source


def test_zip_verification_checks_missing_extra_crc_and_sha():
    source = ENGINE.read_text(encoding="utf-8")
    assert "missing = expected_names - actual_names" in source
    assert "extra = actual_names - expected_names" in source
    assert "archive.testzip()" in source
    assert "ZIP SHA-256 불일치" in source


def test_restore_path_is_hardened():
    source = APP.read_text(encoding="utf-8")
    assert "def _safe_restore_path" in source
    assert "commonpath" in source
    assert "_safe_archive_member(rel)" in source


def test_conservative_disk_preflight_exists():
    source = ENGINE.read_text(encoding="utf-8")
    assert "def _estimate_zip_upper_bound" in source
    assert "free < required" in source


def test_source_scan_supports_cancellation():
    source = APP.read_text(encoding="utf-8")
    assert "cancel_event=None" in source
    assert "원본 분석이 취소되었습니다." in source


def test_updater_replaces_engine_too():
    source = APP.read_text(encoding="utf-8")
    assert "UPDATE_LAUNCHER_URL" in source
    assert "parallel_backup_launcher.py" in source
