# Parallel Backup — Storage and Reliability Audit
**Date:** 2026-10-13  
**Scope:** Python backup engine, GUI orchestration, ZIP manifest, CRC/SHA verification, retention, stop/failure, restore, updater, diagnostic logs, Windows CI.

## Critical findings and changes

| Severity | Finding | Evidence / Fix |
|---|---|---|
| P0 | Retention prefix included each run's timestamp; older ZIPs never matched | Use stable family prefix `base_name.rsplit("_", 2)[0] + "_"`; functional test creates two dated ZIPs with keep=1 and checks older removed |
| P0 | Original parallel code renamed master while other workers copied it | Remote target workers finish BEFORE local master atomic rename; no hardlink required, no additional large local copy |
| P0 | Remote ZIP was published before CRC/SHA validation | Validate `.parallel-backup.partial-*.zip` first, `os.replace` only after pass; inject verification corruption and confirm final file not created |
| P1 | Cancellation during a large CRC scan could remain unresponsive | Read archive members in bounded chunks, check cancellation every chunk |
| P1 | Restore could truncate/overwrite existing files before cancellation | Restore each file to same-folder `.parallel-backup-restore-*.partial`, check size/hash/cancel and atomically replace only when valid |
| P1 | Very conservative ZIP preflight used uncompressed input size and rejected compressible projects even with enough space | Warn when worst-case estimate exceeds free space; enforce safety reserve and dynamically monitor space; remove partial on write failure |
| P1 | Scattered/minimal logs made failures hard to diagnose | Persistent capped per-run logs, traceback, target/partial/free/bytes, progress, cleanup, retention, job version |
| P2 | ZIP verification unnecessarily decompressed source archive multiple times | Verify manifest/files/CRC/optional SHA in a single archive-member read pass, avoid redundant master scan during local finalize |
| P2 | Storage allocation poorly explained | Documentation + read-only `storage_report.py` tool to inspect saved destinations and legacy TEMP residues |

## Where data lives

**Normal backup**
- First destination: `<folder>/.parallel-backup-build-<uuid>.zip.partial`, streamed directly from source or previous ZIP. **No large system TEMP staging.**
- Remaining destinations: `<folder>/.parallel-backup.partial-<uuid>.zip`, copied concurrently and validated before publish.
- Completion: `<folder>/<backup_name>_<YYYYMMDD>_<HHMMSS>.zip` at each successful destination. The local master is atomically renamed LAST; no extra local full-copy or persistent hardlink.
- Persistent local logs: `%LOCALAPPDATA%\\ParallelBackup\\logs\\` (per run ~8MiB cap, ~20 logs / 32MiB cap on next-session pruning).
- Settings: `%LOCALAPPDATA%\\ParallelBackup\\profile.json`.
- Auto-update temporary downloads: `%LOCALAPPDATA%\\ParallelBackup\\update-<uuid>\\` while updating.

**Normal stop or recoverable failure**
- Cancel event stops streaming writes and validation. `.partial` files are deleted in `finally` or exception handling.
- A fully validated ZIP previously published in one destination is **not automatically deleted** if another target fails; results identify partial success explicitly.
- If the process is killed or the PC loses power, Python cleanup cannot run. Old working ZIPs are recognized/cleaned after 24h when that destination is used again. Use the read-only inspector before manual cleanup.
- Restore uses **one file at a time** as a local partial, so temporary restore overhead is limited to **the current file**, not the whole tree. Cancelling preserves the existing target file.

**Legacy versions**
- `%TEMP%\\parallel-backup-stage-*` and `%TEMP%\\parallel-backup-master-*.zip` may persist from OLD versions. The new engine does not create them. Inspect before deletion; do not blindly erase all TEMP data.

## Storage-size and performance model

**This is a FULL ZIP SNAPSHOT backup**, not a deduplicating repository or delta archive.

```
Approximate retained bytes = Σ (compressed ZIP size on each destination × retained versions)
```

Example: at an observed ZIP size of 4.34 GiB, three targets and nine retained full archives may use roughly **117.18 GiB**, distributed among the destination volumes. These figures are illustrative only.

Incremental mode re-reads unchanged bytes from an earlier verified ZIP rather than source, which can reduce original-drive access; it still builds and writes a COMPLETE new compressed ZIP, so incremental mode does **not** remove cross-snapshot storage duplication.

The source tree is never copied wholesale into system TEMP. Peak additional usage per volume depends on how many configured destination folders share that volume: one new ZIP for each destination on that volume, plus at most one in-progress per-file restore on the restore volume. Redundant backups on the same physical drive do not provide independent protection against drive failure.

Other performance improvements: one-pass verification, chunk-cancellable CRC, local atomic rename rather than copying or hardlinking, and file-count-bounded logs. Actual throughput still varies by disk type, CPU, compression ratio and antivirus.

## Verification

Windows GitHub Actions runs launcher, py_compile (all modules), pyflakes (all modules), and pytest. Functional test scenarios include:
- direct-to-destination ZIP, no TEMP staging;
- three-target copies via ThreadPoolExecutor, identical ZIP content in normal and deep mode;
- master survives through remote copies and is renamed last;
- injected disk-full, cancelled copy and verification failure leave no published bad ZIP;
- cancellation during ZIP creation removes master;
- simulated crash residue older than 24h is identified and reclaimed;
- per-file restore cancellation/hash error preserves a pre-existing file;
- retention removes earlier ZIPs across different timestamps with keep=1;
- stored diagnostic logs and traceback/source-lifecycle guard checks.

**Scope limitation:** GitHub CI uses temporary small files; it is not evidence of a successful end-to-end run on the user's real 32+ GiB / 200k-file project across C/E/F volumes, nor a physical kill/power-loss test.

## How to inspect disk usage without deleting data

From the project folder, execute:

```powershell
py -3 storage_report.py
py -3 storage_report.py --deep
```

The first command enumerates ZIP/partial candidates and volume free space quickly. The second additionally calculates sizes of legacy temporary directories and can take longer. Neither deletes files.

## Remaining limits / recommended future work

- True block-level/delta deduplication with indexed chunk storage can radically reduce version history space, but requires a **new verified restore format**, migration design and extensive corruption testing. It is NOT yet implemented.
- No automatic cleanup is possible after immediate process kill or power loss; startup stale-partial detection is time-gated.
- Active source files may change during a long scan/ZIP creation; deep mode detects many changes via hashes, but application-consistent snapshots need VSS or quiescing writers.
- Restore is atomic **per file**, not transactional for the entire project tree. Full rollback on mid-restore failure is not provided.
- Native hardware tests and failure simulation remain necessary before classifying production release as fully verified.
