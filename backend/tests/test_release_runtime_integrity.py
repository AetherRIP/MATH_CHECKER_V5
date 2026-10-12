"""A shared build must actually run on the recipient's machine (§14, §24).

The bundled Windows runtime is what lets a teacher run Math Checker on a PC with
no Python installed. Its compiled modules are ``*.pyd`` files — and a common
``*.py[cod]`` .gitignore rule matches ``*.pyd`` as well, so every one of them was
silently left out of this repository while the runtime still looked complete
(stdlib zip, ``Lib/site-packages``, the DLLs, ``python.exe`` — all present).

A runtime in that state cannot import ``socket``/``sqlite3``/``ssl``, so the app
cannot start at all: no port to listen on, no database, no Google/Gemini call,
no PDF extraction. These tests lock the checks that refuse to BUILD such a
runtime, that refuse to VERIFY such a package, and the `.gitignore` negation
that keeps the .pyd files tracked.
"""

from __future__ import annotations

import fnmatch
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "MathChecker_Distribution"

#: One file per entry in build_release.RUNTIME_EXTENSION_MODULES, using the
#: Windows naming of a real 3.13 runtime.
COMPLETE_RUNTIME = (
    "_socket.pyd",
    "_ssl.pyd",
    "_sqlite3.pyd",
    "_ctypes.pyd",
    "_hashlib.pyd",
    "Lib/site-packages/PIL/_imaging.cp313-win_amd64.pyd",
    "Lib/site-packages/pymupdf/_mupdf.pyd",
    "Lib/site-packages/pydantic_core/_pydantic_core.cp313-win_amd64.pyd",
    "Lib/site-packages/cryptography/hazmat/bindings/_rust.pyd",
)


@pytest.fixture(scope="module")
def build_release_module():
    sys.path.insert(0, str(DIST / "scripts"))
    try:
        import build_release
        yield build_release
    finally:
        sys.path.pop(0)
        sys.modules.pop("build_release", None)


def make_runtime(tmp_path: Path, files=COMPLETE_RUNTIME) -> Path:
    root = tmp_path / "runtime"
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"MZ" + b"\x00" * 16)
    return root


def test_complete_runtime_reports_nothing_missing(build_release_module, tmp_path):
    missing = build_release_module.check_runtime_extensions(make_runtime(tmp_path))
    assert missing == []


def test_runtime_stripped_of_pyd_files_is_refused(build_release_module, tmp_path):
    """The exact repository defect: the runtime, minus every compiled module."""
    runtime = make_runtime(tmp_path)
    for pyd in runtime.rglob("*.pyd"):
        pyd.unlink()
    # Keep the *appearance* of a runtime: a .pyd-free copy still has .py files.
    (runtime / "Lib" / "site-packages" / "pymupdf" / "__init__.py").write_text("")
    missing = build_release_module.check_runtime_extensions(runtime)
    assert len(missing) == len(build_release_module.RUNTIME_EXTENSION_MODULES)
    joined = " ".join(missing)
    for expected in ("_socket", "_sqlite3", "_ssl", "_mupdf", "_imaging"):
        assert expected in joined


