"""Phase 2's critical invariant: peer networking cannot reach marking.

A separate process is the mechanism; these tests are the proof that the mechanism
holds in the source as well. If any marking, correction, scheme, reporting or job
module imports the peer package, the peer layer stops being additive and a peer
bug could change a mark (plan §10.4, R-10).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

#: Every subtree that decides, performs or reports a correction.
MARKING_PATHS = ("engine", "automation", "reporting", "classroom", "jobs",
                 "workers", "diff", "benchmark")


def _python_files(relative: str) -> list[Path]:
    root = APP / relative
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                     filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("subtree", MARKING_PATHS)
def test_marking_subtrees_do_not_import_the_peer_layer(subtree):
    if not (APP / subtree).exists():            # pragma: no cover - layout guard
        pytest.skip(f"{subtree} is not part of this tree")
    offenders = []
    for path in _python_files(subtree):
        for module in _imports(path):
            if module == "app.peer" or module.startswith("app.peer."):
                offenders.append(f"{path.relative_to(BACKEND)} -> {module}")
            if ".peer" in module and module.startswith("app"):
                offenders.append(f"{path.relative_to(BACKEND)} -> {module}")
    assert offenders == [], (
        "the peer layer must stay out of the marking path:\n" + "\n".join(offenders))


def test_the_peer_layer_does_not_import_marking_internals():
    offenders = []
    for path in sorted((APP / "peer").rglob("*.py")):
        for module in _imports(path):
            if any(f"app.{name}" == module or module.startswith(f"app.{name}.")
                   for name in MARKING_PATHS):
                offenders.append(f"{path.relative_to(BACKEND)} -> {module}")
            if module.startswith("..") or module.startswith(".engine"):
                offenders.append(f"{path.relative_to(BACKEND)} -> {module}")
    assert offenders == [], "\n".join(offenders)


def test_importing_the_peer_service_starts_nothing(tmp_path, monkeypatch):
    """Import must be inert: no process, no state file, no directory."""
    monkeypatch.setenv("MC5_DATA_DIR", str(tmp_path))
    import importlib

    module = importlib.import_module("app.peer.service")
    importlib.reload(module)

    service = module.PeerService(directory=tmp_path)
    assert service.status.state == module.STOPPED
    assert service.status.pid is None
    # Constructing and inspecting a supervisor writes nothing at all.
    assert not (tmp_path / "credentials.json").exists()
    assert not (tmp_path / "service.lock").exists()
    assert not (tmp_path / "peer_state.json").exists()


def test_a_disabled_switch_leaves_no_peer_state_behind(tmp_path):
    from app.peer import control, service

    status = service.PeerService(directory=tmp_path).start()
    assert status.state == service.DISABLED
    # Only the (absent) preference is read: nothing is created for a feature
    # nobody switched on.
    assert control.is_enabled(tmp_path) is False
    assert not (tmp_path / "peer_state.json").exists()
    assert not (tmp_path / "service.lock").exists()
