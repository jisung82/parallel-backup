import fnmatch
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Parallel Backup"
APP_VERSION = "1.0.0"
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
MANIFEST_DIR = ".parallel-backup"
MANIFEST_FILE = "manifest.json"
SOURCE_CACHE_FILE = "source_cache.json"
PROFILE_FILE = "profile.json"
STALE_PARTIAL_SECONDS = 24 * 60 * 60
DEFAULT_FREE_SPACE_RESERVE = 64 * 1024 * 1024


def local_app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    path = Path(base) / "ParallelBackup"
    path.mkdir(parents=True, exist_ok=True)
    return path


def source_cache_path() -> Path:
    return local_app_dir() / SOURCE_CACHE_FILE


def profile_path() -> Path:
    return local_app_dir() / PROFILE_FILE


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json_atomic(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_patterns(raw: str):
    patterns = []
    for item in raw.replace("\n", ",").split(","):
        value = item.strip().replace("\\", "/")
        if value and value not in patterns:
            patterns.append(value)
    return patterns


def is_excluded(rel: str, patterns) -> bool:
    rel = rel.replace("\\", "/")
    name = Path(rel).name
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def load_source_cache() -> dict:
    return load_json(source_cache_path(), {"version": 2, "sources": {}})


def save_source_cache(cache: dict):
    save_json_atomic(source_cache_path(), cache)


def build_source_manifest(source: Path, use_cache: bool, exclude_patterns):
    files = {}
    directories = set()
    cache_hits = 0
    cache_misses = 0
    skipped = 0

    cache = load_source_cache() if use_cache else {"version": 2, "sources": {}}
    source_key = str(source.resolve())
    source_cache = cache.setdefault("sources", {}).setdefault(source_key, {})
    cached_files = source_cache.get("files", {})

    for root, dirnames, filenames in os.walk(source):
        root_path = Path(root)

        kept_dirs = []
        for dirname in dirnames:
            rel_dir = (root_path / dirname).relative_to(source).as_posix()
            if is_excluded(rel_dir, exclude_patterns):
                continue
            kept_dirs.append(dirname)
            directories.add(rel_dir)
        dirnames[:] = kept_dirs

        for filename in filenames:
            src = root_path / filename
            rel = src.relative_to(source).as_posix()

            if is_excluded(rel, exclude_patterns):
                skipped += 1
                continue

            stat = src.stat()
            cached = cached_files.get(rel)
            valid_cache = bool(
                use_cache
                and cached
                and cached.get("sha256")
                and cached.get("size") == stat.st_size
                and cached.get("mtime_ns") == stat.st_mtime_ns
                and cached.get("ctime_ns") == stat.st_ctime_ns
            )

            if valid_cache:
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

    result = {
        "files": files,
        "directories": sorted(directories),
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "excluded": skipped,
    }

    if use_cache:
        cache.setdefault("sources", {})[source_key] = {
            "files": files,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_source_cache(cache)

    return result


def write_manifest(target: Path, manifest: dict):
    manifest_dir = target / MANIFEST_DIR
    manifest_dir.mkdir(parents=True, exist_ok=True)
    save_json_atomic(manifest_dir / MANIFEST_FILE, manifest)


def read_manifest(snapshot: Path):
    path = snapshot / MANIFEST_DIR / MANIFEST_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cleanup_stale_partials(destination: Path):
    now = time.time()
    for item in destination.iterdir():
        if not item.is_dir() or ".parallel-backup.partial-" not in item.name:
            continue
        try:
            if now - item.stat().st_mtime > STALE_PARTIAL_SECONDS:
                shutil.rmtree(item, ignore_errors=True)
        except OSError:
            pass


def list_verified_snapshots(destination: Path, prefix: str, source: Path):
    source_text = str(source.resolve())
    results = []
    if not destination.is_dir():
        return results

    for item in destination.iterdir():
        if not item.is_dir() or not item.name.startswith(prefix):
            continue
        manifest = read_manifest(item)
        if not manifest:
            continue
        if (
            manifest.get("version") in (2, 3)
            and manifest.get("source") == source_text
            and manifest.get("verified") is True
        ):
            results.append((item, manifest))

    results.sort(key=lambda x: x[0].name, reverse=True)
    return results


def find_latest_verified_backup(destination: Path, prefix: str, source: Path):
    snapshots = list_verified_snapshots(destination, prefix, source)
    return snapshots[0] if snapshots else (None, None)


def make_unique_backup_name(destination: Path, desired: str):
    candidate = desired
    counter = 1
    while (destination / candidate).exists():
        candidate = f"{desired}_{counter:02d}"
        counter += 1
    return candidate


def ensure_free_space(destination: Path, required_bytes: int):
    free = shutil.disk_usage(destination).free
    needed = required_bytes + DEFAULT_FREE_SPACE_RESERVE
    if free < needed:
        raise OSError(
            f"디스크 여유공간 부족: 필요 약 {needed / (1024**3):.2f} GB, "
            f"현재 여유 {free / (1024**3):.2f} GB"
        )


def copy_file(source_file: Path, target_file: Path, previous_file: Path | None, use_hardlink: bool):
    target_file.parent.mkdir(parents=True, exist_ok=True)

    if use_hardlink and previous_file and previous_file.is_file():
        try:
            os.link(previous_file, target_file)
            return "hardlink"
        except OSError:
            pass

    shutil.copy2(source_file, target_file)
    return "copy"


def verify_snapshot(snapshot: Path, manifest: dict, progress_callback, cancel_event):
    for rel, info in manifest["files"].items():
        if cancel_event.is_set():
            raise RuntimeError("작업이 취소되었습니다.")

        path = snapshot / Path(rel)
        if not path.is_file():
            raise FileNotFoundError(f"검증 대상이 없음: {rel}")

        actual = sha256_file(path)
        if actual != info["sha256"]:
            raise IOError(
                f"SHA-256 불일치: {rel} "
                f"(expected={info['sha256']}, actual={actual})"
            )
        progress_callback()


def safe_remove_snapshot(snapshot: Path):
    if snapshot.is_dir():
        shutil.rmtree(snapshot)


class ParallelBackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("900x760")
        self.root.minsize(820, 700)

        self.source_var = tk.StringVar()
        self.name_var = tk.StringVar(value="backup")
        self.incremental_var = tk.BooleanVar(value=True)
        self.verify_var = tk.BooleanVar(value=True)
        self.cache_var = tk.BooleanVar(value=True)
        self.hardlink_var = tk.BooleanVar(value=True)
        self.parallel_var = tk.IntVar(value=3)
        self.keep_var = tk.IntVar(value=10)
        self.exclude_var = tk.StringVar()
        self.status_var = tk.StringVar(value="대기 중")

        self.destinations = []
        self.running = False
        self.cancel_event = threading.Event()
        self.progress_lock = threading.Lock()
        self.progress_value = 0
        self.progress_total = 1

        self.build_ui()
        self.load_profile()

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer, text="Parallel Backup", font=("", 19, "bold")
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=f"GUI 병렬 백업 / v{APP_VERSION}",
        ).pack(anchor="w", pady=(2, 10))

        source_box = ttk.LabelFrame(outer, text="원본 폴더")
        source_box.pack(fill="x", pady=4)
        ttk.Entry(source_box, textvariable=self.source_var).pack(
            side="left", fill="x", expand=True, padx=8, pady=7
        )
        ttk.Button(source_box, text="찾기", command=self.select_source).pack(
            side="right", padx=8
        )

        name_box = ttk.LabelFrame(outer, text="백업 이름")
        name_box.pack(fill="x", pady=4)
        ttk.Entry(name_box, textvariable=self.name_var).pack(
            fill="x", padx=8, pady=7
        )
        ttk.Label(
            name_box,
            text="예: uni_mcp → uni_mcp_20260921_193000",
        ).pack(anchor="w", padx=8, pady=(0, 6))

        option_box = ttk.LabelFrame(outer, text="백업 옵션")
        option_box.pack(fill="x", pady=4)

        left = ttk.Frame(option_box)
        left.pack(side="left", fill="both", expand=True, padx=8, pady=6)

        ttk.Checkbutton(
            left,
            text="증분 백업",
            variable=self.incremental_var,
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            left,
            text="SHA-256 검증",
            variable=self.verify_var,
        ).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(
            left,
            text="빠른 해시 캐시",
            variable=self.cache_var,
        ).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(
            left,
            text="변경 없는 파일 하드링크 재사용",
            variable=self.hardlink_var,
        ).grid(row=3, column=0, sticky="w")

        right = ttk.Frame(option_box)
        right.pack(side="right", padx=8, pady=6)

        ttk.Label(right, text="동시 대상 수").grid(row=0, column=0, sticky="e", padx=5)
        ttk.Spinbox(
            right, from_=1, to=16, width=6, textvariable=self.parallel_var
        ).grid(row=0, column=1, sticky="w")

        ttk.Label(right, text="보존 스냅샷").grid(row=1, column=0, sticky="e", padx=5)
        ttk.Spinbox(
            right, from_=1, to=999, width=6, textvariable=self.keep_var
        ).grid(row=1, column=1, sticky="w")

        exclude_box = ttk.LabelFrame(
            outer, text="제외 패턴 (쉼표/줄바꿈 구분)"
        )
        exclude_box.pack(fill="x", pady=4)
        ttk.Entry(
            exclude_box, textvariable=self.exclude_var
        ).pack(fill="x", padx=8, pady=7)
        ttk.Label(
            exclude_box,
            text="예: .git, Library, Temp, *.log, *.tmp",
        ).pack(anchor="w", padx=8, pady=(0, 6))

        dest_box = ttk.LabelFrame(outer, text="백업 대상 경로")
        dest_box.pack(fill="both", expand=True, pady=4)

        list_frame = ttk.Frame(dest_box)
        list_frame.pack(fill="both", expand=True, padx=8, pady=7)

        self.dest_list = tk.Listbox(list_frame)
        self.dest_list.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(
            list_frame,
            orient="vertical",
            command=self.dest_list.yview,
        )
        scroll.pack(side="right", fill="y")
        self.dest_list.configure(yscrollcommand=scroll.set)

        dest_buttons = ttk.Frame(dest_box)
        dest_buttons.pack(fill="x", padx=8, pady=(0, 7))

        ttk.Button(
            dest_buttons, text="경로 추가", command=self.add_destination
        ).pack(side="left")
        ttk.Button(
            dest_buttons, text="선택 삭제", command=self.remove_destination
        ).pack(side="left", padx=5)
        ttk.Button(
            dest_buttons, text="전체 삭제", command=self.clear_destinations
        ).pack(side="left")

        action = ttk.Frame(outer)
        action.pack(fill="x", pady=(6, 3))

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
        ).pack(side="left")

        ttk.Button(
            action, text="프로필 저장", command=self.save_profile
        ).pack(side="left", padx=5)

        ttk.Label(action, textvariable=self.status_var).pack(side="right")

        self.progress = ttk.Progressbar(
            outer, mode="determinate", maximum=1
        )
        self.progress.pack(fill="x", pady=5)

        log_box = ttk.LabelFrame(outer, text="로그")
        log_box.pack(fill="both", expand=True, pady=4)
        self.log = tk.Text(
            log_box, height=9, state="disabled", wrap="word"
        )
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

    def select_source(self):
        path = filedialog.askdirectory(title="원본 폴더 선택")
        if path:
            self.source_var.set(path)
            self.save_profile(silent=True)

    def add_destination(self):
        path = filedialog.askdirectory(title="백업 대상 경로 선택")
        if path and path not in self.destinations:
            self.destinations.append(path)
            self.dest_list.insert("end", path)
            self.save_profile(silent=True)

    def remove_destination(self):
        for index in reversed(self.dest_list.curselection()):
            self.dest_list.delete(index)
            del self.destinations[index]
        self.save_profile(silent=True)

    def clear_destinations(self):
        self.destinations.clear()
        self.dest_list.delete(0, "end")
        self.save_profile(silent=True)

    def save_profile(self, silent=False):
        profile = {
            "version": 2,
            "source": self.source_var.get().strip(),
            "name": self.name_var.get().strip(),
            "destinations": self.destinations,
            "incremental": self.incremental_var.get(),
            "verify": self.verify_var.get(),
            "cache": self.cache_var.get(),
            "hardlink": self.hardlink_var.get(),
            "parallel": self.parallel_var.get(),
            "keep": self.keep_var.get(),
            "exclude": self.exclude_var.get(),
        }
        try:
            save_json_atomic(profile_path(), profile)
            if not silent:
                self.write_log("[PROFILE] 저장 완료")
        except OSError as exc:
            if not silent:
                messagebox.showerror("프로필 오류", str(exc))

    def load_profile(self):
        profile = load_json(profile_path(), {})
        if not profile:
            return

        self.source_var.set(profile.get("source", ""))
        self.name_var.set(profile.get("name", "backup"))
        self.incremental_var.set(profile.get("incremental", True))
        self.verify_var.set(profile.get("verify", True))
        self.cache_var.set(profile.get("cache", True))
        self.hardlink_var.set(profile.get("hardlink", True))
        self.parallel_var.set(int(profile.get("parallel", 3)))
        self.keep_var.set(int(profile.get("keep", 10)))
        self.exclude_var.set(profile.get("exclude", ""))

        self.destinations = []
        self.dest_list.delete(0, "end")
        for item in profile.get("destinations", []):
            if item:
                self.destinations.append(item)
                self.dest_list.insert("end", item)

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

    def cancel_backup(self):
        if self.running:
            self.cancel_event.set()
            self.status_var.set("취소 요청...")
            self.write_log("[CANCEL] 취소 요청됨")

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
                f"백업 이름에 사용할 수 없는 문자가 있습니다:\n{invalid}",
            )
            return None

        try:
            parallel = max(1, min(16, int(self.parallel_var.get())))
            keep = max(1, min(999, int(self.keep_var.get())))
            self.parallel_var.set(parallel)
            self.keep_var.set(keep)
        except (ValueError, tk.TclError):
            messagebox.showerror("오류", "동시 대상 수/보존 스냅샷 값을 확인하세요.")
            return None

        if not self.destinations:
            messagebox.showerror("오류", "백업 대상 경로를 하나 이상 추가하세요.")
            return None

        destinations = []
        for raw in self.destinations:
            destination = Path(raw).resolve()
            if destination not in destinations:
                destinations.append(destination)

        for destination in destinations:
            try:
                destination.relative_to(source)
                messagebox.showerror(
                    "오류",
                    f"백업 대상이 원본 폴더 내부입니다.\n{destination}",
                )
                return None
            except ValueError:
                pass

        return source.resolve(), name, destinations, parallel, keep

    def start_backup(self):
        if self.running:
            return

        validated = self.validate()
        if not validated:
            return

        source, name, destinations, parallel, keep = validated
        exclude_patterns = normalize_patterns(self.exclude_var.get())

        base_name = f"{name}_{datetime.now().strftime(TIMESTAMP_FORMAT)}"

        self.save_profile(silent=True)
        incremental = self.incremental_var.get()
        verify = self.verify_var.get()
        use_cache = self.cache_var.get()
        hardlink = self.hardlink_var.get()

        self.running = True
        self.cancel_event.clear()
        self.backup_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress_value = 0
        self.progress_total = 1
        self.progress.configure(value=0, maximum=1)
        self.status_var.set("원본 분석 중...")

        self.write_log(f"[START] {source}")
        self.write_log(f"[TARGETS] {len(destinations)}개 | workers={parallel}")
        self.write_log(f"[NAME] {base_name}")
        self.write_log(f"[KEEP] {keep}")
        self.write_log(
            f"[OPTIONS] incremental={incremental} "
            f"verify={verify} "
            f"cache={use_cache} "
            f"hardlink={hardlink}"
        )
        self.write_log(
            f"[EXCLUDE] {', '.join(exclude_patterns) if exclude_patterns else '(없음)'}"
        )

        threading.Thread(
            target=self.run_backup,
            args=(
                source,
                name,
                destinations,
                parallel,
                keep,
                exclude_patterns,
                incremental,
                verify,
                use_cache,
                hardlink,
            ),
            daemon=True,
        ).start()

    def run_backup(
        self,
        source: Path,
        base_name: str,
        destinations: list[Path],
        parallel: int,
        keep: int,
        exclude_patterns,
        incremental: bool,
        verify: bool,
        use_cache: bool,
        hardlink: bool,
    ):
        try:
            source_data = build_source_manifest(
                source,
                use_cache=use_cache,
                exclude_patterns=exclude_patterns,
            )
            if self.cancel_event.is_set():
                raise RuntimeError("백업이 취소되었습니다.")

            files = source_data["files"]
            file_count = len(files)
            source_size = sum(info["size"] for info in files.values())

            self.write_log(
                f"[SCAN] files={file_count:,} "
                f"size={source_size / (1024**3):.2f} GB "
                f"cache_hits={source_data['cache_hits']:,} "
                f"cache_misses={source_data['cache_misses']:,} "
                f"excluded={source_data['excluded']:,}"
            )

            expected_ops = max(
                1,
                (file_count * (2 if verify else 1))
                * len(destinations),
            )
            self.set_progress(
                value=0,
                total=expected_ops,
                status=f"백업 중... {file_count:,} 파일",
            )

            workers = min(parallel, len(destinations))
            results = []

            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(
                        self.backup_one_destination,
                        source,
                        name,
                        destination,
                        source_data,
                        keep,
                        incremental,
                        verify,
                        hardlink,
                    ): destination
                    for destination in destinations
                }

                for future in as_completed(future_map):
                    results.append(future.result())

            if self.cancel_event.is_set():
                raise RuntimeError("백업이 취소되었습니다.")

            success = sum(1 for item in results if item["ok"])
            failed = len(results) - success

            self.write_log(
                f"[SUMMARY] success={success} failed={failed}"
            )

            def finish():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                if failed == 0:
                    self.status_var.set(f"완료: {success}/{len(results)}")
                    messagebox.showinfo(
                        "백업 완료",
                        f"{success}개 경로 백업 완료",
                    )
                else:
                    self.status_var.set(
                        f"완료: {success} 성공 / {failed} 실패"
                    )
                    messagebox.showwarning(
                        "백업 결과",
                        f"성공: {success}\n실패: {failed}\n로그를 확인하세요.",
                    )

            self.root.after(0, finish)

        except Exception as exc:
            self.write_log(f"[FATAL] {exc}")

            def finish_error():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                self.status_var.set("실패")
                messagebox.showerror("백업 실패", str(exc))

            self.root.after(0, finish_error)

    def backup_one_destination(
        self,
        source: Path,
        requested_name: str,
        destination: Path,
        source_data: dict,
        keep: int,
        incremental: bool,
        verify: bool,
        hardlink: bool,
    ):
        destination.mkdir(parents=True, exist_ok=True)
        cleanup_stale_partials(destination)
        backup_name = make_unique_backup_name(
            destination,
            f"{requested_name}_{datetime.now().strftime(TIMESTAMP_FORMAT)}",
        )
        final_target = destination / backup_name
        partial_target = destination / f".parallel-backup.partial-{uuid.uuid4().hex}"

        previous_dir = None
        previous_manifest = None
        copied = 0
        reused = 0

        try:
            destination.mkdir(parents=True, exist_ok=True)

            if self.incremental_var.get():
                previous_dir, previous_manifest = find_latest_verified_backup(
                    destination,
                    f"{requested_name}_",
                    source,
                )

            previous_files = (
                previous_manifest.get("files", {})
                if previous_manifest else {}
            )

            reusable_bytes = 0
            required_bytes = 0

            for rel, info in source_data["files"].items():
                old_info = previous_files.get(rel)
                old_file = previous_dir / Path(rel) if previous_dir else None
                can_reuse = bool(
                    incremental
                    and hardlink
                    and old_info
                    and old_file
                    and old_file.is_file()
                    and old_info.get("sha256") == info["sha256"]
                    and old_info.get("size") == info["size"]
                    and old_info.get("mtime_ns") == info["mtime_ns"]
                )
                if can_reuse:
                    reusable_bytes += info["size"]
                else:
                    required_bytes += info["size"]

            ensure_free_space(destination, required_bytes)

            self.write_log(
                f"[BEGIN] {destination} | "
                f"reuse={reusable_bytes / (1024**3):.2f} GB "
                f"write={required_bytes / (1024**3):.2f} GB"
            )

            partial_target.mkdir(parents=True, exist_ok=False)

            for rel, info in source_data["files"].items():
                if self.cancel_event.is_set():
                    raise RuntimeError("백업이 취소되었습니다.")

                src = source / Path(rel)
                dst = partial_target / Path(rel)

                old_info = previous_files.get(rel)
                old_file = previous_dir / Path(rel) if previous_dir else None
                can_reuse = bool(
                    self.incremental_var.get()
                    and self.hardlink_var.get()
                    and old_info
                    and old_file
                    and old_file.is_file()
                    and old_info.get("sha256") == info["sha256"]
                    and old_info.get("size") == info["size"]
                    and old_info.get("mtime_ns") == info["mtime_ns"]
                )

                operation = copy_file(
                    src,
                    dst,
                    old_file if can_reuse else None,
                    hardlink and can_reuse,
                )

                if operation == "hardlink":
                    reused += 1
                else:
                    copied += 1

                self.advance_progress()

            manifest = {
                "version": 3,
                "app_version": APP_VERSION,
                "source": str(source.resolve()),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "verified": False,
                "backup_name": backup_name,
                "exclude_patterns": normalize_patterns(self.exclude_var.get()),
                "files": source_data["files"],
                "directories": source_data["directories"],
                "stats": {
                    "files": len(source_data["files"]),
                    "copied": copied,
                    "reused": reused,
                    "source_bytes": sum(
                        item["size"] for item in source_data["files"].values()
                    ),
                },
            }

            write_manifest(partial_target, manifest)

            if verify:
                self.write_log(f"[VERIFY] {destination}")
                verify_snapshot(
                    partial_target,
                    manifest,
                    self.advance_progress,
                    self.cancel_event,
                )
                manifest["verified"] = True
                write_manifest(partial_target, manifest)

            if self.cancel_event.is_set():
                raise RuntimeError("백업이 취소되었습니다.")

            os.replace(partial_target, final_target)

            snapshots = list_verified_snapshots(
                destination,
                f"{requested_name}_",
                source,
            )
            for old_snapshot, _ in snapshots[keep:]:
                try:
                    safe_remove_snapshot(old_snapshot)
                    self.write_log(f"[RETENTION] 삭제: {old_snapshot.name}")
                except OSError as exc:
                    self.write_log(
                        f"[RETENTION FAIL] {old_snapshot.name} -> {exc}"
                    )

            self.write_log(
                f"[OK] {destination} -> {backup_name} "
                f"(copied={copied:,}, reused={reused:,})"
            )
            return {"ok": True, "destination": str(destination), "snapshot": str(final_target)}

        except Exception as exc:
            if partial_target.exists():
                shutil.rmtree(partial_target, ignore_errors=True)
            self.write_log(f"[FAIL] {destination} -> {exc}")
            return {"ok": False, "destination": str(destination), "error": str(exc)}

    def restore_backup(self):
        if self.running:
            messagebox.showwarning("사용 중", "백업 또는 복구가 끝난 후 실행하세요.")
            return

        backup_text = filedialog.askdirectory(title="복구할 백업 폴더 선택")
        if not backup_text:
            return

        backup = Path(backup_text).resolve()
        manifest = read_manifest(backup)

        if (
            not manifest
            or manifest.get("version") not in (2, 3)
            or manifest.get("verified") is not True
        ):
            messagebox.showerror(
                "복구 오류",
                "검증 완료된 v2/v3 백업 스냅샷이 아닙니다.",
            )
            return

        target_text = filedialog.askdirectory(title="복구 대상 폴더 선택")
        if not target_text:
            return

        target = Path(target_text).resolve()
        target.mkdir(parents=True, exist_ok=True)

        if any(target.iterdir()):
            if not messagebox.askyesno(
                "복구 확인",
                f"대상 폴더에 기존 파일이 있습니다.\n\n{target}\n\n"
                "동일 경로 파일을 덮어쓰면서 복구할까요?",
            ):
                return

        files = manifest.get("files", {})
        self.running = True
        self.cancel_event.clear()
        self.backup_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress_value = 0
        self.progress_total = max(1, len(files) * 2)
        self.progress.configure(value=0, maximum=self.progress_total)
        self.status_var.set("복구 중...")

        self.write_log(f"[RESTORE] {backup}")
        self.write_log(f"[RESTORE TARGET] {target}")
        self.write_log(f"[RESTORE FILES] {len(files):,}")

        threading.Thread(
            target=self.run_restore,
            args=(backup, target, manifest),
            daemon=True,
        ).start()

    def run_restore(self, backup: Path, target: Path, manifest: dict):
        restored = 0
        try:
            for rel in manifest["files"]:
                if self.cancel_event.is_set():
                    raise RuntimeError("복구가 취소되었습니다.")

                source_file = backup / Path(rel)
                target_file = target / Path(rel)

                if not source_file.is_file():
                    raise FileNotFoundError(f"백업 파일 없음: {rel}")

                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target_file)
                restored += 1
                self.advance_progress()

            verify_snapshot(
                target,
                manifest,
                self.advance_progress,
                self.cancel_event,
            )

            def finish():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                self.status_var.set(f"복구 완료: {restored:,}개")
                messagebox.showinfo(
                    "복구 완료",
                    f"{restored:,}개 파일 복구 + SHA-256 검증 완료",
                )

            self.root.after(0, finish)

        except Exception as exc:
            self.write_log(f"[RESTORE FAIL] {exc}")

            def finish_error():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                self.status_var.set("복구 실패")
                messagebox.showerror("복구 실패", str(exc))

            self.root.after(0, finish_error)


if __name__ == "__main__":
    root = tk.Tk()
    ParallelBackupApp(root)
    root.mainloop()
