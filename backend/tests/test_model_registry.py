"""The model registry must never confuse "listed" with "usable".

Every test here is about a refusal or a distinction, because those are the parts
that protect production marking: a model is not approved because it is new,
cheap, fast, or present in a listing, and nothing is promoted without evidence.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_registry():
    from app.gemini import model_registry

    model_registry.reset()
    yield
    model_registry.reset()


# -- discovery is not availability -------------------------------------------

def test_a_listed_model_is_not_a_verified_model():
    from app.gemini import model_registry

    result = model_registry.discover([
        {"name": "models/gemini-test-flash", "display_name": "Test Flash",
         "input_token_limit": 1_000_000, "supported_actions": ["generateContent"]},
    ])

    assert result["count"] == 1
    record = model_registry.records()["gemini-test-flash"]
    assert record.discovered is True
    assert record.verified is False
    assert record.as_dict()["availability"] == "listed_not_verified"
    assert record.input_token_limit == 1_000_000


def test_only_a_successful_request_verifies_a_model():
    from app.gemini import model_registry

    model_registry.discover([{"name": "gemini-test-flash"}])
    model_registry.note_request("gemini-test-flash", ok=False, error="503 busy")
    assert model_registry.records()["gemini-test-flash"].verified is False

    model_registry.note_request("gemini-test-flash", ok=True)
    record = model_registry.records()["gemini-test-flash"]
    assert record.verified is True
    assert record.as_dict()["availability"] == "verified"
    assert record.requests == 2 and record.failures == 1
    assert record.error_rate == 0.5


def test_a_failed_discovery_keeps_what_is_already_known():
    from app.gemini import model_registry

    model_registry.discover([{"name": "gemini-test-flash"}])
    model_registry.note_request("gemini-test-flash", ok=True)

    class _Broken:
        @property
        def models(self):
            raise RuntimeError("no network")

    result = model_registry.discover_live(client=_Broken())
    assert result["ok"] is False
    assert result["detail"] == "previous verified state kept"
    assert model_registry.records()["gemini-test-flash"].verified is True


def test_model_ids_are_normalised_so_one_model_is_one_record():
    from app.gemini import model_registry

    model_registry.discover([{"name": "models/Gemini-Test-Flash"},
                             {"name": "gemini-test-flash"}])
    assert list(model_registry.records()) == ["gemini-test-flash"]


# -- approval refusals --------------------------------------------------------

def test_an_unverifiable_model_cannot_be_approved():
    from app.gemini import model_registry

    model_registry.discover([{"name": "gemini-test-flash"}])
    with pytest.raises(model_registry.PromotionRefused) as excinfo:
        model_registry.promote("gemini-test-flash", actor="teacher",
                               evidence={"certified": True, "run_ids": ["r1"]})
    assert "never served a successful request" in str(excinfo.value)


def test_production_approval_requires_certified_evidence():
    from app.gemini import model_registry

    model_registry.discover([{"name": "gemini-test-flash"}])
    model_registry.note_request("gemini-test-flash", ok=True)

    with pytest.raises(model_registry.PromotionRefused) as excinfo:
        model_registry.promote("gemini-test-flash", actor="teacher",
                               evidence={"certified": False})
    assert "teacher-certified" in str(excinfo.value)

    with pytest.raises(model_registry.PromotionRefused) as excinfo:
        model_registry.promote("gemini-test-flash", actor="teacher",
                               evidence={"certified": True})
    assert "run ids" in str(excinfo.value)


def test_an_unattributed_approval_is_refused():
    from app.gemini import model_registry

    model_registry.note_request("gemini-test-flash", ok=True)
    with pytest.raises(model_registry.PromotionRefused):
        model_registry.promote("gemini-test-flash", actor="  ",
                               evidence={"certified": True, "run_ids": ["r"]})


def test_low_risk_approval_does_not_require_certified_evidence():
    from app.gemini import model_registry

    model_registry.note_request("gemini-test-flash", ok=True)
    decision = model_registry.promote(
        "gemini-test-flash", role=model_registry.ROLE_LOW_RISK,
        actor="teacher", evidence={"note": "extraction only"})
    assert decision["role"] == "low_risk"
    record = model_registry.records()["gemini-test-flash"]
    assert record.approved_for_low_risk is True
    assert record.approved_for_production is False


# -- approval is explicit and reversible --------------------------------------

def test_recording_a_decision_does_not_change_production():
    from app.config import get_config
    from app.gemini import model_registry

    model_registry.note_request("gemini-test-flash", ok=True)
    before = list(get_config().gemini.primary_models or [])

    decision = model_registry.promote(
        "gemini-test-flash", actor="teacher",
        evidence={"certified": True, "run_ids": ["run-1"]}, apply=False)

    assert decision["applied"] is False
    assert decision["previous_models"] == before
    # apply=False must leave what marks student work completely alone.
    assert list(get_config().gemini.primary_models or []) == before


def test_applying_a_promotion_is_reversible_by_rollback():
    from app.config import get_config
    from app.gemini import model_registry

    before = list(get_config().gemini.primary_models or [])
    model_registry.note_request("gemini-test-flash", ok=True)

    model_registry.promote("gemini-test-flash", actor="teacher",
                           evidence={"certified": True, "run_ids": ["run-1"]},
                           apply=True)
    assert get_config().gemini.primary_models == ["gemini-test-flash"]

    result = model_registry.rollback(actor="teacher")
    assert result["restored_models"] == before
    assert list(get_config().gemini.primary_models or []) == before


def test_rollback_without_a_promotion_is_refused():
    from app.gemini import model_registry

    with pytest.raises(model_registry.PromotionRefused):
        model_registry.rollback(actor="teacher")


# -- the report ---------------------------------------------------------------

def test_the_snapshot_separates_configured_discovered_and_verified():
    from app.gemini import model_registry

    model_registry.discover([{"name": "gemini-listed-only"},
                             {"name": "gemini-verified"}])
    model_registry.note_request("gemini-verified", ok=True)

    snapshot = model_registry.snapshot()
    by_id = {m["model_id"]: m for m in snapshot["models"]}
    assert by_id["gemini-listed-only"]["availability"] == "listed_not_verified"
    assert by_id["gemini-verified"]["availability"] == "verified"
    assert snapshot["verified_count"] == 1
    assert snapshot["discovered_count"] == 2
    assert snapshot["notes"], "the report must state what its flags mean"
