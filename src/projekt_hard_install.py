"""Projekt Hard installation discovery helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


SCAN_NAME_HINTS = ("projekt", "project", "metin")
STRONG_EXE_HINTS = ("projekt", "project", "metin")
LAUNCHER_EXE_HINTS = ("client", "patcher", "launcher")
MAX_SCAN_DEPTH = 2


@dataclass(frozen=True)
class ProjektHardInstall:
    path: Path
    reason: str


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
    """Find a likely Projekt Hard installation folder.

    The scan is intentionally shallow so startup does not freeze on large drives.
    """
    if saved_path:
        path = Path(saved_path).expanduser()
        if _looks_like_projekt_hard(path):
            return ProjektHardInstall(path=path, reason="saved")

    for root in _candidate_roots():
        if _looks_like_projekt_hard(root):
            return ProjektHardInstall(path=root, reason="root")

        for candidate in _iter_candidate_dirs(root):
            if _looks_like_projekt_hard(candidate):
                return ProjektHardInstall(path=candidate, reason="scan")

    return None
