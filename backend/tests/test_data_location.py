"""Where the data folder comes from, and how data left by older layouts is adopted.

Two things are being proven here, and they are different risks:

* the LOCATION rules - which directory a process uses. They are pure functions
  (an application root, an environment and a platform name are passed in), so
  every branch is tested without touching the real profile, and a regression
  cannot quietly move a teacher's database.
* the MIGRATION - copying an existing database into the new location. It is
  exercised on real SQLite files, with the source connection left open (a live
  app's database) to prove the copy is consistent, and with an assertion that
  the source is byte-for-byte unchanged afterwards: adoption must never be a
  move, because a wrong guess would then be data loss.

The last test boots the real server against an adopted folder and asks the
public API for the migrated class, because "the files are there" is not the
same as "the app shows them".
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app import data_location as dl

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(tmp_path: Path, **extra: str) -> dict:
    """A clean environment: no real profile paths, no MC5_DATA_DIR."""
    env = {
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "TEMP": str(tmp_path / "temp"),
        "TMP": str(tmp_path / "temp"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
    }
    env.update(extra)
    return env


def _app_root(tmp_path: Path, *, checkout: bool = False,
              portable: bool = False, beside_data: bool = False) -> Path:
    root = tmp_path / "app"
    (root / "backend" / "app").mkdir(parents=True, exist_ok=True)
    if checkout:
        (root / ".git").mkdir()
    if portable:
        (root / "portable.flag").write_text("", encoding="utf-8")
    if beside_data:
        (root / "data").mkdir()
        sqlite3.connect(str(root / "data" / "app.db")).close()
    return root


def _source_dir(path: Path, *, rows: int = 1) -> Path:
    """A realistic legacy folder: database, settings, secrets, and junk."""
    path.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path / "app.db"))
    con.execute("CREATE TABLE courses(id TEXT PRIMARY KEY, name TEXT)")
    for index in range(rows):
        con.execute("INSERT INTO courses VALUES(?,?)",
                    (f"COURSE-{index}", f"4Ma1 Group {index}"))
    con.commit()
    con.close()
    (path / "config.toml").write_text("[app]\nrole = \"teacher\"\n", encoding="utf-8")
    (path / "secrets.toml").write_text("[gemini]\napi_keys = []\n", encoding="utf-8")
    (path / "mark_schemes").mkdir(exist_ok=True)
    (path / "mark_schemes" / "cw1.pdf").write_bytes(b"%PDF-1.4 scheme")
    (path / "retained").mkdir(exist_ok=True)
    (path / "retained" / "submission.pdf").write_bytes(b"%PDF-1.4 answer")
    # Per-process / regenerable state that must NOT be carried over.
    (path / "instance.json").write_text('{"pid": 1, "port": 8642}', encoding="utf-8")
    (path / "logs").mkdir(exist_ok=True)
    (path / "logs" / "app.log").write_text("old log\n", encoding="utf-8")
    (path / "tmp").mkdir(exist_ok=True)
    (path / "tmp" / "scratch.bin").write_bytes(b"scratch")
    return path


def _copy_db_snapshot(source: Path, target: Path) -> None:
    """Copy a live WAL database (a plain file copy would miss the -wal rows)."""
    reader = sqlite3.connect(str(source))
    try:
        writer = sqlite3.connect(str(target))
        try:
            with writer:
                reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _names(path: Path) -> set[str]:
    return {p.name for p in path.iterdir()}


# ------------------------------------------------------------------ location

def test_explicit_mc5_data_dir_always_wins(tmp_path):
    """A caller that names a folder gets that folder - never a "better" one."""
    root = _app_root(tmp_path, checkout=True, portable=True, beside_data=True)
    env = _env(tmp_path, MC5_DATA_DIR=str(tmp_path / "chosen"))
    assert dl.resolve_data_dir(root, env, "windows") == tmp_path / "chosen"


def test_portable_copy_keeps_its_data_beside_the_app(tmp_path):
    root = _app_root(tmp_path, portable=True)
    env = _env(tmp_path)
    assert dl.resolve_data_dir(root, env, "windows") == root / "data"


def test_an_existing_installation_keeps_data_where_it_already_is(tmp_path):
    """Data next to the executable predates this rule and must not be moved."""
    root = _app_root(tmp_path, beside_data=True)
    env = _env(tmp_path)
    resolved = dl.resolve_data_dir(root, env, "windows")
    assert resolved == root / "data"
    assert dl.has_data(resolved), "the existing database is still the one in use"


def test_a_development_checkout_uses_its_own_data_folder(tmp_path):
    root = _app_root(tmp_path, checkout=True)
    env = _env(tmp_path)
    assert dl.resolve_data_dir(root, env, "windows") == root / "data"


def test_an_installed_copy_uses_the_per_user_folder(tmp_path):
    root = _app_root(tmp_path)
    env = _env(tmp_path)
    assert dl.resolve_data_dir(root, env, "windows") == \
        tmp_path / "local" / "MathChecker"
    assert dl.resolve_data_dir(root, env, "macos") == \
        tmp_path / "home" / "Library" / "Application Support" / "MathChecker"
    assert dl.resolve_data_dir(root, env, "linux") == \
        tmp_path / "home" / ".local" / "share" / "mathchecker"


def test_linux_honours_xdg_data_home(tmp_path):
    root = _app_root(tmp_path)
    env = _env(tmp_path, XDG_DATA_HOME=str(tmp_path / "xdg"))
    assert dl.resolve_data_dir(root, env, "linux") == tmp_path / "xdg" / "mathchecker"


def test_the_default_location_is_never_inside_the_temp_folder(tmp_path):
    """The whole point: a temp cleanup must not be able to delete the marks."""
    root = _app_root(tmp_path)
    env = _env(tmp_path)
    temp_root = (tmp_path / "temp").resolve()
    for platform in ("windows", "macos", "linux"):
        resolved = dl.resolve_data_dir(root, env, platform).resolve()
        assert resolved.is_absolute()
        assert not resolved.is_relative_to(temp_root), \
            f"{platform} default {resolved} lives in the temp folder {temp_root}"
        assert resolved != temp_root


def tempfile_dir() -> str:
    import tempfile
    return tempfile.gettempdir()


def tmp_path_dir() -> Path:
    """A path that certainly does not exist (portable-marker checks)."""
    return Path(tempfile_dir()) / "__no_such_mathchecker_copy__"


def _config_data_dir(env_extra: dict) -> Path:
    """What a FRESH interpreter binds as config.DATA_DIR (import-time decision)."""
    env = dict(os.environ)
    env.pop("MC5_DATA_DIR", None)
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'backend'); import app.config as c; "
         "print(c.DATA_DIR)"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return Path(out.stdout.strip())


def test_config_binds_the_folder_the_resolver_returns(tmp_path):
    """An explicit MC5_DATA_DIR is what configuration actually binds."""
    chosen = tmp_path / "chosen"
    assert _config_data_dir({"MC5_DATA_DIR": str(chosen)}) == chosen


def test_this_checkout_defaults_to_its_own_data_folder():
    """No override in a source checkout: the documented ``data/`` folder."""
    assert _config_data_dir({}) == REPO_ROOT / "data"
    assert not dl.is_portable(tmp_path_dir() / "nothing-here")


# ---------------------------------------------------------------- candidates

def test_legacy_candidates_prefer_the_old_beside_app_layout(tmp_path):
    root = _app_root(tmp_path, beside_data=True)
    temp = tmp_path / "temp"
    _source_dir(temp / "MathChecker")
    _source_dir(temp / "mc5_ui")
    target = tmp_path / "local" / "MathChecker"

    candidates = dl.legacy_candidates(target, root, _env(tmp_path))
    assert candidates == [root / "data", temp / "MathChecker", temp / "mc5_ui"]


def test_legacy_candidates_ignore_folders_without_a_database_and_the_target(
        tmp_path):
    root = _app_root(tmp_path)
    temp = tmp_path / "temp"
    (temp / "MathChecker").mkdir(parents=True)          # empty: not data
    _source_dir(temp / "mc5_ui")
    assert dl.legacy_candidates(temp / "mc5_ui", root, _env(tmp_path)) == []


# ----------------------------------------------------------------- adoption

def test_adoption_copies_data_and_leaves_the_source_untouched(tmp_path):
    source = _source_dir(tmp_path / "legacy", rows=3)
    before_db = _digest(source / "app.db")
    before_files = _names(source)
    target = tmp_path / "install" / "data"

    result = dl.adopt(source, target)

    assert result.files >= 4 and result.bytes > 0
    assert (target / "app.db").is_file()
    with sqlite3.connect(str(target / "app.db")) as con:
        assert con.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 3
    for kept in ("config.toml", "secrets.toml", "mark_schemes/cw1.pdf",
                 "retained/submission.pdf"):
        assert (target / kept).is_file(), f"{kept} must come along"
    for skipped in ("instance.json", "logs", "tmp", "app.db-wal"):
        assert not (target / skipped).exists(), f"{skipped} must not be copied"

    receipt = json.loads((target / "migration.json").read_text(encoding="utf-8"))
    assert receipt["adopted_from"] == str(source)
    assert receipt["files"] >= 4
    assert "api_keys" not in json.dumps(receipt), "no secret material in the receipt"

    assert _digest(source / "app.db") == before_db, "the source must not be modified"
    assert _names(source) == before_files, "nothing in the source may be removed"


def test_adoption_is_consistent_while_the_source_is_open(tmp_path):
    """A live app holds its database open; the copy must still see every row.

    Rows written into a WAL that has not been checkpointed yet are invisible to
    a plain file copy of ``app.db`` alone. SQLite's backup API is used precisely
    so an adoption of a running installation cannot silently lose the newest
    corrections.
    """
    source = tmp_path / "legacy"
    source.mkdir()
    con = sqlite3.connect(str(source / "app.db"))
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE results(id TEXT PRIMARY KEY)")
        con.execute("INSERT INTO results VALUES('r1')")
        con.commit()
        # Deliberately NOT closed/checkpointed: this is the live case.

        target = tmp_path / "adopted"
        dl.adopt(source, target)

        with sqlite3.connect(str(target / "app.db")) as check:
            assert check.execute("SELECT id FROM results").fetchall() == [("r1",)]
    finally:
        con.close()


def test_adoption_refuses_to_overwrite_existing_data(tmp_path):
    source = _source_dir(tmp_path / "legacy")
    target = _source_dir(tmp_path / "install")
    before = _digest(target / "app.db")

    with pytest.raises(dl.AlreadyHasData):
        dl.adopt(source, target)

    assert _digest(target / "app.db") == before, "the existing database is intact"
    assert not (target / "migration.json").exists()


def test_adoption_refuses_a_folder_with_no_database(tmp_path):
    empty = tmp_path / "legacy"
    empty.mkdir()
    with pytest.raises(dl.NoDataToAdopt):
        dl.adopt(empty, tmp_path / "nowhere")
    assert not (tmp_path / "nowhere").exists(), "nothing is created for a refusal"


def test_a_failed_adoption_leaves_no_half_written_database(tmp_path,
                                                           monkeypatch):
    """A copy that dies half way must not look like real data on the next start."""
    source = _source_dir(tmp_path / "legacy")
    target = tmp_path / "install"

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(dl, "_copy_database", boom)
    with pytest.raises(OSError):
        dl.adopt(source, target)

    assert not (target / "app.db").exists()
    assert not (target.parent / f".{target.name}.adopting").exists()
    assert dl.has_data(source)


# -------------------------------------------------------------- auto adoption

def test_auto_adoption_rescues_data_from_the_temp_folder(tmp_path):
    root = _app_root(tmp_path)
    env = _env(tmp_path)
    _source_dir(tmp_path / "temp" / "mc5_ui", rows=2)

    result = dl.auto_adopt(app_root=root, env=env)

    assert result is not None
    target = tmp_path / "local" / "MathChecker"
    assert result.target == target
    with sqlite3.connect(str(target / "app.db")) as con:
        assert con.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 2
    assert (target / "secrets.toml").is_file()
    # The temp copy is still there, byte-identical: adoption copies.
    assert (tmp_path / "temp" / "mc5_ui" / "app.db").is_file()
    # And a second start does not adopt again (or overwrite anything).
    assert dl.auto_adopt(app_root=root, env=env) is None


def test_auto_adoption_never_second_guesses_an_explicit_folder(tmp_path):
    root = _app_root(tmp_path)
    env = _env(tmp_path, MC5_DATA_DIR=str(tmp_path / "chosen"))
    _source_dir(tmp_path / "temp" / "mc5_ui")

    assert dl.auto_adopt(app_root=root, env=env) is None
    assert not (tmp_path / "chosen").exists(), "an explicit folder is untouched"


def test_a_fresh_install_stays_fresh(tmp_path):
    root = _app_root(tmp_path)
    env = _env(tmp_path)
    assert dl.auto_adopt(app_root=root, env=env) is None
    assert not (tmp_path / "local" / "MathChecker").exists()


# ------------------------------------------------------------------ the CLI

def _run(args: list[str], env: dict, timeout: int = 120) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged.update(env)
    merged["MC5_NO_BROWSER"] = "1"
    merged["MC5_LOGIN_REQUIRED"] = "0"
    return subprocess.run([sys.executable, str(REPO_ROOT / "main.py"), *args],
                          cwd=REPO_ROOT, env=merged, capture_output=True,
                          text=True, timeout=timeout)


def test_the_command_copies_an_old_folder_into_this_install(tmp_path, fresh_state):
    """``main.py --adopt-data`` - the teacher's own choice, end to end."""
    source = tmp_path / "legacy"
    source.mkdir()
    _copy_db_snapshot(fresh_state / "app.db", source / "app.db")
    with sqlite3.connect(str(source / "app.db")) as con:
        con.execute("INSERT INTO courses(id, name, section, state, update_time, "
                    "fetched_at) VALUES('LEGACY-1','Legacy 4Ma1',NULL,'ACTIVE',"
                    "'2026-01-01T00:00:00Z',0)")
    (source / "config.toml").write_text("[app]\nrole = \"teacher\"\n", encoding="utf-8")
    target = tmp_path / "install-data"

    result = _run(["--adopt-data", str(source), "--into", str(target)],
                  {"MC5_DATA_DIR": str(tmp_path / "scratch")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "adopted" in result.stdout.lower()
    with sqlite3.connect(str(target / "app.db")) as con:
        names = [r[0] for r in con.execute("SELECT name FROM courses")]
    assert "Legacy 4Ma1" in names
    assert (target / "config.toml").is_file()
    assert (target / "migration.json").is_file()

    again = _run(["--adopt-data", str(source), "--into", str(target)],
                 {"MC5_DATA_DIR": str(tmp_path / "scratch")})
    assert again.returncode == 3, again.stdout
    assert "refusing to overwrite" in again.stdout.lower()

    # The source is not a move: it still holds its row.
    with sqlite3.connect(str(source / "app.db")) as con:
        assert con.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 1


# --------------------------------------------------------------- the server

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _start_server(cwd: Path, port: int, overrides: dict) -> subprocess.Popen:
    """Start ``main.py --serve`` (the plain backend) with the given environment."""
    env = dict(os.environ)
    env.pop("MC5_DATA_DIR", None)  # the test decides the folder, not conftest
    env.update({"MC5_NO_BROWSER": "1", "MC5_LOGIN_REQUIRED": "0",
                "MC5_PORT": str(port)})
    env.update(overrides)
    return subprocess.Popen([sys.executable, "main.py", "--serve", str(port)],
                            cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def _stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            proc.kill()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:  # pragma: no cover - teardown only
        proc.kill()


def _wait_for(port: int, path: str, needle: str, timeout: float = 90) -> str:
    """Poll ``path`` until ``needle`` is in the body; return the last body."""
    deadline = time.time() + timeout
    body = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                        timeout=2) as resp:
                body = resp.read().decode("utf-8")
            if needle in body:
                return body
        except Exception:  # noqa: BLE001 - still starting
            pass
        time.sleep(0.4)
    return body


def test_the_server_serves_data_that_was_adopted(tmp_path, fresh_state):
    """The real interface: an adopted folder must be a working data folder."""
    source = tmp_path / "legacy"
    source.mkdir()
    _copy_db_snapshot(fresh_state / "app.db", source / "app.db")
    with sqlite3.connect(str(source / "app.db")) as con:
        con.execute("INSERT INTO courses(id, name, section, state, update_time, "
                    "fetched_at) VALUES('LEGACY-2','Adopted 4Ma1',NULL,'ACTIVE',"
                    "'2026-01-01T00:00:00Z',0)")
    target = tmp_path / "install-data"
    dl.adopt(source, target)

    port = _free_port()
    proc = _start_server(REPO_ROOT, port, {"MC5_DATA_DIR": str(target)})
    try:
        body = _wait_for(port, "/api/classes", "Adopted 4Ma1")
        assert body, "the server never answered /api/classes"
        assert "Adopted 4Ma1" in body, body[:400]
    finally:
        _stop_server(proc)


def test_startup_adopts_a_temp_folder_without_being_told(tmp_path, fresh_state):
    """The headline behaviour, through the app's own startup path.

    A copy of the application (no ``.git``, no ``data/``) is treated as an
    installation: it must resolve its data to the per-user folder, find the
    database an older launch left in the temp folder, adopt it, and serve those
    classes - with the temp folder still intact afterwards.
    """
    install = tmp_path / "install"
    install.mkdir()
    shutil.copy2(REPO_ROOT / "main.py", install / "main.py")
    shutil.copytree(REPO_ROOT / "backend", install / "backend",
                    ignore=shutil.ignore_patterns("tests", "scripts",
                                                  "__pycache__", "*.pyc"))

    legacy = tmp_path / "temp" / "mc5_ui"
    legacy.mkdir(parents=True)
    _copy_db_snapshot(fresh_state / "app.db", legacy / "app.db")
    with sqlite3.connect(str(legacy / "app.db")) as con:
        con.execute("INSERT INTO courses(id, name, section, state, update_time, "
                    "fetched_at) VALUES('STARTUP-1','Rescued 4Ma1',NULL,'ACTIVE',"
                    "'2026-01-01T00:00:00Z',0)")
    (legacy / "config.toml").write_text("[app]\nrole = \"teacher\"\n",
                                         encoding="utf-8")
    local = tmp_path / "local"
    port = _free_port()
    proc = _start_server(install, port, {
        "LOCALAPPDATA": str(local),
        "APPDATA": str(tmp_path / "roaming"),
        "TEMP": str(tmp_path / "temp"),
        "TMP": str(tmp_path / "temp"),
    })
    try:
        body = _wait_for(port, "/api/classes", "Rescued 4Ma1")
        assert body, "the server never answered /api/classes"
        assert "Rescued 4Ma1" in body, body[:400]
    finally:
        _stop_server(proc)

    adopted = local / "MathChecker"
    assert (adopted / "app.db").is_file(), "the data now lives in the per-user folder"
    assert (adopted / "config.toml").is_file()
    assert (adopted / "migration.json").is_file(), "the move is explained on disk"
    assert (legacy / "app.db").is_file(), "the temp copy is left untouched"
    assert not (install / "data" / "app.db").exists()
