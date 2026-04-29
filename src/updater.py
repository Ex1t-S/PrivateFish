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
import tkinter as tk
from tkinter import ttk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_OWNER = "Ex1t-S"
REPO_NAME = "PrivateFish"
API_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"
RAILWAY_VERSION_URL = "https://privatefish-production.up.railway.app/version"
ASSET_PREFIX = "Huangue Fish bot v "
NORMALIZED_ASSET_PREFIX = "huanguefishbotv"
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


def _normalized_asset_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


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


def _public_http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": f"{REPO_OWNER}-{REPO_NAME}-updater"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _asset_matches(asset: dict, remote_version: str) -> bool:
    name = str(asset.get("name") or "")
    label = str(asset.get("label") or "")
    if not name.lower().endswith(".exe"):
        return False

    candidates = [name, label]
    normalized_remote = _normalized_asset_name(remote_version)
    for candidate in candidates:
        normalized = _normalized_asset_name(candidate)
        if normalized.startswith(NORMALIZED_ASSET_PREFIX):
            return normalized_remote in normalized
    return False


def _find_exe_asset(release: dict, remote_version: str) -> dict | None:
    fallback = None
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if _asset_matches(asset, remote_version):
            return asset
        if (
            fallback is None
            and name.lower().endswith(".exe")
            and _normalized_asset_name(name).startswith(NORMALIZED_ASSET_PREFIX)
        ):
            fallback = asset
    if fallback:
        _log(f"using fallback exe asset: {fallback.get('name')}")
        return fallback
    available = ", ".join(str(asset.get("name") or "?") for asset in release.get("assets", []))
    _log(f"no matching exe asset found. available assets: {available}")
    return None


class _UpdateProgressWindow:
    def __init__(self, current_version: str, remote_version: str, total_size: int):
        self.total_size = total_size
        self.root = tk.Tk()
        self.root.title("Actualizacion disponible")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)

        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text=f"Actualizando de {current_version} a {remote_version}",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(frame, text="Descargando la ultima version...").pack(anchor="w", pady=(6, 12))

        maximum = total_size if total_size > 0 else 100
        self.progress = ttk.Progressbar(frame, orient="horizontal", length=360, mode="determinate", maximum=maximum)
        self.progress.pack(fill="x")

        self.status = tk.StringVar(value="0%")
        ttk.Label(frame, textvariable=self.status).pack(anchor="e", pady=(6, 0))

        self._center()
        self.root.update()

    def _center(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"+{x}+{y}")

    def update(self, downloaded: int) -> None:
        if self.total_size > 0:
            percent = min(100, int(downloaded * 100 / self.total_size))
            self.progress["value"] = min(downloaded, self.total_size)
            self.status.set(f"{percent}%")
        else:
            self.progress["value"] = (self.progress["value"] + 3) % 100
            self.status.set(f"{downloaded // (1024 * 1024)} MB")
        self.root.update_idletasks()
        self.root.update()

    def done(self) -> None:
        self.status.set("Instalando actualizacion...")
        self.root.update_idletasks()
        self.root.update()

    def close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _download_asset(asset: dict, destination: Path, progress: _UpdateProgressWindow | None = None) -> None:
    request = urllib.request.Request(asset["url"], headers=_github_headers("application/octet-stream"))
    partial = destination.with_name(f"{destination.name}.part")
    partial.unlink(missing_ok=True)
    with urllib.request.urlopen(request, timeout=60) as response:
        with partial.open("wb") as f:
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress.update(downloaded)
            f.flush()
            os.fsync(f.fileno())
    partial.replace(destination)


def _download_url(url: str, destination: Path, progress: _UpdateProgressWindow | None = None) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": f"{REPO_OWNER}-{REPO_NAME}-updater"})
    partial = destination.with_name(f"{destination.name}.part")
    partial.unlink(missing_ok=True)
    with urllib.request.urlopen(request, timeout=60) as response:
        with partial.open("wb") as f:
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress.update(downloaded)
            f.flush()
            os.fsync(f.fileno())
    partial.replace(destination)


