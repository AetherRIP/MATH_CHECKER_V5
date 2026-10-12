"""Model-chain stability tests: the marking model must not be a per-job lottery."""

from app.engine import legacy_marking as legacy
from app.engine import model_health_store as mhs


CHAIN = ["m1", "m2", "m3"]


class TestOrdering:
    def setup_method(self):
        legacy.MODEL_HEALTH.reset()

    def teardown_method(self):
        legacy.MODEL_HEALTH.reset()

    def test_no_pin_keeps_configured_order(self, fresh_state):
        assert mhs.ordered_models(CHAIN) == CHAIN

    def test_pin_moves_to_front(self, fresh_state):
        fresh_state  # data dir exists
        from app import db
        db.set_meta("model_health_v1", '{"preferred": "m2"}')
        assert mhs.ordered_models(CHAIN) == ["m2", "m1", "m3"]

    def test_pin_outside_chain_is_ignored(self, fresh_state):
        from app import db
        db.set_meta("model_health_v1", '{"preferred": "other"}')
        assert mhs.ordered_models(CHAIN) == CHAIN


class TestPinning:
    def setup_method(self):
        legacy.MODEL_HEALTH.reset()

    def teardown_method(self):
        legacy.MODEL_HEALTH.reset()

    def test_first_success_pins(self, fresh_state):
        mhs.record_success("m2", CHAIN)
        assert mhs.preferred_model() == "m2"

    def test_transient_failure_does_not_move_pin(self, fresh_state):
        mhs.record_success("m2", CHAIN)
        # a 503 benches m2 temporarily and m3 ends up marking instead
        legacy.MODEL_HEALTH.record_failure("m2", Exception("503 unavailable"))
        mhs.record_success("m3", CHAIN)
        assert mhs.preferred_model() == "m2"      # pin stays put
        assert mhs.ordered_models(CHAIN)[0] == "m2"

    def test_permanent_death_moves_pin(self, fresh_state):
        mhs.record_success("m2", CHAIN)
        legacy.MODEL_HEALTH.record_failure(
            "m2", Exception("404 model m2 not_found"))
        mhs.record_success("m3", CHAIN)
        assert mhs.preferred_model() == "m3"

    def test_model_removed_from_chain_moves_pin(self, fresh_state):
        mhs.record_success("m2", CHAIN)
        mhs.record_success("m3", ["m1", "m3"])    # m2 no longer configured
        assert mhs.preferred_model() == "m3"


class TestBenchPersistence:
    def setup_method(self):
        legacy.MODEL_HEALTH.reset()

    def teardown_method(self):
        legacy.MODEL_HEALTH.reset()

    def test_snapshot_restore_roundtrip(self, fresh_state):
        legacy.MODEL_HEALTH.record_failure("m2", Exception("404 model m2 not_found"))
        mhs.save()
        legacy.MODEL_HEALTH.reset()
        assert legacy.MODEL_HEALTH.benched() == {}
        mhs.load()
        benched = legacy.MODEL_HEALTH.benched()
        assert "m2" in benched
        assert benched["m2"][0] == "unavailable"

    def test_temporary_bench_expires_naturally(self, fresh_state):
        legacy.MODEL_HEALTH.record_failure("m2", Exception("503 unavailable"))
        mhs.save()
        mhs.load()
        benched = legacy.MODEL_HEALTH.benched()
        assert "m2" in benched and benched["m2"][0] == "busy/slow"

    def test_corrupt_state_is_ignored(self, fresh_state):
        from app import db
        db.set_meta("model_health_v1", "not-json{")
        mhs.load()          # must not raise
        assert legacy.MODEL_HEALTH.benched() == {}
        assert mhs.preferred_model() is None
