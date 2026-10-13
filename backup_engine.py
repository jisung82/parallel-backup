"""Parallel Backup ZIP engine.

Design goals:
- Build one verified master ZIP directly in the first destination.
- Never rename/remove the master while other destination workers may still read it.
- Copy other destinations before finalizing the first destination with an atomic rename.
- Keep all ZIP data on the chosen destination volumes; never copy the source into TEMP.
- Keep detailed stage/target diagnostics in the activity log.
"""

import hashlib
import json
import os
import shutil
import traceback
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path


BUILD_PREFIX = ".parallel-backup-build-"
STALE_BUILD_SECONDS = 24 * 60 * 60
RESERVE_BYTES = 64 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


def _safe_archive_member(app, name: str) -> str:
    normalized = str(name).replace("\\", "/")
    if "\x00" in normalized or not normalized:
        raise ValueError(f"안전하지 않은 ZIP 경로: {name}")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ValueError(f"안전하지 않은 ZIP 경로: {name}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"안전하지 않은 ZIP 경로: {name}")
    if any(":" in part for part in parts):
        raise ValueError(f"안전하지 않은 ZIP 경로: {name}")
    return "/".join(parts)


def _get_app_module():
    return sys.modules.get("app") or sys.modules["__main__"]


def _log(self, message: str):
    self.write_log(message)


def _copy_stream(source_handle, target_handle, cancel_event, work_callback):
    total = 0
    while True:
        if cancel_event.is_set():
            raise RuntimeError("백업이 취소되었습니다.")
        chunk = source_handle.read(CHUNK_SIZE)
        if not chunk:
            return total
        target_handle.write(chunk)
        total += len(chunk)
        if work_callback is not None:
            work_callback(len(chunk))


def _estimate_zip_upper_bound(source_data: dict) -> int:
    source_size = sum(info["size"] for info in source_data["files"].values())
    metadata_bytes = sum(512 + (len(rel.encode("utf-8")) * 2) for rel in source_data["files"])
    manifest_bytes = len(json.dumps(source_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return source_size + metadata_bytes + manifest_bytes + RESERVE_BYTES


def cleanup_build_partials(app, destination: Path):
    if not destination.is_dir():
        return (0, 0)
    now = time.time()
    removed = 0
    freed_bytes = 0
    for item in destination.iterdir():
        if not (item.name.startswith(BUILD_PREFIX) and item.name.endswith(".zip.partial")):
            continue
        try:
            if item.is_file() and now - item.stat().st_mtime > STALE_BUILD_SECONDS:
                size = item.stat().st_size
                item.unlink(missing_ok=True)
                removed += 1
                freed_bytes += size
        except OSError:
            pass
    return removed, freed_bytes


def verify_zip_archive_strict(
    app,
    archive_path: Path,
    manifest: dict,
    progress_callback,
    cancel_event,
    deep_scan: bool,
    work_callback=None,
):
    """Verify all members in one decompression pass.

    ZipExtFile validates each member's CRC when read to EOF. Also verify that
    the embedded manifest actually matches the manifest we are trusting.
    """
    expected = manifest.get("files", {})
    expected_names = {_safe_archive_member(app, rel) for rel in expected}
    manifest_member = f"{app.MANIFEST_DIR}/{app.MANIFEST_FILE}"

    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        names = [_safe_archive_member(app, info.filename) for info in infos if not info.is_dir()]
        name_set = set(names)
        if len(names) != len(name_set):
            raise IOError("ZIP 내부 중복 파일명이 발견되었습니다.")
        if manifest_member not in name_set:
            raise FileNotFoundError("ZIP 내부 manifest.json이 없습니다.")

        missing = expected_names - (name_set - {manifest_member})
        extra = (name_set - {manifest_member}) - expected_names
        if missing:
            raise IOError("ZIP 파일 누락: " + ", ".join(sorted(missing)[:5]))
        if extra:
            raise IOError("manifest에 없는 ZIP 파일: " + ", ".join(sorted(extra)[:5]))

        for info in infos:
            if info.is_dir():
                continue
            if cancel_event.is_set():
                raise RuntimeError("ZIP 검증이 취소되었습니다.")
            rel = _safe_archive_member(app, info.filename)
            expected_info = expected.get(rel) if rel != manifest_member else None
            if rel != manifest_member and expected_info is None:
                raise IOError(f"manifest에 없는 ZIP 파일: {rel}")
            if expected_info is not None and info.file_size != expected_info.get("size"):
                raise IOError(f"ZIP 크기 불일치: {rel}")

            digest = hashlib.sha256() if deep_scan and expected_info is not None else None
            manifest_payload = bytearray() if rel == manifest_member else None
            uncompressed_bytes = 0
            # Read every member through EOF so zipfile checks CRC without a second pass.
            with archive.open(info, "r") as handle:
                while True:
                    if cancel_event.is_set():
                        raise RuntimeError("ZIP 검증이 취소되었습니다.")
                    chunk = handle.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    uncompressed_bytes += len(chunk)
                    if digest is not None:
                        digest.update(chunk)
                    if manifest_payload is not None:
                        if len(manifest_payload) + len(chunk) > 100 * 1024 * 1024:
                            raise IOError("ZIP manifest가 비정상적으로 큽니다.")
                        manifest_payload.extend(chunk)
                    if work_callback is not None:
                        work_callback(len(chunk))

            if uncompressed_bytes != info.file_size:
                raise IOError(f"ZIP 압축 해제 크기 불일치: {rel}")
            if manifest_payload is not None:
                try:
                    embedded = json.loads(manifest_payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise IOError("ZIP manifest JSON 손상") from exc
                if embedded != manifest:
                    raise IOError("ZIP 내부 manifest와 검사 기준 데이터가 다릅니다.")
            elif digest is not None:
                expected_hash = expected_info.get("sha256")
                if not expected_hash:
                    raise IOError(f"정밀 검증용 SHA-256이 manifest에 없습니다: {rel}")
                actual_hash = digest.hexdigest()
                if expected_hash != actual_hash:
                    raise IOError(f"ZIP SHA-256 불일치: {rel}")
            progress_callback()


def build_master_zip(
    self,
    source: Path,
    base_name: str,
    destinations: list[Path],
    source_data: dict,
    incremental: bool,
    deep_scan: bool,
    hardlink: bool,
    exclude_patterns,
):
    del hardlink
    if not destinations:
        raise ValueError("백업 대상이 없습니다.")

    app = _get_app_module()
    first_destination = destinations[0].resolve()
    first_destination.mkdir(parents=True, exist_ok=True)
    cleaned_count, cleaned_bytes = cleanup_build_partials(app, first_destination)
    if cleaned_count:
        _log(self, f"[CLEANUP] stale_builds={cleaned_count} recovered={cleaned_bytes / (1024**3):.2f} GiB volume={first_destination}")

    archive_name = app.make_unique_archive_name(destinations, f"{base_name}.zip")
    _log(self, f"[PLAN] master={first_destination / archive_name}")
    _log(self, f"[PLAN] multi_target={len(destinations)} partial_directory={first_destination}")
    _log(self, "[SPACE MODEL] Every snapshot is a FULL ZIP (even incremental); one compressed ZIP per destination per retained snapshot.")

    previous_archive = None
    previous_manifest = None
    previous_names = set()
    if incremental:
        previous_archive, previous_manifest = app.find_latest_verified_archive(
            first_destination,
            f"{base_name.rsplit('_', 2)[0]}_",
            source,
        )
        if previous_archive is not None:
            _log(self, f"[INCREMENTAL] 기준 ZIP: {previous_archive.name}")
            with zipfile.ZipFile(previous_archive, "r") as previous_zip:
                previous_names = {
                    _safe_archive_member(app, info.filename)
                    for info in previous_zip.infolist()
                    if not info.is_dir() and info.filename != f"{app.MANIFEST_DIR}/{app.MANIFEST_FILE}"
                }
            _log(self, f"[INCREMENTAL] 기준 멤버={len(previous_names):,}")
        else:
            _log(self, "[INCREMENTAL] 기준 검증 ZIP 없음 → 원본에서 직접 구성")
    else:
        _log(self, "[INCREMENTAL] OFF → 원본에서 직접 구성")

    required = _estimate_zip_upper_bound(source_data)
    free = shutil.disk_usage(first_destination).free
    _log(self, f"[DISK PREFLIGHT] target={first_destination} free={free / (1024**3):.2f} GiB uncompressed_upper_bound={required / (1024**3):.2f} GiB")
    if free < RESERVE_BYTES * 2:
        raise OSError(f"ZIP 생성 대상 여유 공간 부족: {first_destination}, free={free:,} bytes")
    if free < required:
        _log(self, "[DISK NOTICE] Worst-case uncompressed ZIP exceeds free space; allow streaming compression and abort/clean if volume becomes full.")

    archive_path = first_destination / f"{BUILD_PREFIX}{uuid.uuid4().hex}.zip.partial"
    manifest = {
        "version": 4,
        "app_version": app.APP_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(source.resolve()),
        "verified": True,
        "verification": "sha256" if deep_scan else "fast",
        "backup_name": Path(archive_name).stem,
        "archive_name": archive_name,
        "exclude_patterns": exclude_patterns,
        "files": source_data["files"],
        "directories": source_data["directories"],
        "stats": {
            "files": len(source_data["files"]),
            "copied": 0,
            "reused": 0,
            "source_bytes": sum(item["size"] for item in source_data["files"].values()),
        },
    }
    previous_files = previous_manifest.get("files", {}) if previous_manifest else {}
    copied = 0
    reused = 0
    total_files = len(source_data["files"])

    _log(self, f"[ZIP BEGIN] files={total_files:,} source={source} working_path={archive_path}")
    self._set_operation(f"ZIP 직접 생성 중 · {archive_name}")
    self._set_timeline_stage(3)
    self._set_eta_stage("ZIP 압축", manifest["stats"]["source_bytes"])

    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as out_zip:
            previous_zip = None
            try:
                if previous_archive is not None:
                    previous_zip = zipfile.ZipFile(previous_archive, "r")
                for index, (rel, info) in enumerate(source_data["files"].items(), 1):
                    if self.cancel_event.is_set():
                        raise RuntimeError("백업이 취소되었습니다.")
                    member = _safe_archive_member(app, rel)
                    old = previous_files.get(rel)
                    can_reuse = bool(
                        previous_zip is not None and old
                        and old.get("size") == info.get("size")
                        and old.get("mtime_ns") == info.get("mtime_ns")
                        and old.get("ctime_ns") == info.get("ctime_ns")
                        and (not deep_scan or old.get("sha256") == info.get("sha256"))
                        and member in previous_names
                    )
                    with out_zip.open(member, "w") as target_handle:
                        if can_reuse:
                            with previous_zip.open(member, "r") as source_handle:
                                _copy_stream(source_handle, target_handle, self.cancel_event, self._advance_eta_work)
                            reused += 1
                        else:
                            src = source / Path(rel)
                            if not src.is_file():
                                raise FileNotFoundError(f"원본 파일 없음: {rel}")
                            with src.open("rb") as source_handle:
                                _copy_stream(source_handle, target_handle, self.cancel_event, self._advance_eta_work)
                            copied += 1
                    self.advance_progress()
                    if index == 1 or index == total_files or index % 1000 == 0:
                        output_bytes = archive_path.stat().st_size
                        if index % 1000 == 0 and shutil.disk_usage(first_destination).free < RESERVE_BYTES:
                            raise OSError(f"ZIP 생성 중 저장공간 부족: {first_destination} (safety reserve={RESERVE_BYTES:,})")
                        if index == 1 or index == total_files or index % 5000 == 0:
                            _log(self, f"[ZIP PROGRESS] {index:,}/{total_files:,} copied={copied:,} reused={reused:,} partial_bytes={output_bytes:,}")
            finally:
                if previous_zip is not None:
                    previous_zip.close()
            manifest["stats"]["copied"] = copied
            manifest["stats"]["reused"] = reused
            out_zip.writestr(
                f"{app.MANIFEST_DIR}/{app.MANIFEST_FILE}",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )

        if self.cancel_event.is_set():
            raise RuntimeError("백업이 취소되었습니다.")
        archive_size = archive_path.stat().st_size
        _log(self, f"[ZIP BUILT] path={archive_path} size={archive_size / (1024**3):.2f} GB")
        self._set_operation(f"ZIP 무결성 검사 중 · {archive_name}")
        self._set_timeline_stage(4)
        self._set_eta_stage("ZIP 검증", archive_size)
        _log(self, f"[VERIFY BEGIN] archive={archive_path.name} mode={'SHA-256' if deep_scan else 'CRC+size'}")
        verify_zip_archive_strict(app, archive_path, manifest, self.advance_progress, self.cancel_event, deep_scan, self._advance_eta_work)
        _log(self, f"[VERIFY OK] archive={archive_path.name} files={total_files:,} deep_scan={deep_scan}")
        _log(self, f"[ZIP READY] {archive_name} · size={archive_size / (1024**3):.2f} GB · copied={copied:,} reused={reused:,} · location={first_destination}")
        return archive_path, archive_name, None, manifest
    except Exception:
        _log(self, f"[ZIP ERROR] path={archive_path} traceback={traceback.format_exc()}")
        try:
            size = archive_path.stat().st_size if archive_path.exists() else 0
            archive_path.unlink(missing_ok=True)
            _log(self, f"[ZIP CLEANUP] removed={archive_path} bytes={size:,}")
        except OSError as cleanup_exc:
            _log(self, f"[CLEANUP FAIL] path={archive_path} error={cleanup_exc}")
        raise


def _verify_target_zip(self, target: Path, destination: Path, deep_scan: bool, master_sha256: str | None):
    app = _get_app_module()
    if not target.is_file():
        raise FileNotFoundError(f"대상 ZIP 없음: {target}")
    size = target.stat().st_size
    _log(self, f"[TARGET CHECK] destination={destination} size={size / (1024**3):.2f} GiB")
    # One linear disk read: sha256 ensures both integrity and cross-target identity.
    # Non-deep mode checks CRC, which catches decompression errors.
    if deep_scan:
        if not master_sha256:
            raise IOError("정밀 검사에서 master SHA-256이 없습니다.")
        actual = app.sha256_file(target, cancel_event=self.cancel_event)
        if actual != master_sha256:
            raise IOError(f"대상 ZIP SHA-256 불일치: {destination}")
        _log(self, f"[TARGET SHA OK] destination={destination} sha256={actual}")
    else:
        checked_bytes = 0
        with zipfile.ZipFile(target, "r") as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                with archive.open(info, "r") as handle:
                    while True:
                        if self.cancel_event.is_set():
                            raise RuntimeError("대상 ZIP 검증이 취소되었습니다.")
                        chunk = handle.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        checked_bytes += len(chunk)
                # Reading each member to EOF checks its CRC.
        _log(self, f"[TARGET CRC OK] destination={destination} uncompressed_checked_bytes={checked_bytes:,}")


def copy_master_archive(
    self,
    master_archive: Path,
    archive_name: str,
    destination: Path,
    source: Path,
    prefix: str,
    keep: int,
    deep_scan: bool,
    master_sha256: str | None,
):
    """Called for remote destinations first; the master destination runs LAST."""
    app = _get_app_module()
    destination.mkdir(parents=True, exist_ok=True)
    app.cleanup_stale_partials(destination)
    target = destination / archive_name
    partial = destination / f".parallel-backup.partial-{uuid.uuid4().hex}.zip"
    source_is_local_master = master_archive.resolve().parent == destination.resolve()
    start = time.monotonic()
    committed = False
    try:
        if self.cancel_event.is_set():
            raise RuntimeError("백업이 취소되었습니다.")
        free = shutil.disk_usage(destination).free
        _log(self, f"[TARGET BEGIN] destination={destination} master={master_archive} local={source_is_local_master} free={free:,} target={target}")
        if source_is_local_master:
            # The caller must wait until all remote workers have finished.
            _log(self, f"[LOCAL VERIFY] master={master_archive} ready_for_commit=true")
            # The master was already verified after ZIP construction; do not decode it again.
            _log(self, "[LOCAL VERIFIED] earlier master ZIP verification already passed")
            if self.cancel_event.is_set():
                raise RuntimeError("백업이 취소되었습니다.")
            os.replace(master_archive, target)
            committed = True
            _log(self, f"[LOCAL COMMIT] atomic_rename={master_archive} -> {target} extra_disk_bytes=0")
        else:
            required = master_archive.stat().st_size
            _log(self, f"[COPY PREFLIGHT] destination={destination} free={free:,} needed={required + RESERVE_BYTES:,} partial={partial}")
            app.ensure_free_space(destination, required)
            _log(self, f"[COPY BEGIN] source={master_archive} partial={partial}")
            with master_archive.open("rb") as src, partial.open("wb") as dst:
                count = _copy_stream(src, dst, self.cancel_event, self._advance_eta_work)
            _log(self, f"[COPY WRITE OK] destination={destination} bytes={count:,} elapsed_s={time.monotonic() - start:.2f}")
            if partial.stat().st_size != required:
                raise IOError(f"대상 ZIP 크기 불일치: {destination}")
            # Validate BEFORE publishing the final archive; failed ZIP is never visible as final.
            _verify_target_zip(self, partial, destination, deep_scan, master_sha256)
            if self.cancel_event.is_set():
                raise RuntimeError("백업이 취소되었습니다.")
            os.replace(partial, target)
            committed = True
            _log(self, f"[COPY COMMIT] destination={destination} target={target}")

        archives = app.list_verified_archives(destination, prefix, source)
        for old_archive, _ in archives[keep:]:
            try:
                old_size = old_archive.stat().st_size
                old_archive.unlink()
                _log(self, f"[RETENTION] removed={old_archive} recovered_bytes={old_size:,}")
            except OSError as exc:
                _log(self, f"[RETENTION FAIL] archive={old_archive} error={exc}")
        _log(self, f"[TARGET OK] destination={destination} target={target} bytes={target.stat().st_size:,} elapsed_s={time.monotonic() - start:.2f}")
        return {"ok": True, "destination": str(destination), "archive": str(target)}
    except Exception as exc:
        # If commit already completed, the ZIP was verified before the rename.
        # Do not erase verified snapshots because of an unrelated retention error.
        _log(self, f"[TARGET FAIL] destination={destination} committed={committed} kind={type(exc).__name__} error={exc}")
        _log(self, f"[TARGET TRACEBACK] destination={destination}\n{traceback.format_exc()}")
        try:
            partial_size = partial.stat().st_size if partial.exists() else 0
            partial.unlink(missing_ok=True)
            if partial_size:
                _log(self, f"[PARTIAL CLEANUP] path={partial} recovered_bytes={partial_size:,}")
        except OSError as cleanup_exc:
            _log(self, f"[PARTIAL CLEANUP FAIL] path={partial} error={cleanup_exc}")
        return {"ok": committed, "destination": str(destination), "archive": str(target) if committed else None, "error": str(exc)}


def restore_member_atomic(input_handle, target: Path, info: dict, cancel_event, work_callback=None):
    """Restore one file via a same-directory partial; keep existing files on failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.parent / f".parallel-backup-restore-{uuid.uuid4().hex}.partial"
    expected_size = info.get("size")
    expected_hash = info.get("sha256")
    digest = hashlib.sha256() if expected_hash else None
    restored_bytes = 0
    try:
        with partial.open("wb") as out:
            while True:
                if cancel_event.is_set():
                    raise RuntimeError("복구가 취소되었습니다.")
                chunk = input_handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                restored_bytes += len(chunk)
                if digest is not None:
                    digest.update(chunk)
                if work_callback is not None:
                    work_callback(len(chunk))
        if expected_size is not None and restored_bytes != expected_size:
            raise IOError(f"복구 크기 불일치: {target}, expected={expected_size}, actual={restored_bytes}")
        if expected_hash and digest.hexdigest() != expected_hash:
            raise IOError(f"복구 SHA-256 불일치: {target}")
        if cancel_event.is_set():
            raise RuntimeError("복구가 취소되었습니다.")
        os.replace(partial, target)
        return restored_bytes
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
