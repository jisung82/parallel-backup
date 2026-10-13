"""Quick storage audit; never deletes backup files.

Usage: py -3 storage_report.py [--deep]
Reads saved destination paths and checks backup ZIPs, partials and legacy TEMP.
"""
import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path


def readable(size):
    return f"{size / (1024**3):.2f} GiB"


def directory_bytes(root):
    total = 0
    for folder, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(folder) / name).stat().st_size
            except OSError:
                pass
    return total


def inspect_path(directory, deep, only_owned=False):
    if not directory.exists():
        print(f"  (not found) {directory}")
        return
    usage = shutil.disk_usage(directory)
    print(f"  path={directory}, volume_free={readable(usage.free)}")
    count = 0
    total = 0
    try:
        children = list(directory.iterdir())
    except OSError as error:
        print(f"  cannot list path: {error}")
        return
    for item in children:
        if not (
            (not only_owned and item.suffix.lower() == ".zip")
            or item.name.startswith((
                ".parallel-backup-build-", ".parallel-backup.partial-",
                "parallel-backup-stage-", "parallel-backup-master-",
                "parallel-backup-update-", "update-",
            ))
        ):
            continue
        try:
            size = directory_bytes(item) if (deep and item.is_dir()) else (
                item.stat().st_size if item.is_file() else 0
            )
            count += 1
            total += size
            detail = readable(size) if item.is_file() or deep else "size not measured (use --deep)"
            print(f"  {detail}  {item}")
        except OSError as error:
            print(f"  unavailable {item}: {error}")
    print(f"  items={count}, measured_size={readable(total)}")


def main():
    parser = argparse.ArgumentParser(description="Parallel Backup storage inspector; read-only")
    parser.add_argument("--deep", action="store_true", help="Measure sizes of legacy staging folders recursively (slow)")
    args = parser.parse_args()

    local_base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    profile = local_base / "ParallelBackup" / "profile.json"
    try:
        settings = json.loads(profile.read_text(encoding="utf-8"))
        destinations = settings.get("destinations", [])
    except (OSError, ValueError):
        destinations = []
    print("Parallel Backup storage report (read-only; does not delete anything)")
    print("Full ZIP snapshots consume space in EACH destination; incremental mode rebuilds full ZIPs.")
    for path in destinations:
        print("[DESTINATION]")
        inspect_path(Path(path).expanduser(), args.deep)

    print("[LEGACY TEMP]")
    inspect_path(Path(tempfile.gettempdir()), args.deep, only_owned=True)

    logs = local_base / "ParallelBackup" / "logs"
    if logs.is_dir():
        total = sum(p.stat().st_size for p in logs.glob("*.log") if p.is_file())
        print(f"[DIAGNOSTICS] path={logs} total={readable(total)}")
    print("Report only. No files were removed.")


if __name__ == "__main__":
    main()
