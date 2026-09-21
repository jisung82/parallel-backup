import hashlib
import json
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Parallel Backup"
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
MANIFEST_DIR = ".parallel-backup"
MANIFEST_FILE = "manifest.json"
SOURCE_CACHE_FILE = "source_cache.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    path = Path(base) / "ParallelBackup"
    path.mkdir(parents=True, exist_ok=True)
    return path / SOURCE_CACHE_FILE


def load_source_cache() -> dict:
    path = source_cache_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "sources": {}}


def save_source_cache(cache: dict):
    path = source_cache_path()
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(cache, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def build_source_manifest(source: Path, use_cache: bool = True):
    files = {}
    directories = []
    cache_hits = 0
    cache_misses = 0

    cache = load_source_cache() if use_cache else {"version": 1, "sources": {}}
    source_key = str(source.resolve())
    cached_files = cache.setdefault("sources", {}).setdefault(source_key, {}).get(
        "files", {}
    )

    for root, dirnames, filenames in os.walk(source):
        root_path = Path(root)
        rel_root = root_path.relative_to(source)
        if str(rel_root) != ".":
            directories.append(rel_root.as_posix())

        for filename in filenames:
            src = root_path / filename
            rel = src.relative_to(source).as_posix()
            stat = src.stat()

            cached = cached_files.get(rel)
            cache_valid = bool(
                use_cache
                and cached
                and cached.get("sha256")
                and cached.get("size") == stat.st_size
                and cached.get("mtime_ns") == stat.st_mtime_ns
                and cached.get("ctime_ns") == stat.st_ctime_ns
            )

            if cache_valid:
                file_hash = cached["sha256"]
                cache_hits += 1
            else:
                file_hash = sha256_file(src)
                cache_misses += 1

            files[rel] = {
                "sha256": file_hash,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
            }

    if use_cache:
        cache["sources"][source_key] = {
            "files": files,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_source_cache(cache)

    return {
        "files": files,
        "directories": sorted(set(directories)),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
    }


def find_latest_verified_backup(destination: Path, backup_name_prefix: str, source: Path):
    candidates = [
        item for item in destination.iterdir()
        if item.is_dir()
        and item.name.startswith(backup_name_prefix)
        and item.name != backup_name_prefix
    ]
    candidates.sort(key=lambda item: item.name, reverse=True)

    source_text = str(source.resolve())

    for candidate in candidates:
        manifest_path = candidate / MANIFEST_DIR / MANIFEST_FILE
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if (
            manifest.get("source") == source_text
            and manifest.get("verified") is True
            and manifest.get("version") == 2
        ):
            return candidate, manifest

    return None, None


def write_manifest(target: Path, manifest: dict):
    manifest_dir = target / MANIFEST_DIR
    manifest_dir.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_dir / "manifest.tmp"
    final_path = manifest_dir / MANIFEST_FILE
    temp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_path, final_path)


def ensure_directory_tree(source: Path, target: Path):
    target.mkdir(parents=True, exist_ok=False)
    for root, dirnames, _ in os.walk(source):
        rel_root = Path(root).relative_to(source)
        for dirname in dirnames:
            (target / rel_root / dirname).mkdir(parents=True, exist_ok=True)


def copy_or_link(
    source_file: Path,
    target_file: Path,
    previous_file: Path | None,
    can_reuse: bool,
):
    target_file.parent.mkdir(parents=True, exist_ok=True)

    if can_reuse and previous_file is not None:
        try:
            os.link(previous_file, target_file)
            return "hardlink"
        except OSError:
            pass

    shutil.copy2(source_file, target_file)
    return "copy"


def verify_backup(target: Path, manifest: dict, progress_callback):
    for rel, info in manifest["files"].items():
        path = target / Path(rel)
        if not path.is_file():
            raise FileNotFoundError(f"검증 대상이 없음: {rel}")

        actual = sha256_file(path)
        if actual != info["sha256"]:
            raise IOError(
                f"SHA-256 불일치: {rel} "
                f"(expected={info['sha256']}, actual={actual})"
            )
        progress_callback()

    return True


class ParallelBackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("820x680")
        self.root.minsize(760, 620)

        self.source_var = tk.StringVar()
        self.name_var = tk.StringVar(value="backup")
        self.incremental_var = tk.BooleanVar(value=True)
        self.verify_var = tk.BooleanVar(value=True)
        self.cache_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="대기 중")

        self.destinations = []
        self.running = False
        self.progress_value = 0
        self.progress_total = 1
        self.progress_lock = threading.Lock()
        self.cancel_event = threading.Event()

        self.build_ui()

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Parallel Backup", font=("", 18, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="하나의 원본을 여러 경로에 병렬 백업합니다."
        ).pack(anchor="w", pady=(2, 12))

        source_box = ttk.LabelFrame(outer, text="원본 폴더")
        source_box.pack(fill="x", pady=5)
        ttk.Entry(source_box, textvariable=self.source_var).pack(
            side="left", fill="x", expand=True, padx=8, pady=8
        )
        ttk.Button(source_box, text="찾기", command=self.select_source).pack(
            side="right", padx=8
        )

        name_box = ttk.LabelFrame(outer, text="백업 이름")
        name_box.pack(fill="x", pady=5)
        ttk.Entry(name_box, textvariable=self.name_var).pack(
            fill="x", padx=8, pady=8
        )
        ttk.Label(
            name_box,
            text="결과 예: uni_mcp_20260921_193000"
        ).pack(anchor="w", padx=8, pady=(0, 8))

        options = ttk.LabelFrame(outer, text="백업 옵션")
        options.pack(fill="x", pady=5)
        ttk.Checkbutton(
            options,
            text="증분 백업 (마지막 검증 완료 스냅샷 재사용)",
            variable=self.incremental_var,
        ).pack(anchor="w", padx=8, pady=(8, 4))
        ttk.Checkbutton(
            options,
            text="SHA-256 무결성 검사",
            variable=self.verify_var,
        ).pack(anchor="w", padx=8, pady=(4, 4))
        ttk.Checkbutton(
            options,
            text="빠른 해시 캐시 사용 (크기/수정시간이 같은 파일의 SHA-256 재사용)",
            variable=self.cache_var,
        ).pack(anchor="w", padx=8, pady=(4, 8))

        dest_box = ttk.LabelFrame(outer, text="백업 대상 경로")
        dest_box.pack(fill="both", expand=True, pady=5)

        list_frame = ttk.Frame(dest_box)
        list_frame.pack(fill="both", expand=True, padx=8, pady=8)

        self.dest_list = tk.Listbox(list_frame)
        self.dest_list.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.dest_list.yview
        )
        scroll.pack(side="right", fill="y")
        self.dest_list.configure(yscrollcommand=scroll.set)

        button_frame = ttk.Frame(dest_box)
        button_frame.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(
            button_frame, text="경로 추가", command=self.add_destination
        ).pack(side="left")
        ttk.Button(
            button_frame, text="선택 삭제", command=self.remove_destination
        ).pack(side="left", padx=5)
        ttk.Button(
            button_frame, text="전체 삭제", command=self.clear_destinations
        ).pack(side="left")

        action = ttk.Frame(outer)
        action.pack(fill="x", pady=(8, 4))
        self.backup_button = ttk.Button(
            action, text="병렬 백업 시작", command=self.start_backup
        )
        self.backup_button.pack(side="left")
        self.cancel_button = ttk.Button(
            action, text="취소", command=self.cancel_backup, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=5)
        ttk.Button(
            action, text="복구", command=self.restore_backup
        ).pack(side="left", padx=5)
        ttk.Label(action, textvariable=self.status_var).pack(side="right")

        self.progress = ttk.Progressbar(outer, mode="determinate", maximum=1)
        self.progress.pack(fill="x", pady=6)

        log_box = ttk.LabelFrame(outer, text="로그")
        log_box.pack(fill="both", expand=True, pady=5)
        self.log = tk.Text(log_box, height=10, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

    def select_source(self):
        path = filedialog.askdirectory(title="원본 폴더 선택")
        if path:
            self.source_var.set(path)

    def add_destination(self):
        path = filedialog.askdirectory(title="백업 대상 경로 선택")
        if path and path not in self.destinations:
            self.destinations.append(path)
            self.dest_list.insert("end", path)

    def remove_destination(self):
        for index in reversed(self.dest_list.curselection()):
            self.dest_list.delete(index)
            del self.destinations[index]

    def clear_destinations(self):
        self.destinations.clear()
        self.dest_list.delete(0, "end")

    def cancel_backup(self):
        if self.running:
            self.cancel_event.set()
            self.status_var.set("취소 요청...")
            self.write_log("[CANCEL] 취소 요청됨")

    def restore_backup(self):
        if self.running:
            messagebox.showwarning("사용 중", "백업 또는 복구가 끝난 후 실행하세요.")
            return

        backup_path_text = filedialog.askdirectory(title="복구할 백업 폴더 선택")
        if not backup_path_text:
            return

        backup_path = Path(backup_path_text).resolve()
        manifest_path = backup_path / MANIFEST_DIR / MANIFEST_FILE

        if not manifest_path.is_file():
            messagebox.showerror(
                "복구 오류",
                "선택한 폴더에서 .parallel-backup/manifest.json을 찾을 수 없습니다."
            )
            return

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("복구 오류", f"manifest 읽기 실패:\n{exc}")
            return

        if manifest.get("version") != 2 or manifest.get("verified") is not True:
            messagebox.showerror(
                "복구 오류",
                "검증 완료된 v2 백업만 복구할 수 있습니다."
            )
            return

        target_text = filedialog.askdirectory(title="복구 대상 폴더 선택")
        if not target_text:
            return

        target = Path(target_text).resolve()
        target.mkdir(parents=True, exist_ok=True)

        if any(target.iterdir()):
            confirmed = messagebox.askyesno(
                "복구 확인",
                f"대상 폴더에 기존 파일이 있습니다.\n\n{target}\n\n"
                "동일 경로의 파일을 덮어쓰면서 복구할까요?"
            )
            if not confirmed:
                return

        file_count = len(manifest.get("files", {}))
        self.running = True
        self.cancel_event.clear()
        self.backup_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress_value = 0
        self.progress_total = max(1, file_count * 2)
        self.progress.configure(value=0, maximum=self.progress_total)
        self.status_var.set("복구 중...")

        self.write_log(f"[RESTORE] {backup_path}")
        self.write_log(f"[RESTORE TARGET] {target}")
        self.write_log(f"[RESTORE FILES] {file_count:,}")

        threading.Thread(
            target=self.run_restore,
            args=(backup_path, target, manifest),
            daemon=True,
        ).start()

    def write_log(self, message):
        def update():
            self.log.configure(state="normal")
            self.log.insert("end", message + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, update)

    def set_progress(self, value=None, total=None, status=None):
        def update():
            if total is not None:
                self.progress_total = max(1, total)
                self.progress.configure(maximum=self.progress_total)
            if value is not None:
                self.progress_value = min(self.progress_total, value)
                self.progress.configure(value=self.progress_value)
            if status is not None:
                self.status_var.set(status)

        self.root.after(0, update)

    def advance_progress(self):
        with self.progress_lock:
            self.progress_value += 1
            value = self.progress_value
        self.set_progress(value=value)

    def validate(self):
        source = Path(self.source_var.get().strip())
        name = self.name_var.get().strip()

        if not source.is_dir():
            messagebox.showerror("오류", "유효한 원본 폴더를 선택하세요.")
            return None

        if not name:
            messagebox.showerror("오류", "백업 이름을 입력하세요.")
            return None

        invalid = '<>:"/\\|?*'
        if any(char in name for char in invalid):
            messagebox.showerror(
                "오류",
                f"백업 이름에 사용할 수 없는 문자가 있습니다:\n{invalid}"
            )
            return None

        if not self.destinations:
            messagebox.showerror("오류", "백업 대상 경로를 하나 이상 추가하세요.")
            return None

        return source.resolve(), name

    def start_backup(self):
        if self.running:
            return

        validated = self.validate()
        if not validated:
            return

        source, name = validated
        destinations = [Path(path).resolve() for path in self.destinations]

        for destination in destinations:
            try:
                destination.relative_to(source)
                messagebox.showerror(
                    "오류",
                    f"백업 대상이 원본 폴더 내부입니다.\n{destination}"
                )
                return
            except ValueError:
                pass

        backup_name = f"{name}_{datetime.now().strftime(TIMESTAMP_FORMAT)}"
        verify = self.verify_var.get()
        incremental = self.incremental_var.get()
        use_cache = self.cache_var.get()

        self.running = True
        self.cancel_event.clear()
        self.backup_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.configure(value=0, maximum=1)
        self.progress_value = 0
        self.progress_total = 1
        self.status_var.set("원본 해시 계산 중...")

        self.write_log(f"[START] {source}")
        self.write_log(f"[BACKUP] {backup_name}")
        self.write_log(f"[TARGETS] {len(destinations)}개")
        self.write_log(f"[INCREMENTAL] {'ON' if incremental else 'OFF'}")
        self.write_log(f"[VERIFY] {'ON' if verify else 'OFF'}")
        self.write_log(f"[HASH CACHE] {'ON' if use_cache else 'OFF'}")

        threading.Thread(
            target=self.run_backup,
            args=(
                source,
                destinations,
                backup_name,
                name,
                incremental,
                verify,
                use_cache,
            ),
            daemon=True,
        ).start()

    def run_backup(
        self,
        source: Path,
        destinations: list[Path],
        backup_name: str,
        backup_name_base: str,
        incremental: bool,
        verify: bool,
        use_cache: bool,
    ):
        try:
            self.write_log("[SCAN] 원본 파일 목록 및 SHA-256 준비 시작")
            source_data = build_source_manifest(source, use_cache=use_cache)
            if self.cancel_event.is_set():
                raise RuntimeError("백업이 취소되었습니다.")
            files_count = len(source_data["files"])
            self.write_log(
                f"[SCAN] 파일 {files_count:,}개 준비 완료 | "
                f"cache_hits={source_data['cache_hits']:,}, "
                f"cache_misses={source_data['cache_misses']:,}"
            )

            operations_per_destination = files_count * (2 if verify else 1)
            self.set_progress(
                value=0,
                total=max(1, operations_per_destination * len(destinations)),
                status=f"백업 중... 0/{files_count * len(destinations):,}",
            )

            with ThreadPoolExecutor(max_workers=len(destinations)) as executor:
                futures = [
                    executor.submit(
                        self.backup_one_destination,
                        source,
                        destination,
                        backup_name,
                        backup_name_base,
                        source_data,
                        incremental,
                        verify,
                    )
                    for destination in destinations
                ]

                results = [future.result() for future in as_completed(futures)]

            success = sum(1 for item in results if item["ok"])
            failed = len(results) - success

            def finish():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                if failed == 0:
                    self.status_var.set(f"완료: {success}/{len(results)}")
                    messagebox.showinfo(
                        "백업 완료",
                        f"{success}개 경로 백업 완료\n\n{backup_name}"
                    )
                else:
                    self.status_var.set(
                        f"완료: {success} 성공 / {failed} 실패"
                    )
                    messagebox.showwarning(
                        "백업 결과",
                        f"성공: {success}\n실패: {failed}\n\n로그를 확인하세요."
                    )

            self.root.after(0, finish)

        except Exception as exc:
            self.write_log(f"[FATAL] {exc}")

            def fail_finish():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                self.status_var.set("실패")
                messagebox.showerror("백업 실패", str(exc))

            self.root.after(0, fail_finish)

    def backup_one_destination(
        self,
        source: Path,
        destination: Path,
        backup_name: str,
        backup_name_base: str,
        source_data: dict,
        incremental: bool,
        verify: bool,
    ):
        target = destination / backup_name
        previous_dir = None
        previous_manifest = None
        copied = 0
        reused = 0

        self.write_log(f"[BEGIN] {destination}")

        try:
            destination.mkdir(parents=True, exist_ok=True)

            if target.exists():
                raise FileExistsError(f"이미 존재함: {target}")

            if incremental:
                previous_dir, previous_manifest = find_latest_verified_backup(
                    destination,
                    f"{backup_name_base}_",
                    source,
                )
                if previous_dir:
                    self.write_log(
                        f"[INCREMENTAL] {destination} <- {previous_dir.name}"
                    )
                else:
                    self.write_log(
                        f"[INCREMENTAL] {destination} -> 검증 완료 기준 없음, 전체 복사"
                    )

            ensure_directory_tree(source, target)

            previous_files = (
                previous_manifest.get("files", {})
                if previous_manifest is not None
                else {}
            )

            for rel, info in source_data["files"].items():
                if self.cancel_event.is_set():
                    raise RuntimeError("백업이 취소되었습니다.")

                src = source / Path(rel)
                dst = target / Path(rel)

                old_info = previous_files.get(rel)
                old_file = previous_dir / Path(rel) if previous_dir else None

                can_reuse = bool(
                    incremental
                    and old_info
                    and old_file
                    and old_file.is_file()
                    and old_info.get("sha256") == info["sha256"]
                    and old_info.get("size") == info["size"]
                    and old_info.get("mtime_ns") == info["mtime_ns"]
                )

                operation = copy_or_link(
                    src,
                    dst,
                    old_file,
                    can_reuse,
                )

                if operation == "hardlink":
                    reused += 1
                else:
                    copied += 1

                self.advance_progress()

            manifest = {
                "version": 2,
                "source": str(source),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "verified": False,
                "files": source_data["files"],
                "directories": source_data["directories"],
                "stats": {
                    "files": len(source_data["files"]),
                    "copied": copied,
                    "reused": reused,
                    "hash_cache_hits": source_data["cache_hits"],
                    "hash_cache_misses": source_data["cache_misses"],
                },
            }

            if verify:
                self.write_log(f"[VERIFY] {destination}")
                verify_backup(target, manifest, self.advance_progress)
                manifest["verified"] = True
                self.write_log(
                    f"[VERIFY OK] {destination} | "
                    f"copied={copied:,}, reused={reused:,}"
                )
            else:
                self.write_log(
                    f"[VERIFY SKIP] {destination} | "
                    f"copied={copied:,}, reused={reused:,}"
                )

            write_manifest(target, manifest)

            self.write_log(f"[OK] {target}")
            return {"ok": True, "target": str(target)}

        except Exception as exc:
            self.write_log(f"[FAIL] {destination} -> {exc}")
            return {"ok": False, "target": str(target), "error": str(exc)}


    def run_restore(self, backup_path: Path, target: Path, manifest: dict):
        files = manifest.get("files", {})
        restored = 0

        try:
            for rel in files:
                if self.cancel_event.is_set():
                    raise RuntimeError("복구가 취소되었습니다.")

                source_file = backup_path / Path(rel)
                target_file = target / Path(rel)

                if not source_file.is_file():
                    raise FileNotFoundError(f"백업 파일 없음: {rel}")

                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target_file)
                restored += 1
                self.advance_progress()

            self.write_log("[RESTORE VERIFY] 복구 결과 SHA-256 검사")

            for rel, info in files.items():
                if self.cancel_event.is_set():
                    raise RuntimeError("복구가 취소되었습니다.")

                target_file = target / Path(rel)
                if not target_file.is_file():
                    raise FileNotFoundError(f"복구 파일 없음: {rel}")

                actual = sha256_file(target_file)
                if actual != info.get("sha256"):
                    raise IOError(
                        f"복구 SHA-256 불일치: {rel} "
                        f"(expected={info.get('sha256')}, actual={actual})"
                    )
                self.advance_progress()

            def finish_restore():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                self.status_var.set(f"복구 완료: {restored:,}개")
                messagebox.showinfo(
                    "복구 완료",
                    f"{restored:,}개 파일을 복구하고 SHA-256 검증을 완료했습니다."
                )

            self.root.after(0, finish_restore)

        except Exception as exc:
            self.write_log(f"[RESTORE FAIL] {exc}")

            def fail_restore():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                self.status_var.set("복구 실패")
                messagebox.showerror("복구 실패", str(exc))

            self.root.after(0, fail_restore)


if __name__ == "__main__":
    root = tk.Tk()
    ParallelBackupApp(root)
    root.mainloop()