def test_posix_runtime_extensions_are_recognised(build_release_module, tmp_path):
    """A .so in lib-dynload counts, so the check is not Windows-only."""
    root = tmp_path / "runtime"
    dynload = root / "lib" / "python3.12" / "lib-dynload"
    dynload.mkdir(parents=True)
    for stem in ("_socket", "_ssl", "_sqlite3", "_ctypes", "_hashlib"):
        (dynload / f"{stem}.cpython-312-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    site = root / "lib" / "python3.12" / "site-packages"
    for folder, stem in (("PIL", "_imaging"), ("pymupdf", "_mupdf"),
                         ("pydantic_core", "_pydantic_core"),
                         ("cryptography/hazmat/bindings", "_rust")):
        (site / folder).mkdir(parents=True, exist_ok=True)
        (site / folder / f"{stem}.so").write_bytes(b"\x7fELF")
    assert build_release_module.check_runtime_extensions(root) == []


def test_stdlib_inside_the_runtime_zip_counts_as_present(build_release_module, tmp_path):
    """A Windows embeddable runtime keeps its stdlib in python3*.zip (.pyc)."""
    root = make_runtime(tmp_path)
    with zipfile.ZipFile(root / "python313.zip", "w") as zf:
        zf.writestr("ssl.pyc", b"")
        zf.writestr("sqlite3/__init__.pyc", b"")
    assert build_release_module._present_in_runtime(root, "ssl") is True
    assert build_release_module._present_in_runtime(root, "sqlite3") is True
    # ...and a module that is not there is still reported.
    assert build_release_module._present_in_runtime(root, "definitely_missing") is False


def test_static_report_accepts_the_complete_embeddable_layout(build_release_module, tmp_path):
    """The on-disk report must not call a complete runtime incomplete.

    Every module in RUNTIME_IMPORTS is either a package directory, a .pyd next
    to the interpreter, or stdlib bytecode in the zip.
    """
    root = make_runtime(tmp_path)
    with zipfile.ZipFile(root / "python313.zip", "w") as zf:
        zf.writestr("ssl.pyc", b"")
        zf.writestr("sqlite3/__init__.pyc", b"")
    for name in build_release_module.RUNTIME_IMPORTS:
        if name in ("sqlite3", "ssl"):
            continue
        top = name.split(".")[0]
        folder = root / "Lib" / "site-packages" / name.replace(".", "/")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").write_text("")
        if "." not in name:
            (root / "Lib" / "site-packages" / f"{top}.py").write_text("")
    missing, _versions = build_release_module._static_runtime_report(root)
    assert missing == []


@pytest.mark.parametrize("folder", ["build", "portable"])
def test_the_repository_runtime_is_not_missing_its_compiled_modules(folder):
    """Directly covers the shipped tree: what is in git must be runnable.

    ``build/runtime`` is the pipeline input and ``portable/MathChecker_Portable``
    is the folder handed to a recipient, so both must contain the .pyd files.
    """
    sys.path.insert(0, str(DIST / "scripts"))
    try:
        import build_release
        runtimes = []
        if folder == "build":
            runtimes = [DIST / "build" / "runtime"]
        else:
            runtimes = [p / "runtime"
                        for p in (DIST / "portable").glob("*/")]
        present = [r for r in runtimes if r.is_dir()]
        if not present:
            pytest.skip(f"no bundled runtime under {folder}/ in this checkout")
        for runtime in present:
            assert build_release.check_runtime_extensions(runtime) == [], runtime
    finally:
        sys.path.pop(0)
        sys.modules.pop("build_release", None)


def test_gitignore_keeps_the_negation_that_pyd_files_need():
    """`*.py[cod]` matches `*.pyd`, so the negation is what keeps them tracked."""
    # The trap, spelled out - if this ever becomes False the trap is gone and
    # the negation below is no longer load-bearing.
    assert fnmatch.fnmatch("_ssl.pyd", "*.py[cod]") is True

    patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    negation = [p for p in patterns if p.strip() == "!MathChecker_Distribution/**/*.pyd"]
    assert negation, "the .pyd negation was removed from .gitignore"
    # It must come AFTER the rule it neutralises (git is last-match-wins).
    index_of_trap = next(i for i, p in enumerate(patterns) if p.strip() == "*.py[cod]")
    index_of_negation = patterns.index(negation[0])
    assert index_of_negation > index_of_trap


def test_bundled_tunnel_helper_keeps_its_windows_name(build_release_module,
                                                      tmp_path, monkeypatch):
    """The package targets Windows, so the helper must be `cloudflared.exe`.

    Naming it after the BUILD machine's platform produced a package containing a
    54 MB helper that ``public_access.find_cloudflared`` could never find.
    """
    fake = tmp_path / "cloudflared.exe"
    fake.write_bytes(b"MZ" + b"\x00" * 32)
    monkeypatch.setattr(build_release_module, "DIST", tmp_path / "dist")
    monkeypatch.setattr(build_release_module, "STAGING", tmp_path / "staging")
    monkeypatch.setattr(build_release_module.shutil, "which",
                        lambda name: str(fake) if "cloudflared" in name else None)
    assert build_release_module.bundle_cloudflared() == "tools/cloudflared.exe"
    assert (tmp_path / "staging" / "tools" / "cloudflared.exe").is_file()


def test_a_non_windows_tunnel_helper_is_not_bundled(build_release_module,
                                                    tmp_path, monkeypatch):
    fake = tmp_path / "cloudflared"
    fake.write_bytes(b"\x7fELF")
    monkeypatch.setattr(build_release_module, "DIST", tmp_path / "dist")
    monkeypatch.setattr(build_release_module, "STAGING", tmp_path / "staging")
    monkeypatch.setattr(build_release_module.shutil, "which",
                        lambda name: str(fake) if "cloudflared" in name else None)
    assert build_release_module.bundle_cloudflared() is None
    assert not (tmp_path / "staging" / "tools").exists()
