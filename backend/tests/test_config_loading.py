"""Configuration loading: unreadable files must be REPORTED, never silent.

A damaged ``config.toml`` or ``secrets.toml`` used to fall back to defaults with
no trace at all, which made a corrupt secret store look exactly like "no Gemini
keys configured" — sending a teacher to re-enter keys that were already there.
"""
from __future__ import annotations

import logging

import pytest


def test_malformed_toml_is_reported_and_never_leaks_its_contents(tmp_path, caplog):
    from app import config as cfg

    secret = "AIza" + "A" * 35
    bad = tmp_path / "secrets.toml"
    # Deliberately invalid TOML that still CONTAINS credential-looking material.
    bad.write_text(f'gemini = {{ api_keys = ["{secret}"]\n', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="app.config"):
        assert cfg._read_toml(bad) == {}

    messages = " | ".join(r.getMessage() for r in caplog.records
                          if r.name == "app.config")
    assert "secrets.toml" in messages, "the damaged file must be named in the log"
    assert secret not in messages, "credential material must never be logged"
    assert "AIza" not in messages


def test_unreadable_config_does_not_stop_configuration_loading(tmp_path,
                                                               monkeypatch):
    """A broken file degrades to defaults instead of taking the app down."""
    from app import config as cfg

    broken = tmp_path / "config.toml"
    broken.write_text("this is not = = toml\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", broken)
    monkeypatch.setattr(cfg, "_config", None)

    loaded = cfg.get_config(reload=True)
    # Defaults, not a crash: the app still starts and a teacher can fix the file.
    assert loaded.worker.concurrency == 2
    assert loaded.network.access_mode == "local"
    assert loaded.security.session_hours == 12
    assert loaded.mock_mode is False


def test_valid_config_is_still_read(tmp_path):
    from app import config as cfg

    good = tmp_path / "config.toml"
    good.write_text('[network]\naccess_mode = "lan"\nport = 9100\n',
                    encoding="utf-8")
    assert cfg._read_toml(good)["network"]["port"] == 9100


def test_obsolete_large_pdf_settings_are_ignored_without_rewriting(tmp_path,
                                                                  monkeypatch):
    """An existing install may still have a ``[large_pdf]`` table.

    Loading must succeed, the other settings must still apply, and merely
    opening the app must not rewrite the user's file.
    """
    from app import config as cfg

    legacy = tmp_path / "config.toml"
    text = ('[network]\naccess_mode = "lan"\nport = 9123\n\n'
            '[large_pdf]\nenabled = true\nthreshold_pages = 1\n'
            'target_min = 2\ntarget_max = 4\n')
    legacy.write_text(text, encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", legacy)
    monkeypatch.setattr(cfg, "_config", None)

    loaded = cfg.get_config(reload=True)

    assert loaded.network.access_mode == "lan"
    assert loaded.network.port == 9123
    assert not hasattr(loaded, "large_pdf")
    assert legacy.read_text(encoding="utf-8") == text



def test_data_dir_is_resolved_on_every_call(fresh_state, monkeypatch, tmp_path):
    """One live accessor: no caller may freeze the data root at import time.

    Tests, ``MC5_DATA_DIR`` overrides and a reload all rebind ``config.DATA_DIR``.
    A frozen copy silently writes (or deletes) in the wrong directory, which is
    exactly the class of bug the per-router ``_data_dir()`` copies prevented.
    """
    from app import config as cfg

    assert cfg.data_dir() == fresh_state
    other = tmp_path / "relocated"
    monkeypatch.setattr(cfg, "DATA_DIR", other)
    assert cfg.data_dir() == other
