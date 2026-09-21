import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Parallel Backup"
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"


class ParallelBackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("760x560")
        self.root.minsize(680, 500)

        self.source_var = tk.StringVar()
        self.name_var = tk.StringVar(value="backup")
        self.status_var = tk.StringVar(value="대기 중")
        self.destinations = []
        self.running = False

        self.build_ui()

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="병렬 백업", font=("", 18, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="하나의 원본 폴더를 여러 경로에 동시에 백업합니다."
        ).pack(anchor="w", pady=(2, 14))

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
        ttk.Label(name_box, text="결과 예: uni_mcp_20260921_193000").pack(
            anchor="w", padx=8, pady=(0, 8)
        )

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
        ttk.Label(action, textvariable=self.status_var).pack(side="right")

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", pady=6)

        log_box = ttk.LabelFrame(outer, text="로그")
        log_box.pack(fill="both", expand=True, pady=5)
        self.log = tk.Text(log_box, height=8, state="disabled", wrap="word")
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

    def write_log(self, message):
        def update():
            self.log.configure(state="normal")
            self.log.insert("end", message + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, update)

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

        self.running = True
        self.backup_button.configure(state="disabled")
        self.progress.start(10)
        self.status_var.set("백업 중...")
        self.write_log(f"[START] {source}")
        self.write_log(f"[BACKUP] {backup_name}")
        self.write_log(f"[TARGETS] {len(destinations)}개")

        threading.Thread(
            target=self.run_backup,
            args=(source, destinations, backup_name),
            daemon=True,
        ).start()

    def run_backup(self, source, destinations, backup_name):
        results = []

        def copy_one(destination):
            target = destination / backup_name
            self.write_log(f"[BEGIN] {destination}")
            try:
                if target.exists():
                    raise FileExistsError(f"이미 존재함: {target}")
                shutil.copytree(source, target)
                self.write_log(f"[OK] {target}")
                return True, str(target), None
            except Exception as exc:
                self.write_log(f"[FAIL] {destination} -> {exc}")
                return False, str(target), str(exc)

        with ThreadPoolExecutor(max_workers=len(destinations)) as executor:
            futures = [executor.submit(copy_one, item) for item in destinations]
            for future in as_completed(futures):
                results.append(future.result())

        success = sum(1 for ok, _, _ in results if ok)
        failed = len(results) - success

        def finish():
            self.running = False
            self.backup_button.configure(state="normal")
            self.progress.stop()
            self.status_var.set(f"완료: {success} 성공 / {failed} 실패")

            if failed == 0:
                messagebox.showinfo(
                    "백업 완료",
                    f"{success}개 경로에 백업했습니다.\n\n{backup_name}"
                )
            else:
                messagebox.showwarning(
                    "백업 결과",
                    f"성공: {success}\n실패: {failed}\n\n로그를 확인하세요."
                )

        self.root.after(0, finish)


if __name__ == "__main__":
    root = tk.Tk()
    ParallelBackupApp(root)
    root.mainloop()
