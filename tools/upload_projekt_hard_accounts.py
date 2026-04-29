r"""Upload Projekt Hard account JSON files to the local PrivateFish backend.

Run from the repository root:
    python tools/upload_projekt_hard_accounts.py
"""

from __future__ import annotations

import json
import os
import platform
import sys
import urllib.error
import urllib.request
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from projekt_hard_install import find_projekt_hard_install


DEFAULT_API_URL = "http://127.0.0.1:3000/upload-accounts"
DEFAULT_UPLOAD_TOKEN = "privatefish-local-upload-token"


def _machine_id() -> str:
    name = platform.node().strip() or "unknown-pc"
    return f"{name}-{os.getlogin()}"


def _load_account_file(path: Path) -> dict:
    raw = path.read_bytes()
    try:
        return {
            "encoding": "json",
            "content": json.loads(raw.decode("utf-8")),
        }
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
        }


def main() -> int:
    api_url = os.environ.get("PRIVATEFISH_API_URL", DEFAULT_API_URL)
    upload_token = os.environ.get("PRIVATEFISH_UPLOAD_TOKEN", DEFAULT_UPLOAD_TOKEN)
    install = find_projekt_hard_install()

    if not install:
        print("Projekt Hard not found")
        return 1

    if not install.has_accounts:
        print(f"No account JSON files found in {install.accounts_path}")
        return 1

    accounts = []
    for account_file in install.account_files:
        stat = account_file.stat()
        accounts.append(
            {
                "fileName": account_file.name,
                "data": _load_account_file(account_file),
                "fileSize": stat.st_size,
                "fileMtime": account_file.stat().st_mtime_ns // 1_000_000,
                "source": str(account_file),
            }
        )

    payload = json.dumps({"machineId": _machine_id(), "accounts": accounts}).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {upload_token}",
            "Content-Type": "application/json",
            "User-Agent": "PrivateFish-account-uploader",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"Upload failed: HTTP {exc.code} {exc.read().decode('utf-8', errors='replace')}")
        return 1
    except urllib.error.URLError as exc:
        print(f"Upload failed: {exc}")
        return 1

    print(f"Uploaded {result.get('savedCount', 0)} account file(s) from {install.accounts_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
