# E2 — Temporary File Sharing cleanup lifecycle

E2 completes the temporary-transfer lifecycle from E1 without changing its public access contract.

- A lifespan worker runs a cleanup pass at startup and then every `HOME_FILE_SHARE_CLEANUP_SECONDS` (default: 3600).
- Expired transfers use `expires_at <= now`: public access remains blocked immediately, independent of worker timing.
- Cleanup removes the validated transfer directory first, then deletes transfer/file metadata and commits. A disk failure is logged and leaves metadata intact for retry on the next pass.
- Cleanup is idempotent: missing files/directories are safe; repeated passes and concurrent owner deletion do not require a separate queue.
- Orphan cleanup considers only old numeric direct children of `HOME_FILE_SHARE_DIR` with no DB transfer. `HOME_FILE_SHARE_ORPHAN_GRACE_HOURS` (default: 24) protects fresh transactions and partial `.tmp` uploads.
- Every transfer path is constructed from the integer DB id beneath the resolved share root. Symlinks are never followed during cleanup; a root-level orphan symlink is unlinked only.
- Uploads stream to private `.tmp` files, enforce actual bytes while writing, and atomically rename on success.
- Optional safety caps: `HOME_FILE_SHARE_MAX_STORAGE_MB` and `HOME_FILE_SHARE_MAX_USER_STORAGE_MB` (`0` means unlimited). `HOME_FILE_SHARE_MIN_FREE_MB` reserves free disk space (default: 512 MB).
- The Files page shows personal storage usage, active/expired counts and configured limits. Owners can remove only their expired transfers, extend from the current time, and rotate the public token. Rotation invalidates the old URL.

Manual recovery: `python -m app.scripts.cleanup_shared_files` uses the same cleanup service as the worker.

No schema migration is required beyond E1 tables. Temporary transfers remain excluded from user JSON export.

Known limitations: no nested folders, password protection, public upload, resumable transfer, antivirus, download analytics, or distributed object storage.
