"""Gemini model migration: deprecated IDs must never be retried as fallbacks.

Google still ACCEPTS ``gemini-3.5-flash`` and ``gemini-3.7-flash`` but serves
them with ``gemini-3.6-flash`` and ``gemini-3.8-flash`` respectively (verified
against ai.google.dev/gemini-api/docs/deprecations, 2026-10-24). Keeping an
alias in the fallback chain means that after the replacement model has failed,
the next entry sends the whole submission + mark scheme to the SAME model
again — one wasted request per alias, and a result labelled with a model that
never ran.

These tests pin the two behaviours that prevent that: the chain is normalised
on load, and the engine's fallback walk skips aliases even when a caller
passes a raw list.
"""

from __future__ import annotations

import pytest


DEPRECATED = ("gemini-3.5-flash", "gemini-3.7-flash")


def test_default_chain_uses_only_models_google_currently_serves():
    from app import config as cfg

    assert cfg.DEFAULT_PRIMARY_MODELS == (
        "gemini-3.8-flash", "gemini-3.6-flash")
    assert cfg.DEFAULT_AUDIT_MODELS == (
        "gemini-3.8-flash", "gemini-3.6-flash")
    # A lightweight model is not a trustworthy marking model.
    assert not cfg.is_trusted_marking_model("gemini-3.5-flash-lite")
    assert cfg.is_trusted_marking_model("gemini-3.6-flash")
    for model in DEPRECATED:
        assert model not in cfg.DEFAULT_PRIMARY_MODELS
        assert model not in cfg.DEFAULT_AUDIT_MODELS
    assert not (set(cfg.DEFAULT_AUDIT_MODELS) & set(DEPRECATED))


def test_normalize_collapses_aliases_to_the_served_model_and_dedupes():
    from app.config import normalize_model_chain

    # The pre-migration chain, in its historical order.
    chain = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
             "gemini-3.5-flash", "gemini-3.5-flash-lite"]

    assert normalize_model_chain(chain) == [
        "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]


def test_normalize_keeps_unknown_models_and_blank_entries_are_dropped():
    from app.config import normalize_model_chain

    assert normalize_model_chain(
        ["  gemini-3.6-flash ", "", "  ", "gemini-3.1-pro-preview"]) == [
        "gemini-3.6-flash", "gemini-3.1-pro-preview"]


def test_an_existing_config_file_with_deprecated_ids_is_normalised_on_load(
        tmp_path, monkeypatch):
    from app import config as cfg

    file = tmp_path / "config.toml"
    text = ('[gemini]\n'
            'primary_models = ["gemini-3.8-flash", "gemini-3.7-flash", '
            '"gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]\n'
            'audit_models = ["gemini-3.7-flash", "gemini-3.6-flash", '
            '"gemini-3.5-flash-lite"]\n')
    file.write_text(text, encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", file)
    monkeypatch.setattr(cfg, "_config", None)

    loaded = cfg.get_config(reload=True)

    assert loaded.gemini.primary_models == [
        "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    assert loaded.gemini.audit_models == [
        "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
    # Opening the app must not rewrite the teacher's file.
    assert file.read_text(encoding="utf-8") == text


class _FailingClient:
    def __init__(self, error: Exception):
        self.error = error
        self.calls: list[str] = []

        outer = self

        class _Models:
            def generate_content(self, *, model, contents, config=None, **kw):
                outer.calls.append(model)
                raise outer.error

        self.models = _Models()


def test_engine_never_sends_a_second_request_to_an_alias_of_a_failed_model():
    """3.6 fails → the old chain would try 3.5, which Google serves as 3.6."""
    from app import config as cfg
    from app.engine import legacy_marking as legacy

    monkeypatch_target = cfg.get_config().gemini
    original = list(monkeypatch_target.primary_models)
    legacy.MODEL_HEALTH.reset()
    try:
        monkeypatch_target.primary_models = [
            "gemini-3.6-flash", "gemini-3.5-flash"]
        client = _FailingClient(RuntimeError("timed out"))

        with pytest.raises(RuntimeError) as raised:
            legacy.grade_with_model(
                client=client,
                model_names=monkeypatch_target.primary_models,
                student_file="student",
                mark_scheme_file="scheme",
            )

        assert client.calls == ["gemini-3.6-flash"]
        assert "gemini-3.5-flash" not in str(raised.value)
        # The alias must not be benched as if it were a separate model.
        assert "gemini-3.5-flash" not in legacy.MODEL_HEALTH.benched()
    finally:
        monkeypatch_target.primary_models = original
        legacy.MODEL_HEALTH.reset()
