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


def test_restore_path_rejects_escape(tmp_path):
    from app import _safe_restore_path

    target = tmp_path / "restore"
    target.mkdir()

    for bad in ("../outside.txt", "/outside.txt", "C:/outside.txt", "a/../../outside.txt"):
        try:
            _safe_restore_path(target, bad)
        except ValueError:
            continue
        raise AssertionError(f"path traversal accepted: {bad}")


def test_strict_zip_verifier_detects_extra_member(tmp_path):
    import threading
    import zipfile
    import json
    import app
    from backup_engine import verify_zip_archive_strict

    archive = tmp_path / "extra.zip"
    manifest = {
        "version": 4,
        "files": {
            "ok.txt": {
                "size": 2,
                "sha256": "2689367b205c16ce4303b3f1f8a8d8b7b2c8d3c7c5a3e8f6a6d8e0a5f8d0d5d"
            }
        }
    }
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ok.txt", "ok")
        zf.writestr("extra.txt", "x")
        zf.writestr(
            ".parallel-backup/manifest.json",
            json.dumps(manifest),
        )

    try:
        verify_zip_archive_strict(
            app,
            archive,
            manifest,
            lambda: None,
            threading.Event(),
            False,
            None,
        )
    except IOError as exc:
        assert "manifest에 없는 ZIP 파일" in str(exc)
    else:
        raise AssertionError("extra ZIP member was not rejected")


def test_no_temp_engine_builds_directly_into_destination(tmp_path):
    import threading
    import app
    from backup_engine import build_master_zip

    class Dummy:
        def __init__(self):
            self.cancel_event = threading.Event()
            self.events = []

        def write_log(self, message):
            self.events.append(message)

        def _set_operation(self, status):
            self.events.append(status)

        def _set_timeline_stage(self, stage, **kwargs):
            pass

        def _set_eta_stage(self, name, total):
            pass

        def _advance_eta_work(self, amount):
            pass

        def advance_progress(self):
            pass

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "hello.txt").write_text("hello", encoding="utf-8")

    cancel_event = threading.Event()
    source_data = app.build_source_manifest(
        source,
        deep_scan=True,
        exclude_patterns=[],
        cancel_event=cancel_event,
    )

    dummy = Dummy()
    archive_path, archive_name, staging_root, manifest = build_master_zip(
        dummy,
        source,
        "test_20260926_000000",
        [destination],
        source_data,
        False,
        True,
        False,
        [],
    )

    assert archive_path.parent.resolve() == destination.resolve()
    assert archive_path.name.startswith(".parallel-backup-build-")
    assert archive_path.suffix == ".partial"
    assert archive_path.is_file()
    assert staging_root is None
    assert manifest["verified"] is True
    archive_path.unlink()
