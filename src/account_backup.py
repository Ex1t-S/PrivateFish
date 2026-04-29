"""Upload Projekt Hard account files to the PrivateFish backend."""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import platform
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from projekt_hard_install import ProjektHardInstall, find_all_projekt_hard_installs


DEFAULT_API_URL = "http://127.0.0.1:3000/upload-accounts"
DEFAULT_UPLOAD_TOKEN = "privatefish-local-upload-token"


@dataclass(frozen=True)
class AccountBackupResult:
    installs_found: int
    accounts_found: int
    uploaded_count: int
    install_paths: tuple[str, ...]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def machine_id() -> str:
    name = platform.node().strip() or "unknown-pc"
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown-user"
    return f"{name}-{user}"


def _install_key(install: ProjektHardInstall) -> str:
    digest = hashlib.sha1(str(install.path).encode("utf-8", errors="ignore")).hexdigest()
    return digest[:10]


def _upload_file_name(install: ProjektHardInstall, account_file: Path) -> str:
    return f"{_install_key(install)}__{account_file.name}"


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


def collect_account_payloads(saved_path: str | None = None) -> tuple[list[ProjektHardInstall], list[dict]]:
    installs = find_all_projekt_hard_installs(saved_path)
    accounts: list[dict] = []
    for install in installs:
        for account_file in install.account_files:
            try:
                stat = account_file.stat()
                accounts.append(
                    {
                        "fileName": _upload_file_name(install, account_file),
                        "data": _load_account_file(account_file),
                        "fileSize": stat.st_size,
                        "fileMtime": stat.st_mtime_ns // 1_000_000,
                        "source": str(account_file),
                    }
                )
            except OSError:
                continue
    return installs, accounts


def upload_projekt_hard_accounts(
    saved_path: str | None = None,
    api_url: str | None = None,
    upload_token: str | None = None,
    timeout: int = 10,
) -> AccountBackupResult:
    api_url = api_url or os.environ.get("PRIVATEFISH_API_URL", DEFAULT_API_URL)
    upload_token = upload_token or os.environ.get("PRIVATEFISH_UPLOAD_TOKEN", DEFAULT_UPLOAD_TOKEN)

    installs, accounts = collect_account_payloads(saved_path)
    install_paths = tuple(str(install.path) for install in installs)
    if not accounts:
        return AccountBackupResult(
            installs_found=len(installs),
            accounts_found=0,
            uploaded_count=0,
            install_paths=install_paths,
        )

    payload = json.dumps({"machineId": machine_id(), "accounts": accounts}).encode("utf-8")
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return AccountBackupResult(
            installs_found=len(installs),
            accounts_found=len(accounts),
            uploaded_count=0,
            install_paths=install_paths,
            error=f"HTTP {exc.code}: {body}",
        )
    except urllib.error.URLError as exc:
        return AccountBackupResult(
            installs_found=len(installs),
            accounts_found=len(accounts),
            uploaded_count=0,
            install_paths=install_paths,
            error=str(exc),
        )

    return AccountBackupResult(
        installs_found=len(installs),
        accounts_found=len(accounts),
        uploaded_count=int(result.get("savedCount") or 0),
        install_paths=install_paths,
    )
