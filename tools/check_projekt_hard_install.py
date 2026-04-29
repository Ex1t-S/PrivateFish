r"""Print Projekt Hard install diagnostics.

Run from the repository root:
    python tools/check_projekt_hard_install.py

Optionally pass a known folder:
    python tools/check_projekt_hard_install.py "D:\games\Projekt-Hard"
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from projekt_hard_install import find_projekt_hard_install


def main() -> int:
    saved_path = sys.argv[1] if len(sys.argv) > 1 else None
    install = find_projekt_hard_install(saved_path)
    if not install:
        print("Projekt Hard: NOT FOUND")
        return 1

    print("Projekt Hard: FOUND")
    print(f"Root: {install.path}")
    print(f"Reason: {install.reason}")
    print(f"Game exe: {install.exe_path}")
    print(f"user_data: {'OK' if install.has_user_data else 'MISSING'} - {install.user_data_path}")
    print(f"accounts: {'OK' if install.has_accounts_dir else 'MISSING'} - {install.accounts_path}")

    if install.has_accounts:
        print(f"Accounts created: {len(install.account_files)}")
        for account_file in install.account_files:
            print(f"  - {account_file.name}")
    else:
        print("Accounts created: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
