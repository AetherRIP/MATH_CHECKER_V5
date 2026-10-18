"""Where Math Correct keeps its data, and how older data is adopted.

ONE decision, made here and nowhere else::

    MC5_DATA_DIR  >  data beside the app  >  development checkout  >  per-user

* ``MC5_DATA_DIR`` - an explicit choice (tests, scripts, the relay's child
  process, a second copy started on purpose). It always wins and is never
  second-guessed, migrated or "tidied".
* data beside the app - a portable copy (``portable.flag`` or
  ``Data/config.toml``) or an installation that already keeps its database
  next to the executable. Silently moving that data would be a data-loss bug,
  so it stays exactly where it is.
* a development checkout (a ``.git`` directory next to ``main.py``) -
  ``data/`` inside the repository, which every script, test and document in
  this tree already assumes.
* everything else - a per-user directory (``%LOCALAPPDATA%\\MathChecker`` on
  Windows, ``~/Library/Application Support/MathChecker`` on macOS,
  ``$XDG_DATA_HOME/mathchecker`` on Linux). An installed copy lives under
  ``Program Files`` or ``%LOCALAPPDATA%\\Programs``, and neither location is a
  safe home for a database: the first is not reliably writable and the second
  is replaced by the next install.

The per-user directory also ends the one genuinely dangerous layout this app
has had: a launch that pointed ``MC5_DATA_DIR`` at the system temp folder
(``%TEMP%\\mc5_ui``). Windows cleanup tools, Disk Cleanup and every "free up
space" helper empty that folder without asking, so a teacher's marks could
vanish between two launches. :func:`legacy_candidates` finds such a database
and :func:`adopt` copies it into the stable location the next time the app
starts there - leaving the original byte-for-byte untouched, so a wrong guess
costs nothing.

Nothing in this module imports the rest of the application (or writes to the
filesystem at import time): it is used by ``config`` before logging, services
or the database exist.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

_log = logging.getLogger(__name__)

#: Directory name used inside the platform's per-user data root.
APP_DIR_NAME = "MathChecker"
#: The database file that decides whether a directory "has data".
DB_NAME = "app.db"
#: Written into an adopted directory: where the data came from, and when.
RECEIPT_NAME = "migration.json"
#: An explicit portable install marker (a copied/extracted tree).
PORTABLE_MARKERS = ("portable.flag",)
#: Data directories beside the executable, as spelled by the two launchers.
BESIDE_APP_NAMES = ("data", "Data")
#: Temp-folder names older launches of this app used for their data. The first
#: is the convention, the second is the developer launch that started all this.
LEGACY_TEMP_NAMES = ("MathChecker", "mc5_ui")

#: Never copied into an adopted directory: the database is copied through
#: SQLite (below), and these are either per-process state or regenerable.
_SKIP_ENTRIES = frozenset({
    DB_NAME, "app.db-wal", "app.db-shm", "app.db-journal",
    "instance.json", RECEIPT_NAME, "tmp", "logs",
})


class AdoptionRefused(RuntimeError):
    """Base class for a refused adoption (nothing was copied)."""


class NoDataToAdopt(AdoptionRefused):
    """The source directory holds no ``app.db``."""


class AlreadyHasData(AdoptionRefused):
    """The destination already holds a database; it is never overwritten."""


@dataclass(frozen=True)
class Adoption:
    """What :func:`adopt` did, for the console line and the receipt."""

    source: Path
    target: Path
    files: int
    bytes: int

    def describe(self) -> str:
        return (f"adopted {self.files} file(s), {_human(self.bytes)} from "
                f"{self.source} -> {self.target}")


def app_dir() -> Path:
    """The application tree (the parent of ``backend/``), resolved from this file."""
    return Path(__file__).resolve().parent.parent.parent


def platform_name() -> str:
    """``windows`` / ``macos`` / ``linux`` - injectable so the rules are testable."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def per_user_data_dir(env: dict | None = None, platform: str | None = None) -> Path:
    """The stable per-user data directory for this operating system.

    Deliberately derived from environment variables that exist on the platform
    (``LOCALAPPDATA``, ``XDG_DATA_HOME``, ``HOME``) instead of a hard-coded
    profile path, so a redirected profile or a non-standard ``$HOME`` still
    lands somewhere writable. A missing variable falls back to the documented
    default rather than raising: this runs before any error reporting exists.
    """
    env = os.environ if env is None else env
    platform = platform or platform_name()
    home = _home(env, platform)

    if platform == "windows":
        base = env.get("LOCALAPPDATA") or env.get("APPDATA")
        root = Path(base) if base else home / "AppData" / "Local"
        return root / APP_DIR_NAME
    if platform == "macos":
        return home / "Library" / "Application Support" / APP_DIR_NAME
    base = env.get("XDG_DATA_HOME")
    root = Path(base) if base else home / ".local" / "share"
    return root / APP_DIR_NAME.lower()


