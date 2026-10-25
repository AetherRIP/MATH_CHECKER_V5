"""Regression tests for the failure a simultaneous batch exposed.

Eight corrections of ONE paper started together. They share a single
Files-API upload (the registry is keyed by key + content SHA), and the first
job to finish deleted that upload in its terminal cleanup. The jobs still
marking then got ``403 PERMISSION_DENIED ... or it may not exist`` and one
healthy correction failed outright (run 2 of the eight-run batch).

Two independent protections are asserted here:

1. terminal cleanup keeps a provider file while another ACTIVE job holds it,
2. a file-permission 403 on a generate call is repaired by re-uploading the
   inputs and retried once, instead of failing the job and cooling the key.
"""

from __future__ import annotations

import time
import types
from pathlib import Path

import pytest

from app import db
from app.config import get_config, get_secrets, save_config
from app.gemini import file_registry
from app.gemini.client_proxy import KeyAwareClient
from app.gemini.key_manager import KeyManager, fingerprint

KEYS = ["shared-upload-key-one-0123456789"]


# ------------------------------------------------------------------ fakes

class _Remote:
    def __init__(self, name: str):
        self.name = name
        self.state = types.SimpleNamespace(name="ACTIVE")


class _Files:
    def __init__(self, world: dict):
        self._world = world

    def upload(self, *, file: str):
        self._world["uploads"].append(str(file))
        name = f"files/shared/{len(self._world['uploads'])}"
        self._world["live"].add(name)
        return _Remote(name)

    def get(self, *, name: str):
        if name not in self._world["live"]:
            raise RuntimeError("404 NOT_FOUND")
        return _Remote(name)

    def delete(self, *, name: str):
        self._world["deletes"].append(name)
        self._world["live"].discard(name)


class _Models:
    def __init__(self, world: dict):
        self._world = world

    def generate_content(self, *, model, contents, config=None, **kwargs):
        self._world["calls"].append(model)
        if self._world.get("deny"):
            raise RuntimeError(
                "403 PERMISSION_DENIED: no permission for the File x")
        items = contents if isinstance(contents, (list, tuple)) else [contents]
        for item in items:
            name = getattr(item, "name", None)
            if name and name not in self._world["live"]:
                raise RuntimeError(
                    "403 PERMISSION_DENIED: You do not have permission to "
                    f"access the File {name} or it may not exist.")
        return types.SimpleNamespace(text="marked")


class _SDK:
    def __init__(self, world: dict):
        self.files = _Files(world)
        self.models = _Models(world)


@pytest.fixture
def world(fresh_state, monkeypatch):
    cfg = get_config()
    cfg.mock_mode = False
    save_config(cfg)
    get_secrets().set_gemini_keys(KEYS)
    world = {"uploads": [], "deletes": [], "live": set(), "calls": []}
    monkeypatch.setattr(KeyAwareClient, "_sdk_client_for_key",
                        lambda self, key: _SDK(world))
    return world


def _client(job_id: str = "job-shared") -> KeyAwareClient:
    return KeyAwareClient(KeyManager(), preferred_fp=fingerprint(KEYS[0]),
                          job_id=job_id)


def _pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 4096)
    return path


def _job(job_id: str, status: str) -> None:
    now = time.time()
    db.execute(
        "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
        "VALUES(?,?,?,?)", (job_id, status, now, now))


# ------------------------------------------------- 1. registry helper

def test_file_held_by_another_active_job(world):
    _job("job-active", "MARKING")
    _job("job-done", "COMPLETED")
    file_registry.record_job_file("job-active", fingerprint(KEYS[0]),
                                  "files/shared/1", "submission")
    file_registry.record_job_file("job-done", fingerprint(KEYS[0]),
                                  "files/shared/1", "submission")

    assert file_registry.remote_held_by_other_active_jobs(
        "files/shared/1", "job-done", ("MARKING",))
    # The job that is still marking is not blocked by anyone.
    assert not file_registry.remote_held_by_other_active_jobs(
        "files/shared/1", "job-active", ("MARKING",))
    # A terminal sibling is not a reason to keep the file.
    assert not file_registry.remote_held_by_other_active_jobs(
        "files/shared/1", "job-done", ("COMPLETED",))


# ------------------------------------------------- 2. terminal cleanup

def test_cleanup_keeps_file_while_another_job_is_running(world, monkeypatch):
    from app.engine.service import cleanup_job_files

    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(KeyAwareClient, "delete_remote_for_key",
                        lambda self, fp, name: deleted.append((fp, name)))
    _job("job-first", "COMPLETED")
    _job("job-second", "MARKING")
    for job_id in ("job-first", "job-second"):
        file_registry.record_job_file(job_id, fingerprint(KEYS[0]),
                                      "files/shared/1", "submission")

    cleanup_job_files("job-first")
    assert deleted == [], "a running sibling still needs that upload"
    # The finished job's own claim is released even though the file stays.
    assert file_registry.job_files("job-first") == []
    assert file_registry.job_files("job-second"), "the live claim must survive"

    # The last holder does delete it.
    cleanup_job_files("job-second")
    assert deleted == [(fingerprint(KEYS[0]), "files/shared/1")]


# --------------------------------------- 3. a 403 on a missing file self-heals

def test_permission_denied_on_missing_file_re_uploads_and_retries(world,
                                                                  tmp_path):
    submission = _pdf(tmp_path / "work.pdf")
    client = _client()
    remote = client.upload_file(str(submission), purpose="submission")
    assert len(world["uploads"]) == 1

    # The upload vanishes provider-side (a sibling job's cleanup, or expiry).
    world["live"].discard(remote.name)

    response = client.generate_content(
        model="gemini-3.6-flash", contents=[remote, "marks, please"])

    assert response.text == "marked"
    assert len(world["uploads"]) == 2, "the file must be re-uploaded"
    assert len(world["calls"]) == 2, "one failed call, one repaired call"


def test_permission_denied_without_a_retry_loop(world, tmp_path):
    """A second file-permission failure is real and must not loop forever."""
    submission = _pdf(tmp_path / "work.pdf")
    client = _client()
    remote = client.upload_file(str(submission), purpose="submission")
    world["live"].discard(remote.name)

    world["deny"] = True          # every attempt is refused, repaired or not

    with pytest.raises(Exception):
        client.generate_content(model="gemini-3.6-flash", contents=[remote])

    assert len(world["calls"]) == 2, "exactly one repair attempt"
