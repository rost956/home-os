"""Run the same safe temporary-file cleanup used by the background worker."""
from __future__ import annotations

from app.main import run_file_share_cleanup


def main() -> None:
    summary = run_file_share_cleanup()
    print(f"removed={summary.transfers_removed} files={summary.files_removed} bytes={summary.bytes_freed} failures={summary.failures} orphans={summary.orphans_removed}")


if __name__ == "__main__":
    main()
