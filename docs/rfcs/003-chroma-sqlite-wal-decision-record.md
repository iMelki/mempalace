# RFC-003 / Architecture Decision Record: SQLite WAL Mode vs. journal_mode=delete for 26.5 GB Chroma Database

**Status:** Proposed / Decided  
**Date:** 2026-09-16  
**Tracking Issues:** [memsys#680](https://github.com/iMelki/memsys/issues/680), [memsys#692](https://github.com/iMelki/memsys/issues/692)  
**Target Surface:** `chroma.sqlite3` (~26.5 GB), ChromaDB `PersistentClient` & SQLite access in `mempalace` / `memsys`  
**Authors:** Assistants Fleet / Antigravity

---

## 1. Context & Problem Statement

MemPalace's primary vector and metadata database (`chroma.sqlite3`) has grown to approximately **26.5 GB**. Under default ChromaDB `PersistentClient` configurations, SQLite operates with:
```sql
PRAGMA journal_mode = DELETE;
```

### The Failure Mode
In `DELETE` journal mode, write operations (such as document ingestion, drawer mining, embedding updates, and cohort builds) acquire an `EXCLUSIVE` database lock. While this lock is held:
- **All concurrent reader connections are completely locked out.**
- Even connections opened with `mode=ro` (read-only URI) fail immediately or hang until timing out with:
  ```text
  sqlite3.OperationalError: database is locked
  ```
- Long-running mining sessions (which can take tens of minutes to hours) render the central memory palace completely unreadable by agents, Mission Control, MemSys router, and health probes, causing false-positive fleet health alarms and degraded agent memory recall.

### The Question
Should MemPalace switch `chroma.sqlite3` to Write-Ahead Logging (`PRAGMA journal_mode = WAL;`)? What are the precise architectural trade-offs, Windows-specific failure modes, checkpoint starvation risks, and recommended operational controls?

---

## 2. Comprehensive Trade-Off Matrix

| Dimension | `journal_mode = DELETE` (Current Default) | `journal_mode = WAL` (Write-Ahead Logging) |
| :--- | :--- | :--- |
| **Reader Concurrency During Mining** | **Zero.** Writers acquire an `EXCLUSIVE` table/file lock. All concurrent readers (even `mode=ro`) are blocked and fail with `SQLITE_BUSY: database is locked`. | **High.** Readers do not block writers, and writers do not block readers. Readers read from a point-in-time snapshot without waiting for write transactions. |
| **Disk Footprint Stability** | **High.** The rollback journal only exists during active write transactions and is deleted upon commit. DB size remains stable (~26.5 GB). | **Variable / High Risk.** If readers are active or checkpointing fails, the `-wal` file grows unbounded, easily adding 10–30+ GB. |
| **I/O & Commit Latency** | High write latency (rewriting rollback journal + main DB pages + dual `fsync`). | Low write latency (append-only writes to `-wal` with single `fsync` under `synchronous=NORMAL`). Checkpoint latency can cause heavy background I/O. |
| **Windows / Antivirus Sensitivity** | Low. Only 1 primary file plus short-lived `.journal` file. | **High.** Relies on `-wal` and `-shm` (shared memory via `MapViewOfFile`). Real-time antivirus scanners (Norton, Defender) frequently lock handles causing transient `SQLITE_BUSY` or `SQLITE_IOERR_LOCK`. |
| **Cross-Boundary (WSL / Network)** | Works on DrvFS (with performance penalty). | **Completely broken over WSL `/mnt/c` (9P) or SMB shares.** Shared memory and POSIX locks fail across filesystem boundaries. |
| **Read-Only (`mode=ro`) Requirement** | Pure read-only; requires no write permissions in folder. | **Requires write permission to directory** to create/update `-shm` coordination file unless opened with `immutable=1` or `nolock=1`. |
| **Backup Complexity** | Simple file copy if no writer is active. | Requires atomic snapshot of both `chroma.sqlite3` AND `chroma.sqlite3-wal`, or forcing a `PRAGMA wal_checkpoint(TRUNCATE)` before backup. |

---

## 3. Deep Analysis of WAL Hazards on a 26.5 GB Database

### 3.1 Checkpoint Starvation & The `mxFrame` Ceiling
In SQLite's WAL implementation, readers do not block writers, but **readers block checkpoints**:
1. When a reader starts a query, it records `mxFrame` (the latest committed frame in the `-wal` file) in the shared memory header (`-shm`).
2. The checkpointer backfills committed pages from the `-wal` file back into `chroma.sqlite3`.
3. However, the checkpointer **cannot backfill past the lowest active reader's `mxFrame`**, because doing so would overwrite pages that the reader's point-in-time snapshot depends on.
4. If background evaluators, web consoles, or watchdog probes maintain open, unclosed read transactions (or if queries overlap continuously), the WAL cannot be reset.
5. In a bulk ingestion run adding 100,000 embeddings, an uncheckpointed WAL file will grow to **10–25 GB**, doubling the disk footprint to >50 GB and severely degrading read performance (as readers must linearly scan the WAL index).

### 3.2 Windows Shared Memory (`-shm`) and Antivirus Contention
On Windows, SQLite coordinates WAL readers and writers using `MapViewOfFile` on `chroma.sqlite3-shm`.
- Antivirus filter drivers (e.g. Norton, Microsoft Defender) intercept file modification events on `.sqlite3-wal` and `.sqlite3-shm`.
- If an AV scanner holds an opportunistic lock or read handle on `-shm` when SQLite attempts to grow or remap the memory section, SQLite raises `SQLITE_IOERR_LOCK` or `SQLITE_BUSY`.

### 3.3 WSL / DrvFS Boundary Incompatibility
MemSys and MemPalace components interact with WSL2 (e.g. Honcho, Hindsight).
- When a Windows NTFS file is accessed inside WSL via `/mnt/c/`, the 9P/DrvFS protocol does not implement POSIX shared memory or byte-range lock translations required by SQLite WAL mode.
- Any process attempting to open a WAL database over DrvFS encounters corruption or `SQLITE_IOERR_SHMMAP`.
- **Constraint:** All SQLite access to `chroma.sqlite3` must remain strictly Windows-host-native or channeled through a local HTTP bridge.

---

## 4. Architectural Decision: The Dual-Track Strategy

We reject both unmanaged naive WAL adoption (which risks disk exhaustion from unbounded WAL growth) and retaining exclusive `DELETE` mode during mining (which breaks read availability).

Instead, we adopt a **Dual-Track Architecture**:

### Track 1: Online Incremental Mining & Normal Operation (Managed WAL Mode)
For day-to-day incremental mining, CLI queries, and concurrent read access:
1. **Database Mode Configuration:**
   Execute on `chroma.sqlite3`:
   ```sql
   PRAGMA journal_mode = WAL;
   PRAGMA synchronous = NORMAL;
   PRAGMA busy_timeout = 30000;
   PRAGMA journal_size_limit = 67108864; -- Cap WAL re-use at 64 MB
   PRAGMA mmap_size = 268435456;         -- 256 MB memory-mapped I/O
   ```
2. **Transaction & Connection Discipline:**
   - **No long-lived read transactions:** All readers must explicitly close queries and finalize cursors immediately. Disallow open transactions across network requests or async sleeps.
   - **Busy timeout:** Set `busy_timeout = 30000` (30 seconds) on all connections to absorb transient checkpoint locks.
3. **Miner Checkpoint Cadence:**
   - The MemPalace miner must execute ingestion in chunked transactions (e.g. batches of 250–500 documents).
   - At each batch boundary, the miner explicitly invokes:
     ```sql
     PRAGMA wal_checkpoint(PASSIVE);
     ```
   - At the completion of a mining session, the miner invokes:
     ```sql
     PRAGMA wal_checkpoint(TRUNCATE);
     ```
     ensuring the `-wal` file is fully backfilled and shrunk to 0 bytes.
4. **Antivirus Directory Exclusion:**
   - Exclude `%LOCALAPPDATA%\MemPalace\` and `S:\source\CCAI\Assistants\tools\Memory\mempalace\storage\` from real-time AV file scanning to eliminate `-shm` lock contention.

---

### Track 2: Bulk Cohort Ingestion & Full Rebuilds (Shadow Staging Swap)
When performing multi-gigabyte batch mining (e.g. importing full conversation archives, reprocessing 50,000+ files):
1. **Never mine directly into the live `chroma.sqlite3`.**
2. **Shadow Staging Database:**
   - Ingest into a staging instance: `storage/staging/chroma.sqlite3`.
   - The live database continues serving read requests with zero lock contention.
3. **Completion & Verification:**
   - Run `PRAGMA wal_checkpoint(TRUNCATE);` on the staging database.
   - Run `PRAGMA integrity_check;` to guarantee 100% database health.
4. **Atomic Promotion / Swap:**
   - Set MemSys maintenance latch (`Write-MaintenanceLatch`).
   - Drain active reader connections (10-second bounded grace period).
   - Atomically swap `staging/chroma.sqlite3` to `live/chroma.sqlite3` via filesystem move / hardlink.
   - Clear maintenance latch.

---

## 5. Operational Alarms & Monitoring Requirements

1. **WAL File Size Telemetry:**
   - Incorporate `-wal` file size checking into `Test-PalaceSnapshotHealth.ps1` and `MemSys-PalaceSnapshotSpaceWatch`.
   - **Thresholds:**
     - `WARN`: `-wal` file size > **250 MB** (indicates passive checkpoint lag).
     - `ERROR`: `-wal` file size > **1,000 MB** (indicates checkpoint starvation or leaked read lock).
2. **Backup Invariant:**
   - Automated snapshot jobs (`Invoke-PalaceSnapshotTask.ps1`) must either:
     - Run `PRAGMA wal_checkpoint(TRUNCATE);` prior to backup; OR
     - Back up the atomic triad: `chroma.sqlite3`, `chroma.sqlite3-wal`, and `chroma.sqlite3-shm` together.

---

## 6. Implementation Checklist & Rollout Plan

- [x] **ADR Formulation:** Record trade-offs and decision in RFC-003.
- [ ] **PRAGMA Migration Script:** Author `Set-ChromaSqliteWalPolicy.ps1` in `mempalace/scripts` to set WAL, NORMAL synchronous, and 64 MB journal size limit.
- [ ] **Miner Checkpoint Hook:** Add `PRAGMA wal_checkpoint(PASSIVE)` at drawer batch commit points in `mempalace.mine`.
- [ ] **Space Watch Monitor:** Add `-wal` file size threshold alarm to `MemSys-PalaceSnapshotSpaceWatch`.
- [ ] **Backup Hook Verification:** Verify `Backup-Palace.ps1` checkpoints or snapshots `-wal` + `-shm`.

---

## 7. References

- SQLite Documentation: [Write-Ahead Logging (WAL)](https://sqlite.org/wal.html)
- SQLite Documentation: [PRAGMA wal_checkpoint](https://sqlite.org/pragma.html#pragma_wal_checkpoint)
- SQLite Documentation: [WAL-mode File Format & mxFrame](https://sqlite.org/walformat.html)
- ChromaDB Architecture: SQLite PersistentClient Concurrency Patterns (Issue #1084, #1492)
- MemSys Issues: [memsys#680](https://github.com/iMelki/memsys/issues/680), [memsys#692](https://github.com/iMelki/memsys/issues/692)
