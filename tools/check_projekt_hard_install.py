r"""Print Projekt Hard install diagnostics.

Run from the repository root:
    python tools/check_projekt_hard_install.py

Optionally pass a known folder:
    python tools/check_projekt_hard_install.py "D:\games\Projekt-Hard"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from projekt_hard_install import find_all_projekt_hard_installs, find_projekt_hard_install


def print_install(install, index: int | None = None) -> None:
    prefix = f"[{index}] " if index is not None else ""
    print(f"{prefix}Root: {install.path}")
    print(f"{prefix}Reason: {install.reason}")
    print(f"{prefix}Game exe: {'OK' if install.has_game_exe else 'MISSING'} - {install.exe_path or (install.path / 'Projekt-Hard.exe')}")
    print(f"{prefix}user_data: {'OK' if install.has_user_data else 'MISSING'} - {install.user_data_path}")
    print(f"{prefix}accounts: {'OK' if install.has_accounts_dir else 'MISSING'} - {install.accounts_path}")

    if install.has_accounts:
        print(f"{prefix}Accounts created: {len(install.account_files)}")
        for account_file in install.account_files:
            print(f"{prefix}  - {account_file.name}")
    else:
        print(f"{prefix}Accounts created: 0")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Projekt Hard install folders.")
    parser.add_argument("path", nargs="?", help="Optional known folder to check first")
    parser.add_argument("--all", action="store_true", help="Print every detected install")
    args = parser.parse_args()

    saved_path = args.path
    if args.all:
        installs = find_all_projekt_hard_installs(saved_path)
        print(f"Projekt Hard installs found: {len(installs)}")
        for index, install in enumerate(installs, start=1):
            print()
            print_install(install, index)
        return 0 if installs else 1

    install = find_projekt_hard_install(saved_path)
    if not install:
        print("Projekt Hard: NOT FOUND")
        return 1

    print("Projekt Hard: FOUND")
    print_install(install)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
