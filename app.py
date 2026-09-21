import fnmatch
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk


APP_TITLE = "Parallel Backup"
APP_VERSION = "1.4.0"
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


def create_zip_archive(snapshot: Path, archive_path: Path, progress_callback, cancel_event):
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        files = [
            path for path in snapshot.rglob("*")
            if path.is_file()
        ]
        for path in files:
            if cancel_event.is_set():
                raise RuntimeError("ZIP 생성이 취소되었습니다.")
            archive.write(path, path.relative_to(snapshot).as_posix())
            progress_callback()


def verify_zip_archive(
    archive_path: Path,
    manifest: dict,
    progress_callback,
    cancel_event,
    deep_scan: bool,
):
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise IOError(f"ZIP CRC 검증 실패: {bad_member}")

        if deep_scan:
            for rel, info in manifest["files"].items():
                if cancel_event.is_set():
                    raise RuntimeError("ZIP 검증이 취소되었습니다.")
                try:
                    with archive.open(rel, "r") as handle:
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                        actual = digest.hexdigest()
                except KeyError:
                    raise FileNotFoundError(f"ZIP에 파일 없음: {rel}")

                if actual != info["sha256"]:
                    raise IOError(
                        f"ZIP SHA-256 불일치: {rel} "
                        f"(expected={info['sha256']}, actual={actual})"
                    )
                progress_callback()

    manifest_member = MANIFEST_DIR + "/" + MANIFEST_FILE
    with zipfile.ZipFile(archive_path, mode="r") as archive:
        if manifest_member not in archive.namelist():
            raise FileNotFoundError("ZIP 내부 manifest.json이 없습니다.")


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


