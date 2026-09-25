"""No-TEMP backup engine used by both GUI entry points."""

import hashlib
import json
import os
import shutil
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
    if normalized.startswith("/") or len(normalized) >= 2 and normalized[1] == ":":
        raise ValueError(f"안전하지 않은 ZIP 경로: {name}")

    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"안전하지 않은 ZIP 경로: {name}")
    if any(":" in part for part in parts):
        raise ValueError(f"안전하지 않은 ZIP 경로: {name}")

    return "/".join(parts)


def _copy_stream(source_handle, target_handle, cancel_event, work_callback):
    while True:
        if cancel_event.is_set():
            raise RuntimeError("백업이 취소되었습니다.")
        chunk = source_handle.read(CHUNK_SIZE)
        if not chunk:
            return
        target_handle.write(chunk)
        if work_callback is not None:
            work_callback(len(chunk))


def _estimate_zip_upper_bound(source_data: dict) -> int:
    source_size = sum(info["size"] for info in source_data["files"].values())
    metadata_bytes = sum(
        512 + (len(rel.encode("utf-8")) * 2)
        for rel in source_data["files"]
    )
    manifest_bytes = len(
        json.dumps(
            source_data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return source_size + metadata_bytes + manifest_bytes + RESERVE_BYTES


def cleanup_build_partials(app, destination: Path):
    if not destination.is_dir():
        return
    now = __import__("time").time()
    for item in destination.iterdir():
        if not item.name.startswith(BUILD_PREFIX):
            continue
        try:
            if (
                item.is_file()
                and now - item.stat().st_mtime > STALE_BUILD_SECONDS
            ):
                item.unlink(missing_ok=True)
        except OSError:
            pass


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
    expected_names = {
        _safe_archive_member(app, rel)
        for rel in expected
    }
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
            raise IOError(
                "ZIP 파일 누락: " + ", ".join(sorted(missing)[:5])
            )
        if extra:
            raise IOError(
                "manifest에 없는 ZIP 파일: " + ", ".join(sorted(extra)[:5])
            )

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
                    f"ZIP 크기 불일치: {rel} "
                    f"(expected={expected_size}, actual={info.file_size})"
                )

            digest = hashlib.sha256() if deep_scan else None
            with archive.open(info, "r") as handle:
                for chunk in iter(
                    lambda: handle.read(CHUNK_SIZE),
                    b"",
                ):
                    if cancel_event.is_set():
                        raise RuntimeError("ZIP 검증이 취소되었습니다.")
                    if digest is not None:
                        digest.update(chunk)
                    if work_callback is not None:
                        work_callback(len(chunk))

            if digest is not None:
                expected_hash = expected_info.get("sha256")
                if not expected_hash:
                    raise IOError(
                        f"정밀 검증용 SHA-256이 manifest에 없습니다: {rel}"
                    )
                actual_hash = digest.hexdigest()
                if expected_hash != actual_hash:
                    raise IOError(
                        f"ZIP SHA-256 불일치: {rel} "
                        f"(expected={expected_hash}, actual={actual_hash})"
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

    import app

    first_destination = destinations[0].resolve()
    first_destination.mkdir(parents=True, exist_ok=True)
    cleanup_build_partials(app, first_destination)

    archive_name = app.make_unique_archive_name(
        destinations,
        f"{base_name}.zip",
    )

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
            self.write_log(
                f"[INCREMENTAL] 기준 ZIP: {previous_archive.name}"
            )
            with zipfile.ZipFile(previous_archive, "r") as previous_zip:
                previous_names = {
                    _safe_archive_member(app, info.filename)
                    for info in previous_zip.infolist()
                    if not info.is_dir()
                    and info.filename != f"{app.MANIFEST_DIR}/{app.MANIFEST_FILE}"
                }
        else:
            self.write_log(
                "[INCREMENTAL] 기준 검증 ZIP 없음 → 원본에서 직접 구성"
            )
    else:
        self.write_log("[INCREMENTAL] OFF → 원본에서 직접 구성")

    required = _estimate_zip_upper_bound(source_data)
    free = shutil.disk_usage(first_destination).free
    if free < required:
        raise OSError(
            f"백업 대상 공간 부족: {first_destination} · "
            f"보수적 필요량 {required / (1024**3):.2f} GB · "
            f"현재 여유 {free / (1024**3):.2f} GB"
        )

    archive_path = first_destination / (
        f"{BUILD_PREFIX}{uuid.uuid4().hex}.zip.partial"
    )

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
            "source_bytes": sum(
                item["size"] for item in source_data["files"].values()
            ),
        },
    }

    previous_files = (
        previous_manifest.get("files", {})
        if previous_manifest
        else {}
    )
    copied = 0
    reused = 0

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

                for rel, info in source_data["files"].items():
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
                        and (
                            not deep_scan
                            or old.get("sha256") == info.get("sha256")
                        )
                        and member in previous_names
                    )

                    with out_zip.open(member, "w") as target_handle:
                        if can_reuse:
                            with previous_zip.open(member, "r") as source_handle:
                                _copy_stream(
                                    source_handle,
                                    target_handle,
                                    self.cancel_event,
                                    self._advance_eta_work,
                                )
                            reused += 1
                        else:
                            src = source / Path(rel)
                            if not src.is_file():
                                raise FileNotFoundError(
                                    f"원본 파일 없음: {rel}"
                                )
                            with src.open("rb") as source_handle:
                                _copy_stream(
                                    source_handle,
                                    target_handle,
                                    self.cancel_event,
                                    self._advance_eta_work,
                                )
                            copied += 1

                    self.advance_progress()

            finally:
                if previous_zip is not None:
                    previous_zip.close()

            manifest["stats"]["copied"] = copied
            manifest["stats"]["reused"] = reused
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")

            out_zip.writestr(
                f"{app.MANIFEST_DIR}/{app.MANIFEST_FILE}",
                manifest_bytes,
            )

        if self.cancel_event.is_set():
            raise RuntimeError("백업이 취소되었습니다.")

        self._set_operation(f"ZIP 무결성 검사 중 · {archive_name}")
        self._set_timeline_stage(4)
        archive_size = archive_path.stat().st_size
        self._set_eta_stage("ZIP 검증", archive_size)

        verify_zip_archive_strict(
            app,
            archive_path,
            manifest,
            self.advance_progress,
            self.cancel_event,
            deep_scan,
            self._advance_eta_work,
        )

        self.write_log(
            f"[ZIP READY] {archive_name} · "
            f"size={archive_size / (1024**3):.2f} GB · "
            f"copied={copied:,} reused={reused:,} · "
            f"location={first_destination}"
        )
        return archive_path, archive_name, None, manifest

    except Exception:
        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
    import app

    destination.mkdir(parents=True, exist_ok=True)
    app.cleanup_stale_partials(destination)

    target = destination / archive_name
    partial = destination / (
        f".parallel-backup.partial-{uuid.uuid4().hex}.zip"
    )

    try:
        if master_archive.resolve().parent == destination.resolve():
            if master_sha256:
                copied_sha256 = app.sha256_file(
                    master_archive,
                    cancel_event=self.cancel_event,
                )
                if copied_sha256 != master_sha256:
                    raise IOError(f"ZIP SHA-256 불일치: {destination}")
            os.replace(master_archive, target)
            self.advance_progress()
        else:
            required_bytes = master_archive.stat().st_size
            app.ensure_free_space(destination, required_bytes)

            self.write_log(
                f"[COPY] {master_archive.name} -> {destination}"
            )

            with (
                master_archive.open("rb") as source_handle,
                partial.open("wb") as target_handle,
            ):
                _copy_stream(
                    source_handle,
                    target_handle,
                    self.cancel_event,
                    self._advance_eta_work,
                )

            if partial.stat().st_size != master_archive.stat().st_size:
                raise IOError(f"ZIP 크기 불일치: {destination}")

            if deep_scan and master_sha256:
                copied_sha256 = app.sha256_file(
                    partial,
                    cancel_event=self.cancel_event,
                )
                if copied_sha256 != master_sha256:
                    raise IOError(f"ZIP SHA-256 불일치: {destination}")

            with zipfile.ZipFile(partial, "r") as archive:
                bad = archive.testzip()
            if bad is not None:
                raise IOError(
                    f"대상 ZIP CRC 오류: {destination} -> {bad}"
                )

            os.replace(partial, target)
            self.advance_progress()

        archives = app.list_verified_archives(
            destination,
            prefix,
            source,
        )
        for old_archive, _ in archives[keep:]:
            try:
                old_archive.unlink()
                self.write_log(
                    f"[RETENTION] 삭제: {old_archive.name}"
                )
            except OSError as exc:
                self.write_log(
                    f"[RETENTION FAIL] {old_archive.name} -> {exc}"
                )

        self.write_log(
            f"[COPY OK] {destination} -> {target.name}"
        )
        return {
            "ok": True,
            "destination": str(destination),
            "archive": str(target),
        }

    except Exception as exc:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        self.write_log(
            f"[COPY FAIL] {destination} -> {exc}"
        )
        return {
            "ok": False,
            "destination": str(destination),
            "error": str(exc),
        }
