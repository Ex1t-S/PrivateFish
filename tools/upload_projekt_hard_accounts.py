r"""Upload Projekt Hard account JSON files to the local PrivateFish backend.

Run from the repository root:
    python tools/upload_projekt_hard_accounts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from account_backup import upload_projekt_hard_accounts


def main() -> int:
    result = upload_projekt_hard_accounts(timeout=15)
    print(f"Projekt Hard paths found: {result.installs_found}")
    for install_path in result.install_paths:
        print(f"  - {install_path}")

    if not result.ok:
        print(f"Upload failed: {result.error}")
        return 1

    print(f"Account files found: {result.accounts_found}")
    print(f"Uploaded: {result.uploaded_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