def _verify_digest(path: Path, digest: str | None) -> bool:
    if not digest:
        return True
    digest = digest.strip()
    if not digest.startswith("sha256:"):
        expected = digest.lower()
    else:
        expected = digest.split(":", 1)[1].lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        return True
    actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
    return actual == expected


def _launch_replacer(downloaded_exe: Path, current_exe: Path, expected_size: int = 0) -> None:
    helper_path = Path(tempfile.gettempdir()) / f"huangue_update_{os.getpid()}.cmd"
    log_path = current_exe.with_name("bot_runtime.log")
    staged_exe = current_exe.with_name(f"{current_exe.name}.update")
    backup_exe = current_exe.with_name(f"{current_exe.name}.bak")
    helper = f"""@echo off
setlocal
set "SRC={downloaded_exe}"
set "DST={current_exe}"
set "STAGE={staged_exe}"
set "BAK={backup_exe}"
set "PID={os.getpid()}"
set "EXPECTED_SIZE={expected_size}"
set "LOG={log_path}"
echo [%date% %time%] update helper started >> "%LOG%"
:wait
tasklist /FI "PID eq %PID%" | find "%PID%" >nul
if not errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto wait
)
set /a TRIES=0
:replace
set /a TRIES+=1
echo [%date% %time%] replace attempt %TRIES% >> "%LOG%"
if exist "%STAGE%" del /F /Q "%STAGE%" >nul 2>nul
copy /Y "%SRC%" "%STAGE%" >nul
if errorlevel 1 (
  echo [%date% %time%] stage copy failed >> "%LOG%"
  goto retry
)
if not exist "%STAGE%" (
  echo [%date% %time%] stage missing after copy >> "%LOG%"
  goto retry
)
for %%A in ("%STAGE%") do set "STAGE_SIZE=%%~zA"
if %EXPECTED_SIZE% GTR 0 if not "%STAGE_SIZE%"=="%EXPECTED_SIZE%" (
  echo [%date% %time%] stage size mismatch: %STAGE_SIZE% expected %EXPECTED_SIZE% >> "%LOG%"
  goto retry
)
if exist "%BAK%" del /F /Q "%BAK%" >nul 2>nul
move /Y "%DST%" "%BAK%" >nul
if errorlevel 1 (
  echo [%date% %time%] backup move failed >> "%LOG%"
  goto retry
)
move /Y "%STAGE%" "%DST%" >nul
if errorlevel 1 (
  echo [%date% %time%] final move failed, restoring backup >> "%LOG%"
  if exist "%BAK%" move /Y "%BAK%" "%DST%" >nul
  goto retry
)
if not exist "%DST%" (
  echo [%date% %time%] destination missing after final move >> "%LOG%"
  if exist "%BAK%" move /Y "%BAK%" "%DST%" >nul
  goto retry
)
for %%A in ("%DST%") do set "DST_SIZE=%%~zA"
if %EXPECTED_SIZE% GTR 0 if not "%DST_SIZE%"=="%EXPECTED_SIZE%" (
  echo [%date% %time%] destination size mismatch: %DST_SIZE% expected %EXPECTED_SIZE% >> "%LOG%"
  if exist "%BAK%" move /Y "%BAK%" "%DST%" >nul
  goto retry
)
echo [%date% %time%] replacement complete, launching >> "%LOG%"
timeout /t 1 /nobreak >nul
start "" "%DST%"
goto cleanup
:retry
if %TRIES% GEQ 30 (
  echo [%date% %time%] replacement failed after retries >> "%LOG%"
  goto cleanup
)
timeout /t 1 /nobreak >nul
goto replace
:cleanup
if exist "%STAGE%" del /F /Q "%STAGE%" >nul 2>nul
if exist "%BAK%" del /F /Q "%BAK%" >nul 2>nul
if exist "%SRC%" del /F /Q "%SRC%" >nul 2>nul
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


def _check_railway_update(current_version: str, current_exe: Path) -> bool:
    manifest = _public_http_json(f"{RAILWAY_VERSION_URL}?current={current_version}")
    if not manifest.get("ok", True):
        _log(f"Railway version check failed: {manifest.get('error')}")
        return False

    remote_version = str(manifest.get("version") or "").lstrip("vV")
    if not remote_version:
        _log("Railway version manifest has no version")
        return False

    if not _is_newer(remote_version, current_version):
        min_version = str(manifest.get("minVersion") or "")
        if not min_version or not _is_newer(min_version, current_version):
            return False

    download_url = str(manifest.get("downloadUrl") or "")
    if not download_url:
        _log(f"Railway version {remote_version} has no downloadUrl")
        return False

    expected_size = int(manifest.get("size") or 0)
    asset_name = Path(urllib.parse.urlparse(download_url).path).name or f"{ASSET_PREFIX}{remote_version}.exe"
    progress = _UpdateProgressWindow(current_version, remote_version, expected_size)
    target = Path(tempfile.gettempdir()) / f"huangue_update_{os.getpid()}_{asset_name}"
    target.unlink(missing_ok=True)

    _log(f"downloading {asset_name} from Railway manifest {remote_version}")
    _download_url(download_url, target, progress)
    progress.done()

    if expected_size and target.stat().st_size != expected_size:
        _log(f"Railway download size mismatch: {target.stat().st_size} != {expected_size}")
        progress.close()
        target.unlink(missing_ok=True)
        return False

    if not _verify_digest(target, str(manifest.get("sha256") or "")):
        _log("Railway download digest mismatch")
        progress.close()
        target.unlink(missing_ok=True)
        return False

    _launch_replacer(target, current_exe, expected_size)
    return True


def _check_github_update(current_version: str, current_exe: Path) -> bool:
    release = _http_json(API_URL)
    remote_version = str(release.get("tag_name") or release.get("name") or "").lstrip("vV")
    if not _is_newer(remote_version, current_version):
        return False

    asset = _find_exe_asset(release, remote_version)
    if not asset:
        _log(f"release {remote_version} has no matching exe asset")
        return False

    if not asset.get("url"):
        _log(f"release {remote_version} asset has no API URL")
        return False

    expected_size = int(asset.get("size") or 0)
    progress = _UpdateProgressWindow(current_version, remote_version, expected_size)
    target = Path(tempfile.gettempdir()) / f"huangue_update_{os.getpid()}_{asset['name']}"
    target.unlink(missing_ok=True)
    _log(f"downloading {asset['name']} from release {remote_version}")
    _download_asset(asset, target, progress)
    progress.done()

    if expected_size and target.stat().st_size != expected_size:
        _log(f"download size mismatch: {target.stat().st_size} != {expected_size}")
        progress.close()
        target.unlink(missing_ok=True)
        return False

    if not _verify_digest(target, asset.get("digest")):
        _log("download digest mismatch")
        progress.close()
        target.unlink(missing_ok=True)
        return False

    _launch_replacer(target, current_exe, expected_size)
    return True


def check_for_update(current_version: str) -> bool:
    """Download and schedule replacement when a newer release exists.

    Returns True only when the current process should exit immediately.
    """
    if not getattr(sys, "frozen", False):
        return False

    current_exe = Path(sys.executable)
    try:
        return _check_railway_update(current_version, current_exe)
    except (urllib.error.URLError, TimeoutError) as exc:
        _log(f"Railway update check failed: {exc}")
    except Exception as exc:
        _log(f"Railway update failed: {exc}")

    try:
        return _check_github_update(current_version, current_exe)
    except (urllib.error.URLError, TimeoutError) as exc:
        _log(f"GitHub update check failed: {exc}")
        return False
    except Exception as exc:
        _log(f"GitHub update failed: {exc}")
        return False
