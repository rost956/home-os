# Checkpoint E1 — Temporary File Sharing foundation

## Domain model

`TemporaryFileTransfer` is the owner-scoped logical container. It stores a title, optional description, high-entropy public token, UTC-naive `expires_at`, and audit timestamps. `TemporarySharedFile` stores each file's display filename, server-generated storage key, byte size, untrusted browser content-type metadata and creation timestamp. Files are not stored in SQLite.

## Storage and deletion

Storage lives at `HOME_FILE_SHARE_DIR`, which is required to be inside `DATA_DIR` and defaults to `DATA_DIR/shared_files` (`/app/data/shared_files` in Docker). Each transfer owns one directory named by its database ID; each physical filename is a random 32-character hex storage key. Original filenames are display/download metadata only and are sanitized before storage.

Uploads are size-validated before writing, then copied in 1 MiB chunks. Metadata is flushed only after the files have been written; failed writes remove created files. Manual single-file deletion tolerates an already-missing physical file. Manual transfer deletion removes its transfer directory and database rows; it immediately invalidates the public token.

## Share and expiry contract

Tokens use `secrets.token_urlsafe(32)` and have a unique database constraint. `GET /share/{token}` and its download route are anonymous read-only capability routes. A token can access only files belonging to its transfer. Public content never includes account identity, internal IDs or storage paths.

`expires_at` is a UTC-naive persistent timestamp, consistent with the application's SQLite convention. At `now >= expires_at`, public listing and downloads return an expired response immediately. Owners can still see and manually delete an expired transfer. E2 will physically clean expired transfer directories.

Public share pages use `Cache-Control: no-store` and `X-Robots-Tag: noindex, nofollow`. Downloads always use attachment disposition, `application/octet-stream`, `no-store`, and `X-Content-Type-Options: nosniff`.

## Owner UI

The Service navigation now contains “Файлы”. Owners can create a transfer with 1 hour, 24 hour, 3 day or 7 day TTL, upload multiple files initially or later, copy the public link with clipboard fallback, download files, delete one file, and delete the whole transfer. Long filenames and controls collapse without horizontal overflow on small screens.

## Configuration and deployment

- `HOME_FILE_SHARE_DIR=/app/data/shared_files`
- `HOME_FILE_SHARE_MAX_FILE_MB=100`
- `HOME_FILE_SHARE_MAX_TRANSFER_MB=500`

Docker already mounts `./data:/app/data`; no additional volume is required. The service rejects a configured sharing directory outside `DATA_DIR`. Request size protection is sized to permit the configured transfer limit plus multipart overhead.

## Backup and migration

Ordinary user JSON export intentionally excludes temporary transfer metadata and physical files. Administrative full database/data backups retain their existing operational semantics.

The runtime SQLite schema creates `temporary_file_transfers` and `temporary_shared_files` non-destructively. Indexes cover owner, token, transfer, and expiry lookup, preparing E2 cleanup queries such as `expires_at <= now`.

## Tests

Coverage includes initial and multiple upload, Unicode and duplicate names, storage persistence, TTL calculations and expiry boundaries, anonymous public download, attachment headers, noindex/no-store, token/file mixing, owner isolation, missing disk files, size limits, path traversal, single-file deletion, transfer deletion, and non-destructive migration.

## Known limitations

- Expired physical files are removed only in E2.
- No nested folders, password protection, public upload, resumable/chunked transfer protocol, download analytics, quotas beyond per-transfer limits, or antivirus scanning.
