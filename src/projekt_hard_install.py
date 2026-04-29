"""Projekt Hard installation discovery helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


GAME_EXE_NAME = "Projekt-Hard.exe"
USER_DATA_DIR = "user_data"
ACCOUNTS_DIR = "accounts"
SCAN_NAME_HINTS = ("projekt", "project", "metin")
STRONG_EXE_HINTS = ("projekt", "project", "metin")
LAUNCHER_EXE_HINTS = ("client", "patcher", "launcher")
MAX_SCAN_DEPTH = 4


@dataclass(frozen=True)
class ProjektHardInstall:
    path: Path
    reason: str
    exe_path: Path | None
    user_data_path: Path
    accounts_path: Path
    account_files: tuple[Path, ...]

    @property
    def has_user_data(self) -> bool:
        return self.user_data_path.is_dir()

    @property
    def has_accounts_dir(self) -> bool:
        return self.accounts_path.is_dir()

    @property
    def has_accounts(self) -> bool:
        return bool(self.account_files)

    @property
    def has_game_exe(self) -> bool:
        return self.exe_path is not None and self.exe_path.is_file()


def _looks_like_projekt_hard(path: Path) -> bool:
    if not path.is_dir():
        return False

    name = path.name.lower()
    has_project_name = "projekt" in name or "project" in name
    has_hard_name = "hard" in name

    try:
        exe_names = [child.name.lower() for child in path.iterdir() if child.is_file() and child.suffix.lower() == ".exe"]
    except OSError:
        return False

    has_strong_exe = any(any(hint in exe_name for hint in STRONG_EXE_HINTS) for exe_name in exe_names)
    has_launcher_exe = any(any(hint in exe_name for hint in LAUNCHER_EXE_HINTS) for exe_name in exe_names)
    return (
        has_project_name and has_hard_name
        or has_project_name and has_launcher_exe
        or has_hard_name and has_strong_exe
        or "metin" in name and has_launcher_exe
    )


def _account_files(accounts_path: Path) -> tuple[Path, ...]:
    if not accounts_path.is_dir():
        return ()

    try:
        files = [
            child
            for child in accounts_path.iterdir()
            if child.is_file() and child.suffix.lower() in {".json", ".cfg", ".ini", ".txt"}
        ]
    except OSError:
        return ()
    return tuple(sorted(files, key=lambda file_path: file_path.name.lower()))


def _install_from_game_exe(exe_path: Path, reason: str) -> ProjektHardInstall:
    root = exe_path.parent
    user_data_path = root / USER_DATA_DIR
    accounts_path = user_data_path / ACCOUNTS_DIR
    return ProjektHardInstall(
        path=root,
        reason=reason,
        exe_path=exe_path,
        user_data_path=user_data_path,
        accounts_path=accounts_path,
        account_files=_account_files(accounts_path),
    )


def _install_from_root(root: Path, reason: str) -> ProjektHardInstall:
    user_data_path = root / USER_DATA_DIR
    accounts_path = user_data_path / ACCOUNTS_DIR
    exe_path = root / GAME_EXE_NAME
    return ProjektHardInstall(
        path=root,
        reason=reason,
        exe_path=exe_path if exe_path.is_file() else None,
        user_data_path=user_data_path,
        accounts_path=accounts_path,
        account_files=_account_files(accounts_path),
    )


def _has_projekt_hard_structure(path: Path) -> bool:
    if not path.is_dir():
        return False

    name = path.name.lower()
    has_name_hint = ("projekt" in name or "project" in name) and "hard" in name
    return has_name_hint and (path / USER_DATA_DIR / ACCOUNTS_DIR).is_dir()


def _find_game_exe(path: Path) -> Path | None:
    direct = path / GAME_EXE_NAME
    if direct.is_file():
        return direct

    try:
        for child in path.iterdir():
            if child.is_dir():
                nested = child / GAME_EXE_NAME
                if nested.is_file():
                    return nested
    except OSError:
        return None
    return None


def _add_install(found: list[ProjektHardInstall], seen: set[str], install: ProjektHardInstall) -> None:
    try:
        key = str(install.path.resolve()).lower()
    except OSError:
        key = str(install.path).lower()
    if key in seen:
        return
    seen.add(key)
    found.append(install)


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "PUBLIC"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value))

    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        user_path = Path(userprofile)
        roots.extend(
            [
                user_path / "Desktop",
                user_path / "Downloads",
                user_path / "Games",
                user_path / "Documents",
            ]
        )

    for drive in "CDEFG":
        root = Path(f"{drive}:\\")
        if root.exists():
            roots.append(root)

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = str(root.resolve()).lower()
        except OSError:
            resolved = str(root).lower()
        if resolved not in seen and root.exists():
            seen.add(resolved)
            unique.append(root)
    return unique


def _iter_candidate_dirs(root: Path, max_depth: int = MAX_SCAN_DEPTH):
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth > max_depth:
            continue

        try:
            children = [child for child in current.iterdir() if child.is_dir()]
        except OSError:
            continue

        for child in children:
            child_name = child.name.lower()
            if any(hint in child_name for hint in SCAN_NAME_HINTS) or child_name == "hard":
                yield child
            if depth < max_depth:
                queue.append((child, depth + 1))


def find_projekt_hard_install(saved_path: str | None = None) -> ProjektHardInstall | None:
    """Find a Projekt Hard installation folder containing Projekt-Hard.exe.

    The scan is intentionally shallow so startup does not freeze on large drives.
    """
    if saved_path:
        path = Path(saved_path).expanduser()
        exe_path = _find_game_exe(path)
        if exe_path:
            return _install_from_game_exe(exe_path, "saved")
        if _has_projekt_hard_structure(path):
            return _install_from_root(path, "saved-structure")

    for root in _candidate_roots():
        exe_path = _find_game_exe(root)
        if exe_path:
            return _install_from_game_exe(exe_path, "root")
        if _has_projekt_hard_structure(root):
            return _install_from_root(root, "root-structure")

        for candidate in _iter_candidate_dirs(root):
            exe_path = _find_game_exe(candidate)
            if exe_path:
                return _install_from_game_exe(exe_path, "scan")
            if _has_projekt_hard_structure(candidate):
                return _install_from_root(candidate, "scan-structure")

            if _looks_like_projekt_hard(candidate):
                try:
                    exe_path = next(candidate.rglob(GAME_EXE_NAME))
                except (OSError, StopIteration):
                    exe_path = None
                if exe_path:
                    return _install_from_game_exe(exe_path, "scan")

    return None


def find_all_projekt_hard_installs(saved_path: str | None = None) -> list[ProjektHardInstall]:
    """Find every Projekt Hard installation folder containing Projekt-Hard.exe."""
    found: list[ProjektHardInstall] = []
    seen: set[str] = set()

    if saved_path:
        path = Path(saved_path).expanduser()
        exe_path = _find_game_exe(path)
        if exe_path:
            _add_install(found, seen, _install_from_game_exe(exe_path, "saved"))
        elif _has_projekt_hard_structure(path):
            _add_install(found, seen, _install_from_root(path, "saved-structure"))

    for root in _candidate_roots():
        exe_path = _find_game_exe(root)
        if exe_path:
            _add_install(found, seen, _install_from_game_exe(exe_path, "root"))
        elif _has_projekt_hard_structure(root):
            _add_install(found, seen, _install_from_root(root, "root-structure"))

        for candidate in _iter_candidate_dirs(root):
            exe_path = _find_game_exe(candidate)
            if exe_path:
                _add_install(found, seen, _install_from_game_exe(exe_path, "scan"))
                continue

            if _has_projekt_hard_structure(candidate):
                _add_install(found, seen, _install_from_root(candidate, "scan-structure"))
                continue

            if _looks_like_projekt_hard(candidate):
                try:
                    matches = candidate.rglob(GAME_EXE_NAME)
                    for exe_path in matches:
                        _add_install(found, seen, _install_from_game_exe(exe_path, "scan"))
                except OSError:
                    pass

    return found