def _home(env: dict, platform: str) -> Path:
    """The user's home directory, taken from the environment that was passed in.

    ``Path.home()`` reads the process environment, which would make the rules
    impossible to test and would ignore a redirected profile in a packaged
    launch. Windows answers ``USERPROFILE`` (what ``Path.home()`` itself uses);
    everything else answers ``HOME``.
    """
    if platform == "windows":
        raw = env.get("USERPROFILE") or env.get("HOME")
    else:
        raw = env.get("HOME")
    return Path(raw) if raw else Path.home()


def beside_app_data_dir(root: Path | None = None) -> Path:
    """``<app>/data`` (``Data`` on a case-sensitive filesystem, if that is what exists)."""
    root = Path(root) if root else app_dir()
    for name in BESIDE_APP_NAMES:
        candidate = root / name
        if candidate.exists() or name == BESIDE_APP_NAMES[0]:
            return candidate
    return root / BESIDE_APP_NAMES[0]


def has_data(path: Path | str) -> bool:
    """True when ``path`` holds a database (the only thing worth protecting)."""
    return (Path(path) / DB_NAME).is_file()


def is_checkout(root: Path | None = None) -> bool:
    """True for a source checkout (``.git`` beside ``main.py``)."""
    return (Path(root) if root else app_dir()).joinpath(".git").exists()


def is_portable(root: Path | None = None) -> bool:
    """True when this copy is portable or already keeps its data beside it.

    The second half matters as much as the first: an installation that has a
    database next to the executable predates this rule, and moving it would
    look exactly like "the app lost my classes".
    """
    root = Path(root) if root else app_dir()
    if any((root / marker).is_file() for marker in PORTABLE_MARKERS):
        return True
    return any(has_data(root / name) or (root / name / "config.toml").is_file()
               for name in BESIDE_APP_NAMES)


