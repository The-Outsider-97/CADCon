"""Per-instance working folders: imported/converted copies must not outlive their session."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app as cadapp
from utils import sessions


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _fake_session(data_dir: Path, name: str, owner: dict | None, *, age: float = 3600) -> Path:
    folder = sessions.sessions_root(data_dir) / name
    (folder / "uploads").mkdir(parents=True)
    (folder / "uploads" / "model.stl").write_bytes(b"x")
    if owner is not None:
        (folder / sessions.OWNER_FILE).write_text(json.dumps(owner), encoding="utf-8")
    _age(folder, age)
    return folder


def test_session_has_private_folders_and_owner_record(tmp_path: Path) -> None:
    session = sessions.create_session(tmp_path)
    assert (session / "uploads").is_dir() and (session / "outputs").is_dir()
    record = json.loads((session / sessions.OWNER_FILE).read_text())
    assert record["pid"] == os.getpid()
    assert sessions.owner_is_alive(record)
    assert sessions.create_session(tmp_path) != session


def test_crashed_session_is_reaped_but_live_ones_are_kept(tmp_path: Path) -> None:
    dead = _fake_session(tmp_path, "dead", {"pid": _dead_pid(), "started": None})
    ownerless = _fake_session(tmp_path, "ownerless", None)
    mine = sessions.create_session(tmp_path)
    _age(mine, 3600)                                  # old, but its owner (this process) is running
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        import psutil
        other = _fake_session(tmp_path, "other-live", {"pid": sleeper.pid, "started": psutil.Process(sleeper.pid).create_time()})
        removed = sessions.reap_stale_sessions(tmp_path)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert sorted(removed) == ["dead", "ownerless"]
    assert not dead.exists() and not ownerless.exists()
    assert mine.exists() and other.exists()


def test_recycled_process_id_does_not_keep_a_dead_session_alive(tmp_path: Path) -> None:
    folder = _fake_session(tmp_path, "recycled", {"pid": os.getpid(), "started": time.time() - 10_000})
    assert sessions.reap_stale_sessions(tmp_path) == ["recycled"] and not folder.exists()


def test_fresh_session_without_owner_record_is_given_a_grace_period(tmp_path: Path) -> None:
    folder = _fake_session(tmp_path, "just-created", None, age=1)
    assert sessions.reap_stale_sessions(tmp_path) == [] and folder.exists()


def test_remove_session_refuses_foreign_paths(tmp_path: Path) -> None:
    sessions.create_session(tmp_path)
    outside = tmp_path / "keep"
    outside.mkdir()
    assert sessions.remove_session(outside, tmp_path) is False and outside.exists()
    assert sessions.remove_session(sessions.sessions_root(tmp_path), tmp_path) is False


@pytest.mark.skipif(os.name == "nt", reason="creating symbolic links needs elevated rights on Windows")
def test_symbolic_links_are_never_followed(tmp_path: Path) -> None:
    target = tmp_path / "precious"
    target.mkdir()
    (target / "data.txt").write_text("keep")
    root = sessions.sessions_root(tmp_path)
    root.mkdir(parents=True)
    (root / "link").symlink_to(target, target_is_directory=True)
    assert sessions.reap_stale_sessions(tmp_path) == [] and (target / "data.txt").exists()


def test_legacy_flat_folders_are_purged_by_age_only(tmp_path: Path) -> None:
    old, new = tmp_path / "uploads" / "old.stl", tmp_path / "outputs" / "new.stl"
    old.parent.mkdir(), new.parent.mkdir()
    old.write_bytes(b"x"), new.write_bytes(b"y")
    _age(old, 3 * 24 * 3600)
    assert sessions.reap_legacy_files(tmp_path) == 1
    assert not old.exists() and new.exists()


def test_application_copies_vanish_with_the_application(tmp_path: Path) -> None:
    app = cadapp.CADCon(tmp_path)
    stored = app.upload_dir / "abc.obj"
    stored.write_bytes(b"v 0 0 0\n")
    assert app.session_dir.parent == sessions.sessions_root(tmp_path) and stored.exists()
    app.close()
    assert not app.session_dir.exists() and not stored.exists()
    app.close()                                       # closing twice is harmless
