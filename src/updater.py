"""Self-update support for packaged releases.

The updater is intentionally stdlib-only so it keeps working inside a
PyInstaller one-file executable without adding runtime dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


REPO_OWNER = "Ex1t-S"
REPO_NAME = "PrivateFish"
API_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"
ASSET_PREFIX = "Huangue Fish bot v "
TOKEN_ENV_VAR = "PRIVATEFISH_GITHUB_TOKEN"
TOKEN_FILE = "github_token.txt"
CREATE_NO_WINDOW = 0x08000000


def _log(message: str) -> None:
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_path = Path.cwd() / "bot_runtime.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] updater: {message}\n")
    except Exception:
        pass


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value or "")
    return tuple(int(part) for part in parts) if parts else (0,)


def _is_newer(remote: str, current: str) -> bool:
    remote_parts = _version_tuple(remote)
    current_parts = _version_tuple(current)
    max_len = max(len(remote_parts), len(current_parts))
    remote_parts += (0,) * (max_len - len(remote_parts))
    current_parts += (0,) * (max_len - len(current_parts))
    return remote_parts > current_parts


def _token_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).with_name(TOKEN_FILE)
    return Path.cwd() / TOKEN_FILE


def _github_token() -> str:
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if token:
        return token

    path = _token_path()
    if not path.exists():
        return ""

    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _github_headers(accept: str) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": f"{REPO_OWNER}-{REPO_NAME}-updater",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers=_github_headers("application/vnd.github+json"))
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _find_exe_asset(release: dict) -> dict | None:
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.startswith(ASSET_PREFIX) and name.lower().endswith(".exe"):
            return asset
    return None


def _download_asset(asset: dict, destination: Path) -> None:
    request = urllib.request.Request(asset["url"], headers=_github_headers("application/octet-stream"))
    with urllib.request.urlopen(request, timeout=60) as response:
        with destination.open("wb") as f:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)


def _verify_digest(path: Path, digest: str | None) -> bool:
    if not digest:
        return True
    if not digest.startswith("sha256:"):
        return True
    expected = digest.split(":", 1)[1].lower()
    actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
    return actual == expected


def _launch_replacer(downloaded_exe: Path, current_exe: Path) -> None:
    helper_path = Path(tempfile.gettempdir()) / f"huangue_update_{os.getpid()}.cmd"
    helper = f"""@echo off
setlocal
set "SRC={downloaded_exe}"
set "DST={current_exe}"
set "PID={os.getpid()}"
:wait
tasklist /FI "PID eq %PID%" | find "%PID%" >nul
if not errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto wait
)
move /Y "%SRC%" "%DST%" >nul
start "" "%DST%"
del "%~f0"
"""
    helper_path.write_text(helper, encoding="utf-8")
    subprocess.Popen(
        ["cmd.exe", "/c", str(helper_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


def check_for_update(current_version: str) -> bool:
    """Download and schedule replacement when a newer GitHub release exists.

    Returns True only when the current process should exit immediately.
    """
    if not getattr(sys, "frozen", False):
        return False

    current_exe = Path(sys.executable)
    try:
        release = _http_json(API_URL)
        remote_version = str(release.get("tag_name") or release.get("name") or "").lstrip("vV")
        if not _is_newer(remote_version, current_version):
            return False

        asset = _find_exe_asset(release)
        if not asset:
            _log(f"release {remote_version} has no matching exe asset")
            return False

        if not asset.get("url"):
            _log(f"release {remote_version} asset has no API URL")
            return False

        target = Path(tempfile.gettempdir()) / asset["name"]
        _log(f"downloading {asset['name']} from release {remote_version}")
        _download_asset(asset, target)

        expected_size = int(asset.get("size") or 0)
        if expected_size and target.stat().st_size != expected_size:
            _log(f"download size mismatch: {target.stat().st_size} != {expected_size}")
            target.unlink(missing_ok=True)
            return False

        if not _verify_digest(target, asset.get("digest")):
            _log("download digest mismatch")
            target.unlink(missing_ok=True)
            return False

        _launch_replacer(target, current_exe)
        return True
    except (urllib.error.URLError, TimeoutError) as exc:
        _log(f"network check failed: {exc}")
        return False
    except Exception as exc:
        _log(f"update failed: {exc}")
        return False