def resolve_data_dir(app_root: Path | str | None = None,
                     env: dict | None = None,
                     platform: str | None = None) -> Path:
    """The data directory for this process, following the precedence above."""
    root = Path(app_root) if app_root else app_dir()
    env = os.environ if env is None else env

    explicit = (env.get("MC5_DATA_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if is_portable(root) or is_checkout(root):
        return beside_app_data_dir(root)
    return per_user_data_dir(env, platform)


def legacy_candidates(target: Path | str | None = None,
                      app_root: Path | str | None = None,
                      env: dict | None = None) -> list[Path]:
    """Directories that may hold data from before the per-user default.

    Ordered most-likely-first: the old beside-the-app layout, then the system
    temp folder (where an older launch of this app could have been pointed).
    Only directories that actually contain a database are returned, and the
    target itself is never a candidate.
    """
    root = Path(app_root) if app_root else app_dir()
    env = os.environ if env is None else env
    resolved_target = Path(target) if target else resolve_data_dir(root, env)

    out: list[Path] = []
    for name in BESIDE_APP_NAMES:
        out.append(root / name)
    temp_root = Path(env.get("TEMP") or env.get("TMP") or tempfile.gettempdir())
    for name in LEGACY_TEMP_NAMES:
        out.append(temp_root / name)

    seen: set[str] = set()
    candidates: list[Path] = []
    for candidate in out:
        try:
            key = str(candidate.resolve())
            if key == str(resolved_target.resolve()) or key in seen:
                continue
        except OSError:
            key = str(candidate)
            if key in seen:
                continue
        seen.add(key)
        if has_data(candidate):
            candidates.append(candidate)
    return candidates


def adopt(source: Path | str, target: Path | str) -> Adoption:
    """Copy the data in ``source`` into ``target`` - never overwriting, never moving.

    The database is copied through SQLite's own backup API, which is the only
    copy that is consistent while the source is open (a plain file copy of a
    live WAL database can capture the database one checkpoint ahead of its
    ``-wal`` file). Everything else - config, secrets, mark schemes, retained
    submissions - is copied file by file.

    The copy is staged in a sibling directory and moved into place only once it
    is complete, so an interrupted adoption cannot leave a half-written
    database behind that the app would then open as real data.
    """
    src, dst = Path(source), Path(target)
    if not has_data(src):
        raise NoDataToAdopt(f"{src} does not contain {DB_NAME}")
    if has_data(dst):
        raise AlreadyHasData(f"{dst} already contains {DB_NAME}")

    _log.info("adopting legacy data %s -> %s", src, dst)
    staging = dst.parent / f".{dst.name}.adopting"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    files = 0
    total = 0
    try:
        for entry in sorted(src.iterdir(), key=lambda p: p.name):
            if entry.name in _SKIP_ENTRIES:
                continue
            destination = staging / entry.name
            if entry.is_dir():
                shutil.copytree(entry, destination, dirs_exist_ok=True)
                for copied in destination.rglob("*"):
                    if copied.is_file():
                        files += 1
                        total += copied.stat().st_size
            elif entry.is_file():
                shutil.copy2(entry, destination)
                files += 1
                total += entry.stat().st_size

        _copy_database(src / DB_NAME, staging / DB_NAME)

        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(staging.iterdir(), key=lambda p: p.name):
            shutil.move(str(child), str(dst / child.name))
        staging.rmdir()
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    receipt = {
        "adopted_from": str(src),
        "adopted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": files,
        "bytes": total,
    }
    try:
        (dst / RECEIPT_NAME).write_text(json.dumps(receipt, indent=2) + "\n",
                                        encoding="utf-8")
    except OSError:
        # A missing receipt costs a line of explanation on the next run, never
        # the data itself.
        pass
    return Adoption(source=src, target=dst, files=files, bytes=total)


def auto_adopt(target: Path | str | None = None,
               app_root: Path | str | None = None,
               env: dict | None = None) -> Adoption | None:
    """Adopt legacy data into an empty target; return what happened (or ``None``).

    Three refusals, all of them on purpose:

    * ``MC5_DATA_DIR`` is set - an explicit location is a decision, not a
      default to be "corrected";
    * the target already has a database - never merge two data sets;
    * no legacy directory has a database - a fresh install stays fresh.

    A source that cannot be read (locked file, permissions) is skipped rather
    than failing the launch: starting with an empty folder is recoverable,
    refusing to start is not.
    """
    env = os.environ if env is None else env
    if (env.get("MC5_DATA_DIR") or "").strip():
        return None
    resolved = Path(target) if target else resolve_data_dir(app_root, env)
    if has_data(resolved):
        return None
    for source in legacy_candidates(resolved, app_root, env):
        try:
            return adopt(source, resolved)
        except AdoptionRefused:
            continue
        except (OSError, sqlite3.Error) as exc:
            _log.warning("could not adopt data from %s: %s", source, exc)
            continue
    return None


def _copy_database(source: Path, target: Path) -> None:
    """Consistent copy of a possibly-open SQLite database (read-only on the source)."""
    uri = "file:" + quote(source.resolve().as_posix(), safe="/:") + "?mode=ro"
    reader = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        writer = sqlite3.connect(str(target), timeout=30)
        try:
            reader.backup(writer)
            writer.commit()
        finally:
            writer.close()
    finally:
        reader.close()


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
