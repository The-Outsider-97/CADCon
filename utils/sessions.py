"""Per-instance working directories for the copies of models that CADCon imports and converts.

CADCon keeps the uploaded models and converted outputs it works on as files, but remembers which file belongs to which model only in memory. Before
this module those files lived in one shared ``uploads``/``outputs`` folder and were never removed, so after every run (and after a crash) copies of
the user's CAD files stayed on disk with no way to see or delete them from the application.

Each running instance now owns one folder, ``<data_dir>/sessions/<pid>-<token>``, holding an ``owner.json`` with its process id and start time.

* A clean shutdown removes the instance's own folder.
* Start-up removes only folders whose owning process is *not running* (a crash, a killed window, a power loss). A folder whose owner cannot be
  determined is left alone: leaving a stale folder is a smaller harm than deleting a live instance's files. This matters because CADCon falls back to
  another port while an earlier instance may still be running against the same data directory.
* Folders left by the previous flat layout (``uploads`` / ``outputs``) are purged by age, since no process id was ever recorded for them.

Only paths below ``<data_dir>/sessions`` and the two legacy folders are ever deleted, and symbolic links are never followed.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

OWNER_FILE = "owner.json"
SESSIONS = "sessions"
LEGACY_FOLDERS = ("uploads", "outputs")
GRACE_SECONDS = 60.0          # a session that has just been created may not have written its owner record yet
LEGACY_MAX_AGE_SECONDS = 24 * 3600.0
START_TIME_TOLERANCE = 2.0    # seconds; process start times are floats read from the operating system


def _start_time(pid: int) -> float | None | bool:
    """Start time of ``pid``; ``None`` when it is not running; ``True`` when it cannot be determined (treated as alive)."""
    try:
        import psutil
    except ImportError:
        if os.name == "nt":
            return True                     # cannot tell without psutil: never delete
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            return True
        return True
    try:
        return float(psutil.Process(pid).create_time())
    except psutil.NoSuchProcess:
        return None
    except (psutil.AccessDenied, psutil.Error):
        return True


def owner_is_alive(record: dict) -> bool:
    """Whether the process described by an owner record is still the one that created the session (guards against pid reuse)."""
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    now = _start_time(pid)
    if now is None:
        return False
    recorded = record.get("started")
    if now is True or not isinstance(recorded, (int, float)) or isinstance(recorded, bool):
        return True
    return abs(now - float(recorded)) <= START_TIME_TOLERANCE


def sessions_root(data_dir: Path) -> Path:
    return Path(data_dir) / SESSIONS


def create_session(data_dir: Path) -> Path:
    """Create this instance's private working folder (with ``uploads`` and ``outputs``) and record its owner."""
    root = sessions_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    session = root / f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
    session.mkdir()
    (session / "uploads").mkdir()
    (session / "outputs").mkdir()
    started = _start_time(os.getpid())
    record = {"pid": os.getpid(), "started": started if isinstance(started, float) else None}
    temporary = session / (OWNER_FILE + ".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.replace(temporary, session / OWNER_FILE)
    return session


def _is_real_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def remove_session(session: Path, data_dir: Path) -> bool:
    """Delete one session folder. Refuses anything that is not a real folder directly below ``<data_dir>/sessions``."""
    root = sessions_root(data_dir)
    session = Path(session)
    if not root.exists() or session.parent.resolve() != root.resolve():
        return False
    if not _is_real_directory(session):
        return False
    shutil.rmtree(session, ignore_errors=True)
    return not session.exists()


def reap_stale_sessions(data_dir: Path, *, now: float | None = None) -> list[str]:
    """Remove sessions whose owner is no longer running. Returns the names that were removed."""
    root = sessions_root(data_dir)
    if not _is_real_directory(root):
        return []
    now = time.time() if now is None else now
    removed: list[str] = []
    for entry in sorted(root.iterdir()):
        if not _is_real_directory(entry):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age < GRACE_SECONDS:
            continue
        try:
            record = json.loads((entry / OWNER_FILE).read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError
        except (OSError, ValueError):
            record = {}                    # no readable owner record: only an old, ownerless folder is removed
        if record and owner_is_alive(record):
            continue
        if remove_session(entry, data_dir):
            removed.append(entry.name)
    return removed


def reap_legacy_files(data_dir: Path, *, max_age: float = LEGACY_MAX_AGE_SECONDS, now: float | None = None) -> int:
    """Delete files older than ``max_age`` from the pre-session ``uploads``/``outputs`` folders. Returns how many were removed."""
    now = time.time() if now is None else now
    count = 0
    for name in LEGACY_FOLDERS:
        folder = Path(data_dir) / name
        if not _is_real_directory(folder):
            continue
        for entry in folder.iterdir():
            try:
                if entry.is_symlink() or not entry.is_file() or now - entry.stat().st_mtime < max_age:
                    continue
                entry.unlink()
                count += 1
            except OSError:
                continue
    return count