def build_source_manifest(source: Path, deep_scan: bool, exclude_patterns):
    files = {}
    directories = set()
    cache_hits = 0
    cache_misses = 0
    skipped = 0

    cache = load_source_cache() if deep_scan else {"version": 2, "sources": {}}
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

            file_hash = None
            if deep_scan:
                cached = cached_files.get(rel)
                valid_cache = bool(
                    cached
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

    if deep_scan:
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
    while (
        (destination / candidate).exists()
        or (destination / f"{candidate}.zip").exists()
    ):
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


def verify_snapshot_fast(
    source: Path,
    snapshot: Path,
    manifest: dict,
    progress_callback,
    cancel_event,
):
    for rel, info in manifest["files"].items():
        if cancel_event.is_set():
            raise RuntimeError("작업이 취소되었습니다.")

        source_path = source / Path(rel)
        snapshot_path = snapshot / Path(rel)
        if not source_path.is_file():
            raise FileNotFoundError(f"원본 파일 없음: {rel}")
        if not snapshot_path.is_file():
            raise FileNotFoundError(f"백업 파일 없음: {rel}")

        source_size = source_path.stat().st_size
        snapshot_size = snapshot_path.stat().st_size
        if source_size != snapshot_size:
            raise IOError(
                f"파일 크기 불일치: {rel} "
                f"(source={source_size}, backup={snapshot_size})"
            )
        progress_callback()


def verify_snapshot_sha256(snapshot: Path, manifest: dict, progress_callback, cancel_event):
    for rel, info in manifest["files"].items():
        if cancel_event.is_set():
            raise RuntimeError("검증 대상 검사가 취소되었습니다.")

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

        icon_path = Path(__file__).resolve().parent / "assets" / "parallel_backup.ico"
        try:
            if icon_path.is_file():
                self.root.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass

        self.source_var = tk.StringVar()
        self.name_var = tk.StringVar(value="backup")
        self.incremental_var = tk.BooleanVar(value=True)
        self.backup_mode_var = tk.StringVar(value="일반 백업")
        self.app_mode_var = tk.StringVar(value="일반 백업")
        self.hardlink_var = tk.BooleanVar(value=True)
        self.parallel_var = tk.IntVar(value=3)
        self.keep_var = tk.IntVar(value=10)
        self.exclude_var = tk.StringVar()
        self.status_var = tk.StringVar(value="대기 중")
        self.elapsed_var = tk.StringVar(value="경과 00:00:00")
        self.eta_var = tk.StringVar(value="예상 계산 중...")
        self.operation_started_at = None
        self.elapsed_job = None
        self.last_elapsed_seconds = 0

        self.compare_source_var = tk.StringVar()
        self.compare_target_var = tk.StringVar()
        self.compare_size_var = tk.BooleanVar(value=True)
        self.compare_mtime_var = tk.BooleanVar(value=True)
        self.compare_sha_var = tk.BooleanVar(value=False)
        self.compare_status_var = tk.StringVar(value="대기 중")
        self.compare_elapsed_var = tk.StringVar(value="경과 00:00:00")
        self.compare_eta_var = tk.StringVar(value="예상 계산 중...")
        self.compare_started_at = None
        self.compare_timer_job = None
        self.compare_progress_value = 0
        self.compare_progress_total = 1
        self.compare_results = []
        self.compare_summary = {"same": 0, "different": 0, "left_only": 0, "right_only": 0}

        self.timeline_steps = [
            ("원본 분석", "scan"),
            ("파일 목록 생성", "files"),
            ("백업 복사", "copy"),
            ("무결성 검사", "check"),
            ("압축 (ZIP)", "zip"),
            ("최종 검증", "shield"),
            ("완료", "flag"),
        ]
        self.timeline_current = -1
        self.timeline_error = False
        self.timeline_success = False

        self.destinations = []
        self.running = False
        self.cancel_event = threading.Event()
        self.progress_lock = threading.Lock()
        self.progress_value = 0
        self.progress_total = 1

        self._setup_style()
        self.build_ui()
        self.load_profile()
        self._refresh_metrics()

    def _setup_style(self):
        self.colors = {
            "bg": "#F8FAFC",
            "surface": "#FFFFFF",
            "surface_soft": "#F8FAFC",
            "primary": "#4F46E5",
            "primary_dark": "#3730A3",
            "secondary": "#7C3AED",
            "danger": "#DC2626",
            "danger_dark": "#B91C1C",
            "text": "#0F172A",
            "muted": "#64748B",
            "success": "#10B981",
            "border": "#E2E8F0",
            "soft_indigo": "#EEF2FF",
            "soft_violet": "#F5F3FF",
            "soft_green": "#ECFDF5",
            "shadow": "#E8EAFB",
        }

        families = set(tkfont.families(self.root))
        self.font_family = (
            "Plus Jakarta Sans"
            if "Plus Jakarta Sans" in families
            else "Segoe UI"
        )

        self.root.configure(bg=self.colors["bg"])

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            ".",
            background=self.colors["bg"],
            foreground=self.colors["text"],
            font=(self.font_family, 10),
        )
        style.configure(
            "Card.TFrame",
            background=self.colors["surface"],
        )
        style.configure(
            "Soft.TFrame",
            background=self.colors["surface_soft"],
        )
        style.configure(
            "Title.TLabel",
            background=self.colors["surface"],
            foreground=self.colors["text"],
            font=(self.font_family, 13, "bold"),
        )
        style.configure(
            "Body.TLabel",
            background=self.colors["surface"],
            foreground=self.colors["muted"],
            font=(self.font_family, 9),
        )
        style.configure(
            "Muted.TLabel",
            background=self.colors["bg"],
            foreground=self.colors["muted"],
            font=(self.font_family, 9),
        )
        style.configure(
            "Metric.TLabel",
            background=self.colors["surface"],
            foreground=self.colors["text"],
            font=(self.font_family, 20, "bold"),
        )
        style.configure(
            "Badge.TLabel",
            background=self.colors["soft_green"],
            foreground=self.colors["success"],
            font=(self.font_family, 9, "bold"),
            padding=(9, 4),
        )

        style.configure(
            "TEntry",
            fieldbackground=self.colors["surface"],
            foreground=self.colors["text"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
            insertcolor=self.colors["primary"],
            padding=9,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", self.colors["primary"])],
            lightcolor=[("focus", self.colors["primary"])],
            darkcolor=[("focus", self.colors["primary"])],
        )

        style.configure(
            "TSpinbox",
            fieldbackground=self.colors["surface"],
            foreground=self.colors["text"],
            bordercolor=self.colors["border"],
            lightcolor=self.colors["border"],
            darkcolor=self.colors["border"],
            padding=7,
        )

        style.configure(
            "TCheckbutton",
            background=self.colors["surface"],
            foreground=self.colors["text"],
            font=(self.font_family, 9, "bold"),
        )
        style.map(
            "TCheckbutton",
            foreground=[("active", self.colors["primary"])],
            background=[("active", self.colors["surface"])],
        )

        style.configure(
            "TButton",
            background=self.colors["surface"],
            foreground="#334155",
            borderwidth=1,
            relief="flat",
            padding=(13, 9),
            font=(self.font_family, 9, "bold"),
        )
        style.map(
            "TButton",
            background=[
                ("pressed", "#F1F5F9"),
                ("active", "#F8FAFC"),
            ],
            foreground=[
                ("active", self.colors["primary_dark"]),
            ],
        )

        style.configure(
            "Primary.TButton",
            background=self.colors["primary"],
            foreground="#FFFFFF",
            borderwidth=0,
            padding=(16, 10),
            font=(self.font_family, 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[
                ("pressed", self.colors["primary_dark"]),
                ("active", self.colors["secondary"]),
                ("disabled", "#A5B4FC"),
            ],
            foreground=[
                ("disabled", "#EEF2FF"),
            ],
        )

        style.configure(
            "Danger.TButton",
            background="#FFF1F2",
            foreground="#BE123C",
            borderwidth=1,
            padding=(12, 9),
        )
        style.map(
            "Danger.TButton",
            background=[
                ("pressed", "#FFE4E6"),
                ("active", "#FFF1F2"),
            ],
        )

        style.configure(
            "Ghost.TButton",
            background=self.colors["surface_soft"],
            foreground=self.colors["muted"],
            borderwidth=0,
            padding=(10, 8),
        )
        style.map(
            "Ghost.TButton",
            background=[
                ("pressed", "#EEF2FF"),
                ("active", "#F1F5F9"),
            ],
        )

        style.configure(
            "Horizontal.TProgressbar",
            background=self.colors["primary"],
            troughcolor="#EDEFFB",
            bordercolor="#EDEFFB",
            lightcolor=self.colors["primary"],
            darkcolor=self.colors["primary"],
            thickness=10,
        )

        style.configure(
            "TScrollbar",
            background="#CBD5E1",
            troughcolor="#F8FAFC",
            bordercolor="#F8FAFC",
            arrowcolor="#64748B",
        )

    def _card(self, parent, padding=16):
        wrapper = tk.Frame(
            parent,
            bg=self.colors["shadow"],
            highlightthickness=0,
        )
        wrapper.configure(padx=2, pady=2)
        card = tk.Frame(
            wrapper,
            bg=self.colors["surface"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
            bd=0,
        )
        card.pack(fill="both", expand=True, padx=0, pady=0)
        inner = ttk.Frame(card, style="Card.TFrame", padding=padding)
        inner.pack(fill="both", expand=True)
        return wrapper, inner

    def _gradient_header(self, parent):
        canvas = tk.Canvas(
            parent,
            height=142,
            bg=self.colors["primary"],
            highlightthickness=0,
            bd=0,
        )
        canvas.pack(fill="x")

        def draw(event=None):
            width = canvas.winfo_width()
            height = canvas.winfo_height()
            canvas.delete("all")

            precision = self.backup_mode_var.get() == "정밀 검사 백업"
            compare = self.app_mode_var.get() == "파일 비교"

            if precision and not compare:
                left = (220, 38, 38)
                right = (239, 68, 68)
                blob_right = "#F87171"
                blob_left = "#991B1B"
                subtitle = "#FEE2E2"
                version = "#FECACA"
                status_fg = self.colors["danger_dark"]
            else:
                left = (79, 70, 229)
                right = (124, 58, 237)
                blob_right = "#8B5CF6"
                blob_left = "#4338CA"
                subtitle = "#E0E7FF"
                version = "#E0E7FF"
                status_fg = self.colors["primary_dark"]

            steps = max(2, width)
            for x in range(steps):
                t = x / max(1, steps - 1)
                color = "#{:02X}{:02X}{:02X}".format(
                    int(left[0] + (right[0] - left[0]) * t),
                    int(left[1] + (right[1] - left[1]) * t),
                    int(left[2] + (right[2] - left[2]) * t),
                )
                canvas.create_rectangle(
                    x, 0, x + 2, height,
                    fill=color, outline=color,
                )

            canvas.create_oval(
                width - 210, -90, width + 60, 180,
                fill=blob_right, outline=""
            )
            canvas.create_oval(
                -80, 72, 120, 272,
                fill=blob_left, outline=""
            )

            canvas.create_text(
                30, 28,
                anchor="nw",
                text="Parallel Backup",
                fill="#FFFFFF",
                font=(self.font_family, 24, "bold"),
            )
            canvas.create_text(
                31, 67,
                anchor="nw",
                text="안전한 병렬 백업 · 중복 스냅샷 · SHA-256 검증",
                fill=subtitle,
                font=(self.font_family, 10),
            )

            status_text = self.compare_status_var.get() if compare else self.status_var.get()
            canvas.create_rectangle(
                width - 180, 27, width - 26, 61,
                fill="#FFFFFF", outline=""
            )
            canvas.create_text(
                width - 103, 44,
                text=status_text,
                fill=status_fg,
                font=(self.font_family, 9, "bold"),
            )
            canvas.create_text(
                width - 30, 113,
                anchor="e",
                text=f"v{APP_VERSION}",
                fill=version,
                font=(self.font_family, 8, "bold"),
            )

        canvas.bind("<Configure>", draw)
        self.header_canvas = canvas
        self._draw_header = draw
        return canvas

    def _refresh_header(self):
        if hasattr(self, "_draw_header"):
            self._draw_header()

    def _metric_card(self, parent, title, value_var, accent):
        wrapper, card = self._card(parent, padding=13)
        ttk.Label(
            card,
            text=title.upper(),
            background=self.colors["surface"],
            foreground=self.colors["muted"],
            font=(self.font_family, 8, "bold"),
        ).pack(anchor="w")
        value = ttk.Label(
            card,
            textvariable=value_var,
            style="Metric.TLabel",
        )
        value.pack(anchor="w", pady=(3, 0))
        tk.Frame(
            card,
            bg=accent,
            height=3,
        ).pack(fill="x", pady=(8, 0))
        return wrapper

    def build_ui(self):
        self.root.title("Parallel Backup")
        self.root.geometry("1040x820")
        self.root.minsize(920, 720)

        outer = tk.Frame(self.root, bg=self.colors["bg"])
        outer.pack(fill="both", expand=True)

        self._gradient_header(outer)

        scroll_area = tk.Frame(outer, bg=self.colors["bg"])
        scroll_area.pack(fill="both", expand=True)

        self.scroll_canvas = tk.Canvas(
            scroll_area,
            bg=self.colors["bg"],
            highlightthickness=0,
            bd=0,
        )
        self.scroll_canvas.pack(side="left", fill="both", expand=True)

        self.scrollbar = ttk.Scrollbar(
            scroll_area,
            orient="vertical",
            command=self.scroll_canvas.yview,
        )
        self.scrollbar.pack(side="right", fill="y")
        self.scroll_canvas.configure(yscrollcommand=self.scrollbar.set)

        content = tk.Frame(self.scroll_canvas, bg=self.colors["bg"])
        self.scroll_window = self.scroll_canvas.create_window(
            (0, 0), window=content, anchor="nw"
        )

        def update_scroll_region(_event=None):
            self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

        def resize_content(event):
            self.scroll_canvas.itemconfigure(self.scroll_window, width=event.width)

        content.bind("<Configure>", update_scroll_region)
        self.scroll_canvas.bind("<Configure>", resize_content)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

        mode_row = tk.Frame(content, bg=self.colors["bg"])
        mode_row.pack(fill="x", padx=14, pady=(12, 10))
        self.mode_cards = {}

        for key, title, desc, icon, color in [
            ("일반 백업", "일반 백업", "빠르고 안정적인 백업", "▣", self.colors["primary"]),
            ("정밀 검사 백업", "정밀 검사 백업", "SHA-256으로 더 꼼꼼하게 검증", "✓", self.colors["danger"]),
            ("파일 비교", "파일 비교", "두 폴더의 차이를 빠르게 확인", "↔", "#0F9D96"),
        ]:
            card = tk.Frame(
                mode_row,
                bg=self.colors["surface"],
                highlightbackground=self.colors["border"],
                highlightthickness=1,
                cursor="hand2",
            )
            card.pack(side="left", fill="both", expand=True, padx=4)
            icon_circle = tk.Label(
                card,
                text=icon,
                bg=color,
                fg="#FFFFFF",
                font=(self.font_family, 16, "bold"),
                width=3,
                height=1,
            )
            icon_circle.pack(side="left", padx=12, pady=12)
            text_frame = tk.Frame(card, bg=self.colors["surface"])
            text_frame.pack(side="left", fill="x", expand=True, pady=10)
            title_label = tk.Label(
                text_frame,
                text=title,
                bg=self.colors["surface"],
                fg=color if key != "일반 백업" else self.colors["primary"],
                font=(self.font_family, 11, "bold"),
            )
            title_label.pack(anchor="w")
            desc_label = tk.Label(
                text_frame,
                text=desc,
                bg=self.colors["surface"],
                fg=self.colors["muted"],
                font=(self.font_family, 8),
            )
            desc_label.pack(anchor="w", pady=(2, 0))
            self.mode_cards[key] = {
                "frame": card,
                "icon": icon_circle,
                "title": title_label,
                "text": desc_label,
                "color": color,
            }
            for widget in (card, icon_circle, text_frame, title_label, desc_label):
                widget.bind("<Button-1>", lambda _e, value=key: self.set_app_mode(value))

        self.mode_content = tk.Frame(content, bg=self.colors["bg"])
        self.mode_content.pack(fill="both", expand=True)

        self.backup_view = tk.Frame(self.mode_content, bg=self.colors["bg"])
        self.compare_view = tk.Frame(self.mode_content, bg=self.colors["bg"])

        self._build_backup_view()
        self._build_compare_view()
        self.set_app_mode(self.app_mode_var.get())

    def _build_backup_view(self):
        top = ttk.Frame(self.backup_view)
        top.pack(fill="x", pady=(0, 12))

        intro = ttk.Frame(top)
        intro.pack(side="left", fill="x", expand=True)
        ttk.Label(
            intro,
            text="백업 작업",
            font=(self.font_family, 16, "bold"),
            foreground=self.colors["text"],
        ).pack(anchor="w")
        ttk.Label(
            intro,
            text="원본과 대상만 정하면 나머지는 자동으로 처리합니다.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        self.metric_targets = tk.StringVar(value=str(len(self.destinations)))
        self.metric_keep = tk.StringVar(value=str(self.keep_var.get()))
        self.metric_parallel = tk.StringVar(value=str(self.parallel_var.get()))

        metrics = ttk.Frame(top)
        metrics.pack(side="right")

        self._metric_card(
            metrics, "Targets", self.metric_targets, self.colors["primary"]
        ).pack(side="left", padx=(0, 8))
        self._metric_card(
            metrics, "Retention", self.metric_keep, self.colors["secondary"]
        ).pack(side="left", padx=(0, 8))
        self._metric_card(
            metrics, "Workers", self.metric_parallel, self.colors["success"]
        ).pack(side="left")

        grid = ttk.Frame(self.backup_view)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        grid.rowconfigure(1, weight=1)

        wrapper, source_card = self._card(grid, padding=17)
        wrapper.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 10))
        ttk.Label(
            source_card,
            text="SOURCE & PROFILE",
            foreground=self.colors["primary"],
            font=(self.font_family, 9, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            source_card,
            text="백업 원본과 스냅샷 이름",
            style="Title.TLabel",
        ).pack(anchor="w", pady=(3, 2))
        ttk.Label(
            source_card,
            text="날짜와 시간이 자동으로 이름에 추가됩니다.",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(0, 12))

        ttk.Label(
            source_card,
            text="원본 폴더",
            foreground=self.colors["muted"],
            font=(self.font_family, 9, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        source_row = ttk.Frame(source_card, style="Card.TFrame")
        source_row.pack(fill="x")
        self.source_entry = tk.Entry(
            source_row,
            textvariable=self.source_var,
            state="normal",
            takefocus=True,
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["primary"],
            bg=self.colors["surface"],
            fg=self.colors["text"],
            insertbackground=self.colors["primary"],
            font=(self.font_family, 10),
        )
        self.source_entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.source_entry.bind("<Button-1>", lambda event: self.source_entry.focus_set())
        self.source_entry.bind("<Control-a>", lambda event: (self.source_entry.selection_range(0, tk.END), "break")[1])
        ttk.Button(
            source_row,
            text="찾기",
            command=self.select_source,
        ).pack(side="left", padx=(8, 0))

        ttk.Label(
            source_card,
            text="백업 이름",
            foreground=self.colors["muted"],
            font=(self.font_family, 9, "bold"),
        ).pack(anchor="w", pady=(13, 4))
        ttk.Entry(
            source_card,
            textvariable=self.name_var,
        ).pack(fill="x")

        ttk.Label(
            source_card,
            text="예: uni_mcp → uni_mcp_YYYYMMDD_HHMMSS",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(5, 0))

        wrapper, options_card = self._card(grid, padding=17)
        wrapper.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=(0, 10))

        ttk.Label(
            options_card,
            text="BACKUP POLICY",
            foreground=self.colors["secondary"],
            font=(self.font_family, 9, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            options_card,
            text="안전성과 저장공간 정책",
            style="Title.TLabel",
        ).pack(anchor="w", pady=(3, 2))
        ttk.Label(
            options_card,
            text="권장 기본값을 그대로 사용해도 됩니다.",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(0, 10))

        option_grid = ttk.Frame(options_card, style="Card.TFrame")
        option_grid.pack(fill="x", pady=(4, 0))
        option_grid.columnconfigure(0, weight=1)
        option_grid.columnconfigure(1, weight=1)

        for row, (text_label, variable) in enumerate([
            ("증분 백업", self.incremental_var),
            ("하드링크 재사용", self.hardlink_var),
        ]):
            ttk.Checkbutton(
                option_grid,
                text=text_label,
                variable=variable,
            ).grid(
                row=row // 2,
                column=row % 2,
                sticky="w",
                padx=(0, 8),
                pady=5,
            )

        control_row = ttk.Frame(options_card, style="Card.TFrame")
        control_row.pack(fill="x", pady=(11, 0))

        ttk.Label(
            control_row,
            text="병렬 대상",
            foreground=self.colors["muted"],
            font=(self.font_family, 9, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            control_row,
            from_=1, to=16,
            width=7,
            textvariable=self.parallel_var,
            command=lambda: self._refresh_metrics(),
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0), padx=(0, 8))

        ttk.Label(
            control_row,
            text="보존 스냅샷",
            foreground=self.colors["muted"],
            font=(self.font_family, 9, "bold"),
        ).grid(row=0, column=1, sticky="w")
        ttk.Spinbox(
            control_row,
            from_=1, to=999,
            width=7,
            textvariable=self.keep_var,
            command=lambda: self._refresh_metrics(),
        ).grid(row=1, column=1, sticky="ew", pady=(4, 0), padx=(0, 8))

        control_row.columnconfigure(0, weight=1)
        control_row.columnconfigure(1, weight=1)

        wrapper, exclude_card = self._card(grid, padding=17)
        wrapper.grid(row=1, column=0, sticky="nsew", padx=(0, 7), pady=(0, 10))

        ttk.Label(
            exclude_card,
            text="EXCLUSIONS",
            foreground=self.colors["primary"],
            font=(self.font_family, 9, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            exclude_card,
            text="제외할 파일과 폴더",
            style="Title.TLabel",
        ).pack(anchor="w", pady=(3, 2))
        ttk.Label(
            exclude_card,
            text="쉼표 또는 줄바꿈으로 여러 패턴을 입력할 수 있습니다.",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(0, 10))

        ttk.Entry(
            exclude_card,
            textvariable=self.exclude_var,
        ).pack(fill="x")

        ttk.Label(
            exclude_card,
            text=".git, Library, Temp, *.log, *.tmp, node_modules",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(6, 10))

        action_row = ttk.Frame(exclude_card, style="Card.TFrame")
        action_row.pack(fill="x", side="bottom")

        self.backup_button = ttk.Button(
            action_row,
            text="  병렬 백업 시작",
            command=self.start_backup,
            style="Primary.TButton",
        )
        self.backup_button.pack(side="left")

        self.cancel_button = ttk.Button(
            action_row,
            text="정지",
            command=self.cancel_backup,
            style="Danger.TButton",
            state="disabled",
        )
        self.cancel_button.pack(side="left", padx=7)

        ttk.Button(
            action_row,
            text="복구",
            command=self.restore_backup,
        ).pack(side="left")

        ttk.Button(
            action_row,
            text="프로필 저장",
            command=self.save_profile,
            style="Ghost.TButton",
        ).pack(side="right")

        wrapper, destinations_card = self._card(grid, padding=17)
        wrapper.grid(row=1, column=1, sticky="nsew", padx=(7, 0), pady=(0, 10))
        ttk.Label(
            destinations_card,
            text="DESTINATIONS",
            foreground=self.colors["secondary"],
            font=(self.font_family, 9, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            destinations_card,
            text="백업 대상",
            style="Title.TLabel",
        ).pack(anchor="w", pady=(3, 2))
        ttk.Label(
            destinations_card,
            text="여러 드라이브를 동시에 지정할 수 있습니다.",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(0, 9))

        ttk.Label(
            destinations_card,
            text="여러 경로를 추가하면 같은 원본을 동시에 백업합니다.",
            style="Body.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        self.destination_rows = []

        self.dest_rows_holder = tk.Frame(
            destinations_card,
            bg=self.colors["surface"],
        )
        self.dest_rows_holder.pack(fill="x", expand=False)

        dest_add_row = tk.Frame(
            destinations_card,
            bg=self.colors["surface"],
        )
        dest_add_row.pack(fill="x", pady=(9, 0))

        ttk.Button(
            dest_add_row,
            text="+ 백업 대상 추가",
            command=self.add_destination_row,
            style="Primary.TButton",
        ).pack(side="left")

        ttk.Button(
            dest_add_row,
            text="전체 삭제",
            command=self.clear_destinations,
            style="Ghost.TButton",
        ).pack(side="left", padx=7)

        status_card = tk.Frame(
            self.backup_view,
            bg="#111827",
            highlightthickness=0,
        )
        status_card.pack(fill="x", pady=(2, 9))

        status_inner = ttk.Frame(status_card, style="Card.TFrame", padding=(15, 12))
        status_inner.configure(style="Card.TFrame")
        status_card.configure(bg="#111827")

        status_left = tk.Frame(status_card, bg="#111827")
        status_left.pack(side="left", fill="x", expand=True, padx=15, pady=11)
        tk.Label(
            status_left,
            text="BACKUP STATUS",
            bg="#111827",
            fg="#A5B4FC",
            font=(self.font_family, 8, "bold"),
        ).pack(anchor="w")
        status_row = tk.Frame(status_left, bg="#111827")
        status_row.pack(fill="x", pady=(2, 0))

        self.status_label = tk.Label(
            status_row,
            textvariable=self.status_var,
            bg="#111827",
            fg="#FFFFFF",
            font=(self.font_family, 11, "bold"),
        )
        self.status_label.pack(side="left", anchor="w")

        self.elapsed_label = tk.Label(
            status_row,
            textvariable=self.elapsed_var,
            bg="#111827",
            fg="#CBD5E1",
            font=(self.font_family, 9, "bold"),
        )
        self.elapsed_label.pack(side="left", padx=(12, 0), anchor="w")

        self.eta_label = tk.Label(
            status_row,
            textvariable=self.eta_var,
            bg="#111827",
            fg="#CBD5E1",
            font=(self.font_family, 9, "bold"),
        )
        self.eta_label.pack(side="left", padx=(12, 0), anchor="w")

        progress_wrap = tk.Frame(status_card, bg="#111827")
        progress_wrap.pack(side="right", fill="x", expand=True, padx=15, pady=14)
        self.progress = ttk.Progressbar(
            progress_wrap,
            mode="determinate",
            maximum=1,
            style="Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x")

        timeline_wrapper, timeline_card = self._card(self.backup_view, padding=12)
        timeline_wrapper.pack(fill="x", pady=(0, 9))
        self.timeline_canvas = tk.Canvas(
            timeline_card,
            height=132,
            bg=self.colors["surface"],
            highlightthickness=0,
            bd=0,
        )
        self.timeline_canvas.pack(fill="x")
        self.timeline_canvas.bind("<Configure>", self._draw_timeline)
        self.root.after_idle(self._draw_timeline)

        log_wrapper, log_card = self._card(self.backup_view, padding=13)
        log_wrapper.pack(fill="both", expand=True)
        ttk.Label(
            log_card,
            text="ACTIVITY LOG",
            foreground=self.colors["primary"],
            font=(self.font_family, 8, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            log_card,
            text="최근 작업 기록",
            style="Title.TLabel",
        ).pack(anchor="w", pady=(2, 6))

        log_holder = tk.Frame(
            log_card,
            bg=self.colors["surface"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
        )
        log_holder.pack(fill="both", expand=True)

        self.log = tk.Text(
            log_holder,
            height=9,
            state="disabled",
            wrap="word",
            bg="#FAFBFF",
            fg="#334155",
            insertbackground=self.colors["primary"],
            selectbackground="#E0E7FF",
            selectforeground=self.colors["primary_dark"],
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            font=("Consolas", 9),
        )
        self.log.pack(fill="both", expand=True)

        self._refresh_metrics()



    def _build_compare_view(self):
        view = self.compare_view

        title_row = tk.Frame(view, bg=self.colors["bg"])
        title_row.pack(fill="x", pady=(2, 10))
        tk.Label(
            title_row,
            text="파일 비교",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=(self.font_family, 18, "bold"),
        ).pack(side="left")
        tk.Label(
            title_row,
            text="두 폴더/드라이브의 파일 차이를 빠르고 정확하게 비교합니다.",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=(self.font_family, 9),
        ).pack(side="left", padx=(10, 0), pady=(5, 0))

        compare_card = tk.Frame(
            view, bg=self.colors["surface"],
            highlightbackground=self.colors["border"], highlightthickness=1
        )
        compare_card.pack(fill="x", pady=(0, 10))
        tk.Label(
            compare_card, text="비교 대상 1 (원본)",
            bg=self.colors["surface"], fg=self.colors["text"],
            font=(self.font_family, 10, "bold")
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 6))
        tk.Label(
            compare_card, text="비교 대상 2 (비교할 폴더)",
            bg=self.colors["surface"], fg=self.colors["text"],
            font=(self.font_family, 10, "bold")
        ).grid(row=0, column=1, sticky="w", padx=16, pady=(14, 6))

        self._make_compare_path_row(
            compare_card, 1, self.compare_source_var, "원본 폴더 선택", 0
        )
        self._make_compare_path_row(
            compare_card, 1, self.compare_target_var, "비교 폴더 선택", 1
        )

        swap_button = ttk.Button(
            compare_card, text="↔", command=self.swap_compare_paths,
            style="Ghost.TButton"
        )
        swap_button.grid(row=1, column=2, padx=5, sticky="s")
        compare_card.columnconfigure(0, weight=1)
        compare_card.columnconfigure(1, weight=1)

        settings = tk.Frame(
            view, bg=self.colors["surface"],
            highlightbackground=self.colors["border"], highlightthickness=1
        )
        settings.pack(fill="x", pady=(0, 10))
        tk.Label(
            settings, text="비교 설정",
            bg=self.colors["surface"], fg=self.colors["secondary"],
            font=(self.font_family, 9, "bold")
        ).pack(anchor="w", padx=16, pady=(12, 4))
        setting_row = tk.Frame(settings, bg=self.colors["surface"])
        setting_row.pack(fill="x", padx=16, pady=(0, 10))
        ttk.Checkbutton(setting_row, text="파일 크기", variable=self.compare_size_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(setting_row, text="수정 시간", variable=self.compare_mtime_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(setting_row, text="SHA-256 정밀 비교", variable=self.compare_sha_var).pack(side="left")

        compare_actions = tk.Frame(settings, bg=self.colors["surface"])
        compare_actions.pack(fill="x", padx=16, pady=(0, 14))
        self.compare_button = ttk.Button(
            compare_actions, text="  비교 시작", command=self.start_compare,
            style="Primary.TButton"
        )
        self.compare_button.pack(side="left")
        ttk.Button(
            compare_actions, text="초기화", command=self.reset_compare,
            style="Ghost.TButton"
        ).pack(side="left", padx=7)

        info = tk.Label(
            settings,
            text="파일 크기 + 수정 시간을 기본으로 비교하고, SHA-256을 켜면 내용까지 검사합니다.",
            bg="#EEF2FF", fg=self.colors["primary_dark"],
            font=(self.font_family, 8),
            anchor="w", padx=12, pady=8
        )
        info.pack(fill="x", padx=16, pady=(0, 14))

        status_card = tk.Frame(view, bg="#111827", highlightthickness=0)
        status_card.pack(fill="x", pady=(0, 10))
        status_left = tk.Frame(status_card, bg="#111827")
        status_left.pack(side="left", fill="x", expand=True, padx=15, pady=11)
        tk.Label(
            status_left, text="COMPARE STATUS",
            bg="#111827", fg="#67E8F9",
            font=(self.font_family, 8, "bold")
        ).pack(anchor="w")
        compare_row = tk.Frame(status_left, bg="#111827")
        compare_row.pack(fill="x", pady=(2,0))
        self.compare_status_label = tk.Label(
            compare_row, textvariable=self.compare_status_var,
            bg="#111827", fg="#FFFFFF",
            font=(self.font_family, 11, "bold")
        )
        self.compare_status_label.pack(side="left")
        tk.Label(
            compare_row, textvariable=self.compare_elapsed_var,
            bg="#111827", fg="#CBD5E1",
            font=(self.font_family, 9, "bold")
        ).pack(side="left", padx=(12,0))
        self.compare_eta_label = tk.Label(
            compare_row, textvariable=self.compare_eta_var,
            bg="#111827", fg="#CBD5E1",
            font=(self.font_family, 9, "bold")
        )
        self.compare_eta_label.pack(side="left", padx=(12,0))
        compare_progress_wrap = tk.Frame(status_card, bg="#111827")
        compare_progress_wrap.pack(side="right", fill="x", expand=True, padx=15, pady=14)
        self.compare_progress = ttk.Progressbar(
            compare_progress_wrap, mode="determinate",
            maximum=1, style="Horizontal.TProgressbar"
        )
        self.compare_progress.pack(fill="x")

        timeline_card = tk.Frame(
            view, bg=self.colors["surface"],
            highlightbackground=self.colors["border"], highlightthickness=1
        )
        timeline_card.pack(fill="x", pady=(0,10))
        self.compare_timeline_canvas = tk.Canvas(
            timeline_card, height=115, bg=self.colors["surface"],
            highlightthickness=0, bd=0
        )
        self.compare_timeline_canvas.pack(fill="x", padx=8, pady=8)
        self.compare_timeline_canvas.bind("<Configure>", self._draw_compare_timeline)
        self._set_compare_timeline(0, reset=True)

        result_wrap = tk.Frame(view, bg=self.colors["bg"])
        result_wrap.pack(fill="both", expand=True)
        summary_card = tk.Frame(
            result_wrap, bg=self.colors["surface"],
            highlightbackground=self.colors["border"], highlightthickness=1
        )
        summary_card.pack(side="left", fill="both", expand=True, padx=(0,6))
        tk.Label(summary_card, text="비교 결과", bg=self.colors["surface"],
                 fg=self.colors["primary"], font=(self.font_family,9,"bold")).pack(anchor="w", padx=12, pady=(12,4))
        self.compare_summary_labels = {}
        summary_row = tk.Frame(summary_card, bg=self.colors["surface"])
        summary_row.pack(fill="x", padx=10)
        for key, label, color in [
            ("same","일치",self.colors["success"]),
            ("different","다른 파일",self.colors["danger"]),
            ("left_only","원본만", "#D97706"),
            ("right_only","비교 대상만", self.colors["secondary"]),
        ]:
            card=tk.Frame(summary_row,bg="#F8FAFC",highlightbackground=self.colors["border"],highlightthickness=1)
            card.pack(side="left",fill="both",expand=True,padx=3)
            tk.Label(card,text=label,bg="#F8FAFC",fg=self.colors["muted"],font=(self.font_family,8,"bold")).pack(pady=(8,0))
            var=tk.StringVar(value="0")
            self.compare_summary_labels[key]=var
            tk.Label(card,textvariable=var,bg="#F8FAFC",fg=color,font=(self.font_family,15,"bold")).pack(pady=(1,8))
        self.compare_result_info = tk.Label(summary_card,text="비교 결과가 여기에 표시됩니다.",
                                            bg=self.colors["surface"],fg=self.colors["muted"],
                                            font=(self.font_family,8),anchor="w")
        self.compare_result_info.pack(fill="x",padx=12,pady=10)

        table_card = tk.Frame(
            result_wrap,bg=self.colors["surface"],
            highlightbackground=self.colors["border"],highlightthickness=1
        )
        table_card.pack(side="left",fill="both",expand=True,padx=(6,0))
        tk.Label(table_card,text="변경된 파일",bg=self.colors["surface"],fg=self.colors["primary"],
                 font=(self.font_family,9,"bold")).pack(anchor="w",padx=12,pady=(12,5))
        table_holder=tk.Frame(table_card,bg=self.colors["surface"])
        table_holder.pack(fill="both",expand=True,padx=10,pady=(0,10))
        columns=("status","path","left_size","right_size")
        self.compare_tree=ttk.Treeview(table_holder,columns=columns,show="headings",height=10)
        headings={"status":"상태","path":"경로","left_size":"원본","right_size":"비교 대상"}
        widths={"status":90,"path":360,"left_size":90,"right_size":90}
        for col in columns:
            self.compare_tree.heading(col,text=headings[col])
            self.compare_tree.column(col,width=widths[col],anchor="w")
        tree_scroll=ttk.Scrollbar(table_holder,orient="vertical",command=self.compare_tree.yview)
        self.compare_tree.configure(yscrollcommand=tree_scroll.set)
        self.compare_tree.pack(side="left",fill="both",expand=True)
        tree_scroll.pack(side="right",fill="y")

        log_card=tk.Frame(view,bg=self.colors["surface"],highlightbackground=self.colors["border"],highlightthickness=1)
        log_card.pack(fill="both",expand=True,pady=(10,0))
        tk.Label(log_card,text="활동 로그",bg=self.colors["surface"],fg=self.colors["primary"],
                 font=(self.font_family,9,"bold")).pack(anchor="w",padx=12,pady=(10,4))
        self.compare_log=tk.Text(log_card,height=7,state="disabled",wrap="word",
                                 bg="#FAFBFF",fg="#334155",relief="flat",bd=0,
                                 font=("Consolas",8),padx=10,pady=7)
        self.compare_log.pack(fill="both",expand=True,padx=10,pady=(0,10))

    def _make_compare_path_row(self, parent, row, variable, title, column):
        holder=tk.Frame(parent,bg=self.colors["surface"])
        holder.grid(row=row,column=column,sticky="ew",padx=16,pady=(0,14))
        entry=tk.Entry(holder,textvariable=variable,font=(self.font_family,10),
                        relief="solid",bd=1,highlightthickness=1,
                        highlightbackground=self.colors["border"],
                        highlightcolor=self.colors["primary"],
                        bg=self.colors["surface"],fg=self.colors["text"],
                        insertbackground=self.colors["primary"])
        entry.pack(side="left",fill="x",expand=True,ipady=7)
        ttk.Button(holder,text="찾기",command=lambda v=variable,t=title:self.select_compare_path(v,t)).pack(side="left",padx=(8,0))

    def select_compare_path(self, variable, title):
        path=filedialog.askdirectory(title=title)
        if path:
            variable.set(path)

    def swap_compare_paths(self):
        left=self.compare_source_var.get()
        self.compare_source_var.set(self.compare_target_var.get())
        self.compare_target_var.set(left)

    def reset_compare(self):
        self.compare_source_var.set("")
        self.compare_target_var.set("")
        self.compare_summary={"same":0,"different":0,"left_only":0,"right_only":0}
        self.compare_results=[]
        for var in self.compare_summary_labels.values():
            var.set("0")
        for item in self.compare_tree.get_children():
            self.compare_tree.delete(item)
        self.compare_result_info.configure(text="비교 결과가 여기에 표시됩니다.")
        self.compare_status_var.set("대기 중")
        self.compare_elapsed_var.set("경과 00:00:00")
        self.compare_eta_var.set("예상 계산 중...")
        self._set_compare_timeline(0,reset=True)
        self._refresh_header()

    def _compare_log(self, message):
        def update():
            self.compare_log.configure(state="normal")
            self.compare_log.insert("end", message + "\n")
            self.compare_log.see("end")
            self.compare_log.configure(state="disabled")
        self.root.after(0, update)

    def _format_size(self, size):
        value=float(size)
        units=("B","KB","MB","GB","TB")
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"

    def _scan_compare_folder(self, root):
        files={}
        for base, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".parallel-backup.partial-")]
            base_path=Path(base)
            for filename in filenames:
                path=base_path / filename
                rel=path.relative_to(root).as_posix()
                try:
                    stat=path.stat()
                except OSError:
                    continue
                files[rel]={
                    "size":stat.st_size,
                    "mtime_ns":stat.st_mtime_ns,
                }
        return files

    def _compare_file_maps(self, left_root, right_root, left_files, right_files, use_size, use_mtime, use_sha):
        results=[]
        all_paths=sorted(set(left_files)|set(right_files))
        for rel in all_paths:
            left=left_files.get(rel)
            right=right_files.get(rel)
            if left is None:
                results.append(("추가",rel,None,right["size"]))
                continue
            if right is None:
                results.append(("누락",rel,left["size"],None))
                continue
            same=True
            if use_size and left["size"] != right["size"]:
                same=False
            if use_mtime and left["mtime_ns"] != right["mtime_ns"]:
                same=False
            if use_sha and same:
                same = sha256_file(left_root/Path(rel)) == sha256_file(right_root/Path(rel))
            elif use_sha and not same:
                same = sha256_file(left_root/Path(rel)) == sha256_file(right_root/Path(rel))
            if same:
                results.append(("일치",rel,left["size"],right["size"]))
            else:
                results.append(("다름",rel,left["size"],right["size"]))
        return results

    def _set_compare_timeline(self, stage, reset=False, error=False, success=False):
        steps=[
            ("원본 스캔","folder"),
            ("백업 스캔","folder"),
            ("파일 비교","compare"),
            ("변경 분석","list"),
            ("결과 정리","refresh"),
            ("보고서","report"),
            ("완료","flag"),
        ]
        self.compare_timeline_state=(stage,error,success,reset)
        def draw():
            canvas=self.compare_timeline_canvas
            canvas.delete("all")
            width=max(700,canvas.winfo_width())
            left=55
            right=width-55
            y=35
            gap=(right-left)/max(1,len(steps)-1)
            for i in range(len(steps)-1):
                color="#0F9D96" if success or i<stage else "#CBD5E1"
                canvas.create_line(left+gap*i+22,y,right if i==len(steps)-2 else left+gap*(i+1)-22,y,fill=color,width=3)
            for i,(label,icon) in enumerate(steps):
                x=left+gap*i
                active=i==stage and not success and not error
                done=success or i<stage
                err=error and i==stage
                fill="#EF4444" if err else ("#0F9D96" if done else ("#4F46E5" if active else "#F8FAFC"))
                outline=fill if fill!="#F8FAFC" else "#CBD5E1"
                canvas.create_oval(x-21,y-21,x+21,y+21,fill=fill,outline=outline,width=2)
                canvas.create_text(x,y,text="✓" if done else ("×" if err else ("⌕" if icon=="compare" else "●")),
                                  fill="#FFFFFF" if fill!="#F8FAFC" else "#94A3B8",
                                  font=(self.font_family,10,"bold"))
                canvas.create_text(x,72,text=f"{i+1}. {label}",fill="#0F172A" if done or active else "#64748B",
                                  font=(self.font_family,8,"bold"))
                canvas.create_text(x,93,text="완료" if done else ("오류" if err else ("진행 중..." if active else "대기 중")),
                                  fill="#0F9D96" if done else ("#EF4444" if err else ("#4F46E5" if active else "#94A3B8")),
                                  font=(self.font_family,8))
        self.root.after(0,draw)

    def _start_compare_timer(self):
        self._stop_compare_timer()
        self.compare_started_at=time.monotonic()
        self.compare_elapsed_var.set("경과 00:00:00")
        self.compare_eta_var.set("예상 계산 중...")
        self.compare_timer_job=self.root.after(2000,self._update_compare_timer)

    def _update_compare_timer(self):
        if self.compare_started_at is None:
            self.compare_timer_job=None
            return
        elapsed=time.monotonic()-self.compare_started_at
        self.compare_elapsed_var.set(f"경과 {self._format_elapsed(elapsed)}")
        if self.compare_progress_value>0 and self.compare_progress_total>self.compare_progress_value:
            rate=self.compare_progress_value/max(1,elapsed)
            eta=(self.compare_progress_total-self.compare_progress_value)/rate if rate else 0
            self.compare_eta_var.set(f"예상 {self._format_elapsed(eta)}")
        else:
            self.compare_eta_var.set("예상 계산 중...")
        self.compare_timer_job=self.root.after(2000,self._update_compare_timer)

    def _stop_compare_timer(self, complete=False):
        if self.compare_timer_job is not None:
            try:self.root.after_cancel(self.compare_timer_job)
            except tk.TclError:pass
            self.compare_timer_job=None
        if self.compare_started_at is not None:
            elapsed=time.monotonic()-self.compare_started_at
            self.compare_elapsed_var.set(f"소요 {self._format_elapsed(elapsed)}")
            self.compare_eta_var.set("예상 --:--:--" if not complete else "예상 00:00:00")
            self.compare_started_at=None

    def _set_compare_progress(self,value,total,status=None):
        self.compare_progress_value=max(0,min(total,value))
        self.compare_progress_total=max(1,total)
        self.compare_progress.configure(maximum=self.compare_progress_total,value=self.compare_progress_value)
        if status:
            self.compare_status_var.set(status)
            self._refresh_header()

    def start_compare(self):
        if self.running:
            messagebox.showwarning("사용 중","백업/복구가 실행 중입니다.")
            return
        left=Path(self.compare_source_var.get().strip())
        right=Path(self.compare_target_var.get().strip())
        if not left.is_dir() or not right.is_dir():
            messagebox.showerror("비교 오류","두 비교 경로를 모두 지정하세요.")
            return
        if left.resolve()==right.resolve():
            messagebox.showerror("비교 오류","같은 폴더는 비교할 수 없습니다.")
            return
        self.running=True
        self.compare_cancel_event=threading.Event()
        self._set_compare_progress(0,1,"원본 스캔 중...")
        self._set_compare_timeline(0)
        self._start_compare_timer()
        self.compare_button.configure(state="disabled")
        self._compare_log(f"[START] {left}")
        self._compare_log(f"[TARGET] {right}")
        threading.Thread(
            target=self.run_compare,
            args=(left.resolve(),right.resolve(),self.compare_size_var.get(),self.compare_mtime_var.get(),self.compare_sha_var.get()),
            daemon=True
        ).start()

    def run_compare(self,left,right,use_size,use_mtime,use_sha):
        try:
            self._set_compare_timeline(0)
            left_files=self._scan_compare_folder(left)
            self._set_compare_timeline(1)
            right_files=self._scan_compare_folder(right)
            all_count=max(1,len(set(left_files)|set(right_files)))
            self._set_compare_progress(0,all_count,"파일 비교 중...")
            results=[]
            paths=sorted(set(left_files)|set(right_files))
            for index,rel in enumerate(paths,1):
                if hasattr(self,"compare_cancel_event") and self.compare_cancel_event.is_set():
                    raise RuntimeError("비교가 정지되었습니다.")
                left_item=left_files.get(rel)
                right_item=right_files.get(rel)
                results.append((rel,left_item,right_item))
                self.compare_progress_value=index
                if index % 250==0 or index==all_count:
                    self.root.after(0,lambda i=index:self._set_compare_progress(i,all_count,f"파일 비교 중... {i:,}/{all_count:,}"))
            self._set_compare_timeline(2)
            final=self._compare_file_maps(left,right,left_files,right_files,use_size,use_mtime,use_sha)
            self._set_compare_timeline(3)
            same=sum(1 for r in final if r[0]=="일치")
            different=sum(1 for r in final if r[0]=="다름")
            left_only=sum(1 for r in final if r[0]=="누락")
            right_only=sum(1 for r in final if r[0]=="추가")
            summary={"same":same,"different":different,"left_only":left_only,"right_only":right_only}
            self._set_compare_timeline(4)
            def finish():
                self.compare_summary=summary
                for key,var in self.compare_summary_labels.items():var.set(f"{summary[key]:,}")
                for item in self.compare_tree.get_children():self.compare_tree.delete(item)
                shown=0
                for status,rel,ls,rs in final:
                    if status=="일치":continue
                    if shown>=2000:break
                    self.compare_tree.insert("", "end", values=(status,rel,self._format_size(ls) if ls is not None else "-",self._format_size(rs) if rs is not None else "-"))
                    shown+=1
                self.compare_result_info.configure(text=f"전체 {len(final):,}개 · 변경/누락/추가 {different+left_only+right_only:,}개 · 표에는 최대 2,000개 표시")
                self.compare_status_var.set(f"비교 완료 · {len(final):,}개")
                self._set_compare_timeline(6,success=True)
                self._stop_compare_timer(complete=True)
                self.running=False
                self.compare_button.configure(state="normal")
                self._refresh_header()
            self.root.after(0,finish)
        except Exception as exc:
            error_text=str(exc)
            self._compare_log(f"[FAIL] {error_text}")
            def fail():
                self.compare_status_var.set(f"실패 · {error_text}")
                self._set_compare_timeline(2,error=True)
                self._stop_compare_timer()
                self.running=False
                self.compare_button.configure(state="normal")
                self._refresh_header()
                messagebox.showerror("파일 비교 실패",error_text)
            self.root.after(0,fail)

    def set_app_mode(self, mode):
        if mode not in ("일반 백업", "정밀 검사 백업", "파일 비교"):
            mode = "일반 백업"
        self.app_mode_var.set(mode)

        if mode in ("일반 백업", "정밀 검사 백업"):
            self.backup_mode_var.set(mode)
            self.compare_view.pack_forget()
            self.backup_view.pack(fill="both", expand=True)
            self.timeline_steps = [
                ("원본 분석", "scan"),
                ("파일 목록 생성", "files"),
                ("백업 복사", "copy"),
                ("무결성 검사", "check"),
                ("압축 (ZIP)", "zip"),
                ("최종 검증", "shield"),
                ("완료", "flag"),
            ]
            self._refresh_header()
        else:
            self.backup_view.pack_forget()
            self.compare_view.pack(fill="both", expand=True)
            self.compare_status_var.set("대기 중")
            self._refresh_header()
            self._set_compare_timeline(0, reset=True)

        for key, card in self.mode_cards.items():
            active = key == mode
            color = card["color"]
            if active:
                card["frame"].configure(
                    bg=color,
                    highlightbackground=color,
                    highlightthickness=2,
                )
                card["icon"].configure(bg="#FFFFFF", fg=color)
                card["title"].configure(bg=color, fg="#FFFFFF")
                card["text"].configure(bg=color, fg="#F8FAFC")
                card["frame"].configure(relief="solid")
            else:
                card["frame"].configure(
                    bg=self.colors["surface"],
                    highlightbackground=self.colors["border"],
                    highlightthickness=1,
                )
                card["icon"].configure(bg=color, fg="#FFFFFF")
                card["title"].configure(bg=self.colors["surface"], fg=color)
                card["text"].configure(bg=self.colors["surface"], fg=self.colors["muted"])

        self.root.after_idle(lambda: self.scroll_canvas.configure(
            scrollregion=self.scroll_canvas.bbox("all")
        ))

    def _draw_compare_timeline(self, _event=None):
        self._set_compare_timeline(
            getattr(self, "compare_timeline_state", (0, False, False, True))[0],
            error=getattr(self, "compare_timeline_state", (0, False, False, True))[1],
            success=getattr(self, "compare_timeline_state", (0, False, False, True))[2],
        )

    def _on_mousewheel(self, event):
        delta = -1 * int(event.delta / 120)
        if delta:
            self.scroll_canvas.yview_scroll(delta, "units")

    def _bind_mousewheel(self, _event=None):
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event=None):
        self.root.unbind_all("<MouseWheel>")

    def set_backup_mode(self, mode):
        if mode not in ("일반 백업", "정밀 검사 백업"):
            return
        self.backup_mode_var.set(mode)
        self.set_app_mode(mode)
        self._refresh_header()
        self.save_profile(silent=True)

    def _refresh_backup_mode_segment(self):
        return

    def _refresh_metrics(self):
        if hasattr(self, "metric_targets"):
            self.metric_targets.set(str(len(self.destinations)))
        if hasattr(self, "metric_keep"):
            try:
                self.metric_keep.set(str(int(self.keep_var.get())))
            except (ValueError, tk.TclError):
                pass
        if hasattr(self, "metric_parallel"):
            try:
                self.metric_parallel.set(str(int(self.parallel_var.get())))
            except (ValueError, tk.TclError):
                pass

    def select_source(self):
        path = filedialog.askdirectory(title="원본 폴더 선택")
        if path:
            self.source_var.set(path)
            self.save_profile(silent=True)

    def _sync_destinations(self):
        destinations = []
        for row in self.destination_rows:
            value = row["var"].get().strip().strip('"')
            if value and value not in destinations:
                destinations.append(value)
        self.destinations = destinations
        self._refresh_metrics()

    def _build_destination_row(self, value=""):
        row_frame = tk.Frame(
            self.dest_rows_holder,
            bg=self.colors["surface"],
        )
        row_frame.pack(fill="x", pady=3)

        number = tk.Label(
            row_frame,
            text=f"{len(self.destination_rows) + 1}",
            bg=self.colors["soft_indigo"],
            fg=self.colors["primary_dark"],
            width=3,
            font=(self.font_family, 9, "bold"),
        )
        number.pack(side="left", padx=(0, 7), ipady=5)

        path_var = tk.StringVar(value=value)
        entry = tk.Entry(
            row_frame,
            textvariable=path_var,
            font=(self.font_family, 9),
            bg=self.colors["surface"],
            fg=self.colors["text"],
            insertbackground=self.colors["primary"],
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["primary"],
        )
        entry.pack(side="left", fill="x", expand=True, ipady=6)

        ttk.Button(
            row_frame,
            text="찾기",
            command=lambda e=entry, v=path_var: self.select_destination_row(e, v),
        ).pack(side="left", padx=(7, 5))

        ttk.Button(
            row_frame,
            text="×",
            width=3,
            command=lambda frame=row_frame: self.remove_destination_row(frame),
            style="Ghost.TButton",
        ).pack(side="left")

        row_info = {"frame": row_frame, "var": path_var, "number": number}
        self.destination_rows.append(row_info)
        path_var.trace_add("write", lambda *_args: self._sync_destinations())
        self._renumber_destination_rows()

    def _renumber_destination_rows(self):
        for index, row in enumerate(self.destination_rows, start=1):
            row["number"].configure(text=str(index))

    def add_destination_row(self, value=""):
        self._build_destination_row(value)
        self._sync_destinations()
        self.save_profile(silent=True)

    def select_destination_row(self, entry, variable):
        path = filedialog.askdirectory(title="백업 대상 경로 선택")
        if path:
            variable.set(path)
            entry.focus_set()

    def remove_destination_row(self, frame):
        self.destination_rows = [
            row for row in self.destination_rows
            if row["frame"] is not frame
        ]
        frame.destroy()
        self._renumber_destination_rows()
        self._sync_destinations()
        self.save_profile(silent=True)

    def clear_destinations(self):
        for row in self.destination_rows:
            row["frame"].destroy()
        self.destination_rows.clear()
        self.destinations.clear()
        self._refresh_metrics()
        self.save_profile(silent=True)

    def _load_destination_rows(self, destinations):
        self.clear_destinations()
        for path in destinations:
            if path:
                self._build_destination_row(path)
        self._sync_destinations()

    def save_profile(self, silent=False):
        self._sync_destinations()
        profile = {
            "version": 2,
            "source": self.source_var.get().strip(),
            "name": self.name_var.get().strip(),
            "destinations": self.destinations,
            "incremental": self.incremental_var.get(),
            "backup_mode": self.backup_mode_var.get(),
            "hardlink": self.hardlink_var.get(),
            "app_mode": self.app_mode_var.get(),
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
        self.backup_mode_var.set(profile.get("backup_mode", "일반 백업"))
        self.app_mode_var.set(profile.get("app_mode", self.backup_mode_var.get()))

        self._refresh_backup_mode_segment()
        self._refresh_header()
        self.hardlink_var.set(profile.get("hardlink", True))
        self.parallel_var.set(int(profile.get("parallel", 3)))
        self.keep_var.set(int(profile.get("keep", 10)))
        self.exclude_var.set(profile.get("exclude", ""))

        self._load_destination_rows(profile.get("destinations", []))

    def write_log(self, message):
        def update():
            self.log.configure(state="normal")
            self.log.insert("end", message + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, update)

    def _format_elapsed(self, seconds):
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _start_operation_timer(self):
        self._stop_operation_timer()
        self.operation_started_at = time.monotonic()
        self.last_elapsed_seconds = 0
        self.elapsed_var.set("경과 00:00:00")
        self.eta_var.set("예상 계산 중...")
        self.elapsed_job = self.root.after(2000, self._update_elapsed)

    def _update_elapsed(self):
        if self.operation_started_at is None:
            self.elapsed_job = None
            return

        self.last_elapsed_seconds = time.monotonic() - self.operation_started_at
        self.elapsed_var.set(
            f"경과 {self._format_elapsed(self.last_elapsed_seconds)}"
        )

        remaining = max(0, self.progress_total - self.progress_value)
        if (
            self.progress_value > 0
            and self.progress_total > self.progress_value
            and self.last_elapsed_seconds >= 2
        ):
            rate = self.progress_value / self.last_elapsed_seconds
            if rate > 0:
                eta_seconds = remaining / rate
                self.eta_var.set(
                    f"예상 {self._format_elapsed(eta_seconds)}"
                )
            else:
                self.eta_var.set("예상 계산 중...")
        elif self.progress_total > 0 and self.progress_value >= self.progress_total:
            self.eta_var.set("예상 00:00:00")
        else:
            self.eta_var.set("예상 계산 중...")

        self.elapsed_job = self.root.after(2000, self._update_elapsed)

    def _stop_operation_timer(self):
        if self.elapsed_job is not None:
            try:
                self.root.after_cancel(self.elapsed_job)
            except tk.TclError:
                pass
            self.elapsed_job = None

        if self.operation_started_at is not None:
            self.last_elapsed_seconds = time.monotonic() - self.operation_started_at
            self.elapsed_var.set(
                f"소요 {self._format_elapsed(self.last_elapsed_seconds)}"
            )
            self.eta_var.set("예상 --:--:--")
            self.operation_started_at = None

    def _set_operation(self, status):
        def update():
            self.status_var.set(status)
            self._refresh_header()
        self.root.after(0, update)

    def _timeline_palette(self):
        precision = self.backup_mode_var.get() == "정밀 검사 백업"
        active = self.colors["danger"] if precision else self.colors["primary"]
        active_dark = self.colors["danger_dark"] if precision else self.colors["primary_dark"]
        return {
            "active": active,
            "active_dark": active_dark,
            "success": self.colors["success"],
            "success_soft": "#D1FAE5",
            "pending": "#CBD5E1",
            "pending_text": "#94A3B8",
            "error": self.colors["danger"],
            "error_soft": "#FEE2E2",
            "surface": self.colors["surface"],
            "text": self.colors["text"],
            "muted": self.colors["muted"],
        }

    def _draw_timeline_icon(self, canvas, cx, cy, kind, color):
        # Simple vector icons: no emoji/font dependency.
        if kind == "scan":
            canvas.create_oval(
                cx - 9, cy - 10, cx + 5, cy + 4,
                outline=color, width=2
            )
            canvas.create_line(
                cx + 3, cy + 2, cx + 11, cy + 10,
                fill=color, width=2
            )
            canvas.create_line(
                cx - 10, cy + 11, cx - 2, cy + 3,
                fill=color, width=2
            )
        elif kind == "files":
            canvas.create_rectangle(
                cx - 9, cy - 10, cx + 5, cy + 9,
                outline=color, width=2
            )
            canvas.create_rectangle(
                cx - 4, cy - 6, cx + 10, cy + 13,
                outline=color, width=2
            )
        elif kind == "copy":
            canvas.create_rectangle(
                cx - 10, cy - 8, cx + 3, cy + 10,
                outline=color, width=2
            )
            canvas.create_rectangle(
                cx - 3, cy - 11, cx + 10, cy + 7,
                outline=color, width=2
            )
        elif kind == "check":
            canvas.create_oval(
                cx - 10, cy - 10, cx + 10, cy + 10,
                outline=color, width=2
            )
            canvas.create_line(
                cx - 6, cy, cx - 1, cy + 5,
                fill=color, width=2
            )
            canvas.create_line(
                cx - 1, cy + 5, cx + 7, cy - 5,
                fill=color, width=2
            )
        elif kind == "zip":
            canvas.create_rectangle(
                cx - 9, cy - 11, cx + 9, cy + 11,
                outline=color, width=2
            )
            canvas.create_line(
                cx - 3, cy - 7, cx - 3, cy + 7,
                fill=color, width=2
            )
            canvas.create_line(
                cx + 1, cy - 7, cx + 1, cy + 7,
                fill=color, width=2
            )
        elif kind == "shield":
            canvas.create_polygon(
                cx, cy - 11,
                cx + 9, cy - 6,
                cx + 7, cy + 6,
                cx, cy + 11,
                cx - 7, cy + 6,
                cx - 9, cy - 6,
                fill="", outline=color, width=2
            )
            canvas.create_line(
                cx - 5, cy, cx - 1, cy + 4,
                fill=color, width=2
            )
            canvas.create_line(
                cx - 1, cy + 4, cx + 6, cy - 4,
                fill=color, width=2
            )
        elif kind == "flag":
            canvas.create_line(
                cx - 7, cy - 10, cx - 7, cy + 11,
                fill=color, width=2
            )
            canvas.create_polygon(
                cx - 6, cy - 9,
                cx + 8, cy - 5,
                cx - 6, cy + 1,
                fill=color, outline=color
            )

    def _draw_timeline(self, _event=None):
        if not hasattr(self, "timeline_canvas"):
            return

        canvas = self.timeline_canvas
        canvas.delete("all")
        width = max(700, canvas.winfo_width())
        height = canvas.winfo_height()
        palette = self._timeline_palette()

        left = 50
        right = width - 50
        y = 36
        label_y = 78
        state_y = 103
        count = len(self.timeline_steps)
        gap = (right - left) / max(1, count - 1)

        # Connector line first.
        for i in range(count - 1):
            x1 = left + gap * i
            x2 = left + gap * (i + 1)
            segment_color = palette["pending"]

            if self.timeline_success:
                segment_color = palette["success"]
            elif self.timeline_error and i >= max(0, self.timeline_current - 1):
                segment_color = palette["error"]
            elif self.timeline_current >= 0:
                if i < self.timeline_current:
                    segment_color = palette["success"]
                elif i == self.timeline_current:
                    segment_color = palette["active"]

            canvas.create_line(
                x1 + 23, y, x2 - 23, y,
                fill=segment_color,
                width=3,
                capstyle="round",
            )

        for index, (label, icon_kind) in enumerate(self.timeline_steps):
            x = left + gap * index
            state = "pending"
            if self.timeline_success:
                state = "success"
            elif self.timeline_error and index == self.timeline_current:
                state = "error"
            elif self.timeline_current >= 0:
                if index < self.timeline_current:
                    state = "done"
                elif index == self.timeline_current:
                    state = "current"

            if state == "success":
                fill = palette["success"]
                outline = palette["success"]
                icon_color = "#FFFFFF"
                state_text = "완료"
                text_color = palette["success"]
            elif state == "done":
                fill = palette["success_soft"]
                outline = palette["success"]
                icon_color = palette["success"]
                state_text = "완료"
                text_color = palette["muted"]
            elif state == "current":
                fill = palette["active"]
                outline = palette["active"]
                icon_color = "#FFFFFF"
                state_text = "진행 중..."
                text_color = palette["active"]
            elif state == "error":
                fill = palette["error"]
                outline = palette["error"]
                icon_color = "#FFFFFF"
                state_text = "오류 발생"
                text_color = palette["error"]
            else:
                fill = "#F8FAFC"
                outline = palette["pending"]
                icon_color = palette["pending_text"]
                state_text = "대기 중"
                text_color = palette["pending_text"]

            # Soft halo for active/final state.
            if state in ("current", "success"):
                halo = palette["success_soft"] if state == "success" else "#EEF2FF"
                canvas.create_oval(
                    x - 29, y - 29, x + 29, y + 29,
                    fill=halo,
                    outline="",
                )

            canvas.create_oval(
                x - 22, y - 22, x + 22, y + 22,
                fill=fill,
                outline=outline,
                width=2,
            )
            self._draw_timeline_icon(canvas, x, y, icon_kind, icon_color)

            canvas.create_text(
                x, label_y,
                text=f"{index + 1}. {label}",
                fill=palette["text"] if state not in ("pending",) else palette["muted"],
                font=(self.font_family, 9, "bold"),
            )
            canvas.create_text(
                x, state_y,
                text=state_text,
                fill=text_color,
                font=(self.font_family, 8, "bold"),
            )

    def _set_timeline_stage(self, stage_index, error=False, success=False):
        self.timeline_current = max(-1, min(len(self.timeline_steps) - 1, stage_index))
        self.timeline_error = error
        self.timeline_success = success

        def draw():
            self._draw_timeline()

        self.root.after(0, draw)

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
                self._refresh_header()
        self.root.after(0, update)

    def advance_progress(self):
        with self.progress_lock:
            self.progress_value += 1
            value = self.progress_value
        self.set_progress(value=value)

    def cancel_backup(self):
        if self.running:
            self.cancel_event.set()
            self.status_var.set("정지 요청...")
            self._set_timeline_stage(self.timeline_current, error=True)
            self._refresh_header()
            self.write_log("[CANCEL] 취소 요청됨")

    def validate(self):
        self._sync_destinations()
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
        deep_scan = self.backup_mode_var.get() == "정밀 검사 백업"
        hardlink = self.hardlink_var.get()

        self.running = True
        self.cancel_event.clear()
        self.backup_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress_value = 0
        self.progress_total = 1
        self.progress.configure(value=0, maximum=1)
        self.status_var.set(
            "정밀 원본 분석 중..." if deep_scan else "원본 빠른 분석 중..."
        )
        self._refresh_header()
        self._set_timeline_stage(0)
        self._start_operation_timer()

        self.write_log(f"[START] {source}")
        self.write_log(f"[TARGETS] {len(destinations)}개 | workers={parallel}")
        self.write_log(f"[NAME] {base_name}")
        self.write_log(f"[KEEP] {keep}")
        self.write_log(
            f"[OPTIONS] mode={self.backup_mode_var.get()} "
            f"incremental={incremental} "
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
                deep_scan,
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
        deep_scan: bool,
        hardlink: bool,
    ):
        try:
            source_data = build_source_manifest(
                source,
                deep_scan=deep_scan,
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
                (
                    file_count
                    + file_count * (2 if deep_scan else 1)
                    + 1
                )
                * len(destinations),
            )
            self.set_progress(
                value=0,
                total=expected_ops,
                status=f"파일 목록 생성 완료 · {file_count:,} 파일",
            )
            self._set_timeline_stage(1)

            workers = min(parallel, len(destinations))
            results = []

            self._set_operation(
                f"백업 복사 중 · {len(destinations)}개 대상 병렬 처리"
            )
            self._set_timeline_stage(2)

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
                        deep_scan,
                        hardlink,
                        exclude_patterns,
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
                elapsed_text = self._format_elapsed(self.last_elapsed_seconds)
                self._stop_operation_timer()

                if failed == 0:
                    self._set_timeline_stage(6, success=True)
                    self.status_var.set(f"완료 · {success}/{len(results)}개 대상")
                    self._refresh_header()
                    messagebox.showinfo(
                        "백업 완료",
                        f"{success}개 경로 백업 완료\n소요 시간: {elapsed_text}",
                    )
                else:
                    self._set_timeline_stage(self.timeline_current, error=True)
                    self.status_var.set(
                        f"완료 · {success} 성공 / {failed} 실패"
                    )
                    self._refresh_header()
                    messagebox.showwarning(
                        "백업 결과",
                        f"성공: {success}\n실패: {failed}\n소요 시간: {elapsed_text}\n로그를 확인하세요.",
                    )

            self.root.after(0, finish)

        except Exception as exc:
            self.write_log(f"[FATAL] {exc}")

            def finish_error():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                elapsed_text = self._format_elapsed(self.last_elapsed_seconds)
                self._stop_operation_timer()
                self._set_timeline_stage(self.timeline_current, error=True)
                self.status_var.set(f"실패 · {elapsed_text}")
                self._refresh_header()
                messagebox.showerror("백업 실패", f"{exc}\n\n소요 시간: {elapsed_text}")

            self.root.after(0, finish_error)

    def backup_one_destination(
        self,
        source: Path,
        requested_name: str,
        destination: Path,
        source_data: dict,
        keep: int,
        incremental: bool,
        deep_scan: bool,
        hardlink: bool,
        exclude_patterns,
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

            if incremental:
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
                    and old_info.get("size") == info["size"]
                    and old_info.get("mtime_ns") == info["mtime_ns"]
                    and old_info.get("ctime_ns") == info["ctime_ns"]
                    and (
                        not deep_scan
                        or old_info.get("sha256") == info["sha256"]
                    )
                )
                if can_reuse:
                    reusable_bytes += info["size"]
                else:
                    required_bytes += info["size"]

            ensure_free_space(destination, required_bytes)

            self._set_operation(
                f"백업 복사 중 · {destination.name or destination}"
            )
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
                    incremental
                    and hardlink
                    and old_info
                    and old_file
                    and old_file.is_file()
                    and old_info.get("size") == info["size"]
                    and old_info.get("mtime_ns") == info["mtime_ns"]
                    and old_info.get("ctime_ns") == info["ctime_ns"]
                    and (
                        not deep_scan
                        or old_info.get("sha256") == info["sha256"]
                    )
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
                "verification": "sha256" if deep_scan else "fast",
                "backup_name": backup_name,
                "exclude_patterns": exclude_patterns,
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

            self._set_operation(
                f"{'정밀 무결성 검사' if deep_scan else '빠른 무결성 검사'} · {destination.name or destination}"
            )
            self._set_timeline_stage(3)
            self.write_log(
                f"[VERIFY] {destination} | "
                f"{'SHA-256' if deep_scan else '빠른 검사'}"
            )
            if deep_scan:
                verify_snapshot_sha256(
                    partial_target,
                    manifest,
                    self.advance_progress,
                    self.cancel_event,
                )
            else:
                verify_snapshot_fast(
                    source,
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

            archive_target = destination / f"{backup_name}.zip"
            partial_archive = destination / f".parallel-backup.partial-{uuid.uuid4().hex}.zip"

            self._set_operation(
                f"ZIP 압축 중 · {destination.name or destination}"
            )
            self._set_timeline_stage(4)
            self.write_log(f"[ZIP] 생성 시작: {archive_target.name}")
            create_zip_archive(
                final_target,
                partial_archive,
                self.advance_progress,
                self.cancel_event,
            )

            self._set_operation(
                f"ZIP 무결성 검사 중 · {destination.name or destination}"
            )
            self._set_timeline_stage(5)
            self.write_log(f"[ZIP VERIFY] {archive_target.name}")
            verify_zip_archive(
                partial_archive,
                manifest,
                self.advance_progress,
                self.cancel_event,
                deep_scan,
            )

            if self.cancel_event.is_set():
                raise RuntimeError("ZIP 생성이 취소되었습니다.")

            os.replace(partial_archive, archive_target)

            snapshots = list_verified_snapshots(
                destination,
                f"{requested_name}_",
                source,
            )
            for old_snapshot, _ in snapshots[keep:]:
                try:
                    safe_remove_snapshot(old_snapshot)
                    old_archive = old_snapshot.with_name(f"{old_snapshot.name}.zip")
                    if old_archive.exists():
                        old_archive.unlink()
                    self.write_log(
                        f"[RETENTION] 삭제: {old_snapshot.name} + {old_archive.name}"
                    )
                except OSError as exc:
                    self.write_log(
                        f"[RETENTION FAIL] {old_snapshot.name} -> {exc}"
                    )

            self.write_log(
                f"[ZIP OK] {archive_target.name}"
            )
            self.write_log(
                f"[OK] {destination} -> {backup_name} "
                f"(copied={copied:,}, reused={reused:,})"
            )
            return {
                "ok": True,
                "destination": str(destination),
                "snapshot": str(final_target),
                "archive": str(archive_target),
            }

        except Exception as exc:
            if partial_target.exists():
                shutil.rmtree(partial_target, ignore_errors=True)
            for partial_archive in destination.glob(".parallel-backup.partial-*.zip"):
                try:
                    partial_archive.unlink()
                except OSError:
                    pass
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
        self._set_timeline_stage(2)

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

            verify_snapshot_sha256(
                target,
                manifest,
                self.advance_progress,
                self.cancel_event,
            )

            def finish():
                self.running = False
                self.backup_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                self._set_timeline_stage(6, success=True)
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
                self._set_timeline_stage(self.timeline_current, error=True)
                self.status_var.set("복구 실패")
                messagebox.showerror("복구 실패", str(exc))

            self.root.after(0, finish_error)


if __name__ == "__main__":
    root = tk.Tk()
    ParallelBackupApp(root)
    root.mainloop()
