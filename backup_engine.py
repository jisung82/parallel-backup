"""Parallel Backup ZIP engine.

Design goals:
- Build one verified master ZIP directly in the first destination.
- Never rename/remove the master while other destination workers may still read it.
- Finalize the first destination with a hard link so the master source remains readable.
- Keep detailed stage/target diagnostics in the activity log.
"""

import hashlib
import json
import os
import shutil
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
    metadata_bytes = sum(
        512 + (len(rel.encode("utf-8")) * 2)
        for rel in source_data["files"]
    )
    manifest_bytes = len(
        json.dumps(source_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return source_size + metadata_bytes + manifest_bytes + RESERVE_BYTES


def cleanup_build_partials(app, destination: Path):
    if not destination.is_dir():
        return
    now = time.time()
    removed = 0
    for item in destination.iterdir():
        if not item.name.startswith(BUILD_PREFIX):
            continue
        try:
            if item.is_file() and now - item.stat().st_mtime > STALE_BUILD_SECONDS:
                item.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    if removed:
        # Cleanup is intentionally quiet for normal runs; callers log the actual build path.
        return removed
    return 0


def verify_zip_archive_strict(
    app,
    archive_path: Path,
    manifest: dict,
    progress_callback,
    cancel_event,
    deep_scan: bool,
    work_callback=None,
):
    expected = manifest.get("files", {})
    expected_names = {_safe_archive_member(app, rel) for rel in expected}
    manifest_member = f"{app.MANIFEST_DIR}/{app.MANIFEST_FILE}"

    with zipfile.ZipFile(archive_path, "r") as archive:
        bad = archive.testzip()
        if bad is not None:
            raise IOError(f"ZIP CRC 오류: {bad}")

        infos = archive.infolist()
        names = [
            _safe_archive_member(app, info.filename)
            for info in infos
            if not info.is_dir()
        ]
        name_set = set(names)

        if len(names) != len(name_set):
            raise IOError("ZIP 내부 중복 파일명이 발견되었습니다.")
        if manifest_member not in name_set:
            raise FileNotFoundError("ZIP 내부 manifest.json이 없습니다.")

        actual_names = name_set - {manifest_member}
        missing = expected_names - actual_names
        extra = actual_names - expected_names
        if missing:
            raise IOError("ZIP 파일 누락: " + ", ".join(sorted(missing)[:5]))
        if extra:
            raise IOError("manifest에 없는 ZIP 파일: " + ", ".join(sorted(extra)[:5]))

        for info in infos:
            if info.is_dir() or info.filename == manifest_member:
                continue
            if cancel_event.is_set():
                raise RuntimeError("ZIP 검증이 취소되었습니다.")

            rel = _safe_archive_member(app, info.filename)
            expected_info = expected.get(rel)
            if expected_info is None:
                raise IOError(f"manifest에 없는 ZIP 파일: {rel}")
            expected_size = expected_info.get("size")
            if expected_size is not None and info.file_size != expected_size:
                raise IOError(
                    f"ZIP 크기 불일치: {rel} (expected={expected_size}, actual={info.file_size})"
                )

            digest = hashlib.sha256() if deep_scan else None
            with archive.open(info, "r") as handle:
                for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                    if cancel_event.is_set():
                        raise RuntimeError("ZIP 검증이 취소되었습니다.")
                    if digest is not None:
                        digest.update(chunk)
                    if work_callback is not None:
                        work_callback(len(chunk))

            if digest is not None:
                expected_hash = expected_info.get("sha256")
                if not expected_hash:
                    raise IOError(f"정밀 검증용 SHA-256이 manifest에 없습니다: {rel}")
                actual_hash = digest.hexdigest()
                if expected_hash != actual_hash:
                    raise IOError(
                        f"ZIP SHA-256 불일치: {rel} (expected={expected_hash}, actual={actual_hash})"
                    )
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
    cleaned = cleanup_build_partials(app, first_destination)
    if cleaned:
        _log(self, f"[CLEANUP] 오래된 ZIP build partial {cleaned}개 삭제")

    archive_name = app.make_unique_archive_name(destinations, f"{base_name}.zip")
    _log(self, f"[PLAN] master={first_destination / archive_name}")
    _log(self, f"[PLAN] build={first_destination / (BUILD_PREFIX + '...zip.partial')}")

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
                    if not info.is_dir()
                    and info.filename != f"{app.MANIFEST_DIR}/{app.MANIFEST_FILE}"
                }
            _log(self, f"[INCREMENTAL] 기준 멤버={len(previous_names):,}")
        else:
            _log(self, "[INCREMENTAL] 기준 검증 ZIP 없음 → 원본에서 직접 구성")
    else:
        _log(self, "[INCREMENTAL] OFF → 원본에서 직접 구성")

    required = _estimate_zip_upper_bound(source_data)
    free = shutil.disk_usage(first_destination).free
    _log(
        self,
        f"[DISK PREFLIGHT] target={first_destination} free={free / (1024**3):.2f} GB "
        f"estimated_need<={required / (1024**3):.2f} GB",
    )
    if free < required:
        raise OSError(
            f"백업 대상 공간 부족: {first_destination} · "
            f"보수적 필요량 {required / (1024**3):.2f} GB · "
            f"현재 여유 {free / (1024**3):.2f} GB"
        )

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
    _log(self, f"[ZIP BEGIN] files={total_files:,} source={source}")
    self._set_operation(f"ZIP 직접 생성 중 · {archive_name}")
    self._set_timeline_stage(3)
    self._set_eta_stage("ZIP 압축", manifest["stats"]["source_bytes"])

    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as out_zip:
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
                        previous_zip is not None
                        and old
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
                    if index == 1 or index == total_files or index % 5000 == 0:
                        _log(self, f"[ZIP PROGRESS] {index:,}/{total_files:,} copied={copied:,} reused={reused:,}")
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
        verify_zip_archive_strict(
            app,
            archive_path,
            manifest,
            self.advance_progress,
            self.cancel_event,
            deep_scan,
            self._advance_eta_work,
        )
        _log(self, f"[VERIFY OK] archive={archive_path.name} files={total_files:,} deep_scan={deep_scan}")
        _log(
            self,
            f"[ZIP READY] {archive_name} · size={archive_size / (1024**3):.2f} GB · "
            f"copied={copied:,} reused={reused:,} · location={first_destination}",
        )
        return archive_path, archive_name, None, manifest
    except Exception:
        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _verify_target_zip(self, target: Path, destination: Path, deep_scan: bool, master_sha256: str | None):
    app = _get_app_module()
    if not target.is_file():
        raise FileNotFoundError(f"대상 ZIP 없음: {target}")
    size = target.stat().st_size
    _log(self, f"[TARGET CHECK] destination={destination} size={size / (1024**3):.2f} GB")
    with zipfile.ZipFile(target, "r") as archive:
        bad = archive.testzip()
    if bad is not None:
        raise IOError(f"대상 ZIP CRC 오류: {destination} -> {bad}")
    _log(self, f"[TARGET CRC OK] destination={destination}")
    if deep_scan and master_sha256:
        _log(self, f"[TARGET SHA BEGIN] destination={destination}")
        copied_sha256 = app.sha256_file(target, cancel_event=self.cancel_event)
        if copied_sha256 != master_sha256:
            raise IOError(
                f"ZIP SHA-256 불일치: {destination} "
                f"(expected={master_sha256}, actual={copied_sha256})"
            )
        _log(self, f"[TARGET SHA OK] destination={destination} sha256={copied_sha256}")


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
    app = _get_app_module()
    destination.mkdir(parents=True, exist_ok=True)
    app.cleanup_stale_partials(destination)
    target = destination / archive_name
    partial = destination / f".parallel-backup.partial-{uuid.uuid4().hex}.zip"
    source_is_local_master = master_archive.resolve().parent == destination.resolve()

    try:
        _log(self, f"[TARGET BEGIN] destination={destination} source_is_master={source_is_local_master}")
        if source_is_local_master:
            # CRITICAL: never os.replace(master_archive, target) here.
            # Other worker threads may still be reading master_archive.
            if master_sha256:
                _log(self, f"[MASTER SHA CHECK] local master={master_archive.name}")
                actual = app.sha256_file(master_archive, cancel_event=self.cancel_event)
                if actual != master_sha256:
                    raise IOError(f"마스터 ZIP SHA-256 불일치: {destination}")
                _log(self, f"[MASTER SHA OK] {actual}")

            local_link = destination / f".parallel-backup-finalize-{uuid.uuid4().hex}.zip"
            _log(self, f"[LOCAL FINALIZE] hardlink {master_archive.name} -> {target.name}")
            try:
                os.link(master_archive, local_link)
                os.replace(local_link, target)
                _log(self, "[LOCAL FINALIZE OK] hardlink created; master partial remains readable")
            except OSError as exc:
                try:
                    local_link.unlink(missing_ok=True)
                except OSError:
                    pass
                raise OSError(
                    f"첫 번째 백업 대상의 안전한 ZIP 확정에 실패했습니다. "
                    f"master partial은 유지되었습니다: {exc}"
                ) from exc
            self._verify_target_zip(self, target, destination, deep_scan, master_sha256)
            self.advance_progress()
        else:
            required_bytes = master_archive.stat().st_size
            free = shutil.disk_usage(destination).free
            _log(self, f"[COPY PREFLIGHT] destination={destination} free={free / (1024**3):.2f} GB need={required_bytes / (1024**3):.2f} GB")
            app.ensure_free_space(destination, required_bytes)
            _log(self, f"[COPY BEGIN] {master_archive.name} -> {destination} partial={partial.name}")
            copied_bytes = 0
            started = time.monotonic()
            with master_archive.open("rb") as source_handle, partial.open("wb") as target_handle:
                copied_bytes = _copy_stream(source_handle, target_handle, self.cancel_event, self._advance_eta_work)
            elapsed = max(0.001, time.monotonic() - started)
            _log(self, f"[COPY WRITE OK] destination={destination} bytes={copied_bytes:,} rate={(copied_bytes / elapsed) / (1024**2):.2f} MB/s")
            if partial.stat().st_size != master_archive.stat().st_size:
                raise IOError(f"ZIP 크기 불일치: {destination}")
            os.replace(partial, target)
            _log(self, f"[COPY COMMIT] destination={destination} target={target.name}")
            self._verify_target_zip(self, target, destination, deep_scan, master_sha256)
            self.advance_progress()

        archives = app.list_verified_archives(destination, prefix, source)
        for old_archive, _ in archives[keep:]:
            try:
                old_archive.unlink(missing_ok=True)
                _log(self, f"[RETENTION] 삭제: {old_archive.name}")
            except OSError as exc:
                _log(self, f"[RETENTION FAIL] {old_archive.name} -> {exc}")

        _log(self, f"[TARGET OK] destination={destination} target={target.name}")
        return {"ok": True, "destination": str(destination), "archive": str(target)}
    except Exception as exc:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        _log(self, f"[TARGET FAIL] destination={destination} error={type(exc).__name__}: {exc}")
        return {"ok": False, "destination": str(destination), "error": str(exc)}
