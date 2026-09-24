"""Stable launcher for Parallel Backup.

The GUI lives in app.py. This launcher patches the backup engine so large ZIP
construction never uses the system TEMP directory for a full staging copy.
ZIPs are built directly in the first backup destination as a hidden .partial
file, verified, and then atomically promoted/copies to the other destinations.
"""

import json
import os
import shutil
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import tkinter as tk

import app


BUILD_PREFIX = ".parallel-backup-build-"
BUILD_STALE_SECONDS = 24 * 60 * 60
RESERVE_BYTES = 64 * 1024 * 1024


def _cleanup_build_partials(destination: Path) -> None:
    if not destination.is_dir():
        return
    now = time.time()
    for item in destination.iterdir():
        if not item.name.startswith(BUILD_PREFIX):
            continue
        try:
            if now - item.stat().st_mtime > BUILD_STALE_SECONDS:
                if item.is_file():
                    item.unlink(missing_ok=True)
        except OSError:
            pass


def _safe_archive_member(name: str) -> str:
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise ValueError(f"안전하지 않은 ZIP 경로: {name}")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"안전하지 않은 ZIP 경로: {name}")
    return "/".join(parts)


def _copy_stream(source_handle, target_handle, cancel_event, work_callback):
    while True:
        if cancel_event.is_set():
            raise RuntimeError("ZIP 생성이 취소되었습니다.")
        chunk = source_handle.read(1024 * 1024)
        if not chunk:
            return
        target_handle.write(chunk)
        if work_callback is not None:
            work_callback(len(chunk))


def _estimate_archive_size(source_size: int, previous_archive: Path | None, previous_manifest: dict | None) -> int:
    if previous_archive and previous_manifest:
        old_source = previous_manifest.get("stats", {}).get("source_bytes")
        if old_source and old_source > 0:
            ratio = previous_archive.stat().st_size / old_source
            # Keep a conservative floor and cap the estimate at the source size.
            return max(256 * 1024 * 1024, min(source_size, int(source_size * ratio * 1.15)))
    return source_size


def build_master_zip_no_temp(
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
    del hardlink  # Hardlinks cannot reduce bytes inside a ZIP archive.

    if not destinations:
        raise ValueError("백업 대상이 없습니다.")

    first_destination = destinations[0].resolve()
    first_destination.mkdir(parents=True, exist_ok=True)
    _cleanup_build_partials(first_destination)

    archive_name = app.make_unique_archive_name(
        destinations,
        f"{base_name}.zip",
    )

    previous_archive = None
    previous_manifest = None
    if incremental:
        previous_archive, previous_manifest = app.find_latest_verified_archive(
            first_destination,
            f"{base_name.rsplit('_', 2)[0]}_",
            source,
        )
        if previous_archive is not None:
            self.write_log(f"[INCREMENTAL] 기준 ZIP: {previous_archive.name}")
        else:
            self.write_log("[INCREMENTAL] 기준 ZIP 없음 → 원본에서 직접 구성")

    source_size = sum(info["size"] for info in source_data["files"].values())
    estimated = _estimate_archive_size(source_size, previous_archive, previous_manifest)
    free = shutil.disk_usage(first_destination).free
    if free < estimated + RESERVE_BYTES:
        raise OSError(
            f"백업 대상 공간 부족: {first_destination} · "
            f"예상 ZIP {estimated / (1024**3):.2f} GB / "
            f"여유 {free / (1024**3):.2f} GB"
        )

    archive_path = first_destination / (
        f"{BUILD_PREFIX}{uuid.uuid4().hex}.zip.partial"
    )

    manifest = {
        "version": 4,
        "app_version": app.APP_VERSION,
        "source": str(source.resolve()),
        "created_at": datetime.now().isoformat(timespec="seconds"),
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
            "source_bytes": source_size,
        },
    }

    previous_files = previous_manifest.get("files", {}) if previous_manifest else {}
    reused = 0
    copied = 0

    self._set_operation(f"ZIP 직접 생성 중 · {archive_name}")
    self._set_timeline_stage(3)
    self._set_eta_stage("ZIP 압축", source_size)

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

                    member = _safe_archive_member(rel)
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
                        and member in previous_zip.namelist()
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
                                raise FileNotFoundError(f"원본 파일 없음: {rel}")
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
        app.verify_zip_archive(
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


def copy_master_archive_no_temp(
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
    destination.mkdir(parents=True, exist_ok=True)
    app.cleanup_stale_partials(destination)

    target = destination / archive_name
    partial = destination / (
        f".parallel-backup.partial-{uuid.uuid4().hex}.zip"
    )

    try:
        # The first destination already owns the verified build file.
        # Promote it directly instead of making a second full ZIP copy.
        if master_archive.resolve().parent == destination.resolve():
            if deep_scan:
                copied_sha256 = app.sha256_file(master_archive)
                if copied_sha256 != master_sha256:
                    raise IOError(f"ZIP SHA-256 불일치: {destination}")
            os.replace(master_archive, target)
            self.advance_progress()
        else:
            required_bytes = master_archive.stat().st_size
            app.ensure_free_space(destination, required_bytes)
            self.write_log(f"[COPY] {master_archive.name} -> {destination}")
            with master_archive.open("rb") as source_handle, partial.open("wb") as target_handle:
                while True:
                    if self.cancel_event.is_set():
                        raise RuntimeError("백업이 취소되었습니다.")
                    chunk = source_handle.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    target_handle.write(chunk)
                    self._advance_eta_work(len(chunk))
            if partial.stat().st_size != master_archive.stat().st_size:
                raise IOError(f"ZIP 크기 불일치: {destination}")
            if deep_scan:
                copied_sha256 = app.sha256_file(partial)
                if copied_sha256 != master_sha256:
                    raise IOError(f"ZIP SHA-256 불일치: {destination}")
            os.replace(partial, target)
            self.advance_progress()

        archives = app.list_verified_archives(destination, prefix, source)
        for old_archive, _ in archives[keep:]:
            try:
                old_archive.unlink()
                self.write_log(f"[RETENTION] 삭제: {old_archive.name}")
            except OSError as exc:
                self.write_log(f"[RETENTION FAIL] {old_archive.name} -> {exc}")

        self.write_log(f"[COPY OK] {destination} -> {target.name}")
        return {"ok": True, "destination": str(destination), "archive": str(target)}

    except Exception as exc:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        self.write_log(f"[COPY FAIL] {destination} -> {exc}")
        return {"ok": False, "destination": str(destination), "error": str(exc)}


# Install the new engine before the GUI is constructed.
app.ParallelBackupApp._build_master_zip = build_master_zip_no_temp
app.ParallelBackupApp._copy_master_archive = copy_master_archive_no_temp


def main():
    root = tk.Tk()
    app.ParallelBackupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
