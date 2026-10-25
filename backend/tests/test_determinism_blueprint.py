"""Reliability tests for the correction pipeline (V5 determinism work).

Covers the four properties that made identical inputs disagree before:
  1. sampling was left at the API default          -> now temperature 0 + seed
  2. question segmentation/max marks re-invented   -> cached mark-scheme blueprint
  3. missing questions silently dropped            -> REVIEW, never silent zero
  4. a mid-job 429 switched MODEL instead of KEY   -> in-job key rotation
"""

import json

import pytest

from app.engine import blueprint as bp
from app.engine import legacy_marking as legacy
from app.engine.verify import verify_result


# ------------------------------------------------------------------ helpers

class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.configs = []

    def generate_content(self, *, model, contents, config=None, **kw):
        self.calls.append({"model": model, "contents": contents,
                           "config": config})
        self.configs.append(config)
        # "*" matches any model so tests stay independent of the configured
        # fallback chain order.
        action = self.responses.get(model, self.responses.get("*"))
        if isinstance(action, Exception):
            raise action
        return FakeResponse(action)


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)
        self.deleted = []

    def upload_file(self, path):
        return f"remote:{path}"

    def files_delete(self, remote):
        self.deleted.append(remote)


MARKING_JSON = json.dumps({
    "paper_info": {"qualification": "Q", "paper": "1H", "session": "S"},
    "questions": [
        {"question_number": "1", "max_marks": 3, "awarded_marks": 3,
         "topic": "t", "student_answer": "a", "working_summary": "w",
         "loss_reason": "", "mark_breakdown": [], "correct_method": "c",
         "full_mark_solution": "f"},
        {"question_number": "2", "max_marks": 4, "awarded_marks": 1,
         "topic": "t", "student_answer": "a", "working_summary": "w",
         "loss_reason": "wrong", "mark_breakdown": [], "correct_method": "c",
         "full_mark_solution": "f"},
    ],
    "overall_notes": {"summary": "s", "strengths": [], "revision_areas": []},
    "uncertainties": [],
})

BLUEPRINT_JSON = json.dumps({
    "paper": "1H May 2024",
    "total_marks": 7,
    "questions": [
        {"question_number": "1", "max_marks": 3, "criteria": "M1 A1 A1"},
        {"question_number": "2", "max_marks": 4, "criteria": "B1 B1 B1 B1"},
        {"question_number": "3", "max_marks": 0 + 5, "criteria": "M1 A1 A1 A1 A1"},
    ],
})


# -------------------------------------------------------- 1. determinism

class TestDeterministicSampling:
    def setup_method(self):
        legacy.MODEL_HEALTH.reset()

    def teardown_method(self):
        legacy.MODEL_HEALTH.reset()

    def test_grade_uses_temperature_zero_and_fixed_seed(self):
        client = FakeClient({"m1": MARKING_JSON})
        legacy.grade_with_model(client, ["m1"], ["sf"], ["ms"])
        config = client.models.configs[0]
        assert config["temperature"] == 0.0
        assert config["seed"] == 42

    def test_audit_also_uses_deterministic_sampling(self):
        client = FakeClient({"m1": MARKING_JSON})
        legacy.audit_result(client, {"questions": []}, ["sf"], ["ms"], ["m1"])
        config = client.models.configs[0]
        assert config["temperature"] == 0.0
        assert config["seed"] == 42

    def test_schema_and_system_prompt_still_present(self):
        client = FakeClient({"m1": MARKING_JSON})
        legacy.grade_with_model(client, ["m1"], ["sf"], ["ms"])
        config = client.models.configs[0]
        assert config["response_schema"] is legacy.OUTPUT_SCHEMA
        assert config["system_instruction"] == legacy.MARKING_SYSTEM

    def test_same_inputs_same_prompt_without_blueprint(self):
        c1 = FakeClient({"m1": MARKING_JSON})
        c2 = FakeClient({"m1": MARKING_JSON})
        legacy.grade_with_model(c1, ["m1"], ["sf"], ["ms"])
        legacy.grade_with_model(c2, ["m1"], ["sf"], ["ms"])
        assert (c1.models.calls[0]["contents"]
                == c2.models.calls[0]["contents"])
        assert c1.models.configs[0] == c2.models.configs[0]


# -------------------------------------------------------- 2. blueprint prompt

class TestBlueprintPrompt:
    def setup_method(self):
        legacy.MODEL_HEALTH.reset()

    def teardown_method(self):
        legacy.MODEL_HEALTH.reset()

    def test_blueprint_block_is_injected(self):
        blueprint = {"paper": "1H", "total_marks": 7, "questions": [
            {"question_number": "1", "max_marks": 3, "criteria": "M1 A1"},
            {"question_number": "2", "max_marks": 4, "criteria": "B1"},
        ]}
        client = FakeClient({"m1": MARKING_JSON})
        legacy.grade_with_model(client, ["m1"], ["sf"], ["ms"],
                                blueprint=blueprint)
        contents = client.models.calls[0]["contents"]
        prompt = contents[-1]
        assert "OFFICIAL MARK SCHEME BLUEPRINT" in prompt
        assert "question_number=1 max_marks=3" in prompt
        assert "Do not invent, split, merge, rename or omit" in prompt

    def test_no_blueprint_keeps_legacy_prompt(self):
        client = FakeClient({"m1": MARKING_JSON})
        legacy.grade_with_model(client, ["m1"], ["sf"], ["ms"])
        prompt = client.models.calls[0]["contents"][-1]
        assert "BLUEPRINT" not in prompt


# -------------------------------------------------------- 3. blueprint cache

class TestBlueprintExtraction:
    def _write_scheme(self, tmp_path):
        path = tmp_path / "scheme.pdf"
        path.write_bytes(b"%PDF-1.4 fake mark scheme content")
        return path

    def test_extract_builds_then_caches(self, fresh_state, tmp_path):
        path = self._write_scheme(tmp_path)
        client = FakeClient({"*": BLUEPRINT_JSON})
        first, meta = bp.get_blueprint(client, path)
        assert meta["source"] == "built"
        assert first["total_marks"] == 12
        assert [q["question_number"] for q in first["questions"]] == ["1", "2", "3"]
        assert client.deleted, "blueprint upload must be cleaned up"

        class ExplodingClient:
            def upload_file(self, path):
                raise AssertionError("cache should prevent a second upload")

        second, meta2 = bp.get_blueprint(ExplodingClient(), path)
        assert meta2["source"] == "cache"
        assert second == first

    def test_unusable_extraction_returns_none(self, fresh_state, tmp_path):
        path = self._write_scheme(tmp_path)
        client = FakeClient({"*": json.dumps({"questions": "not-a-list"})})
        got, meta = bp.get_blueprint(client, path)
        assert got is None
        assert meta["source"] == "unavailable"
        assert meta.get("error")

    def test_mock_mode_skips_extraction(self, fresh_state, tmp_path):
        from app.config import get_config, save_config
        cfg = get_config()
        cfg.mock_mode = True
        save_config(cfg)
        path = self._write_scheme(tmp_path)

        class ExplodingClient:
            def upload_file(self, path):
                raise AssertionError("mock mode must not call the API")

        got, meta = bp.get_blueprint(ExplodingClient(), path)
        assert got is None
        assert meta["source"] == "unavailable"

    def test_missing_file_is_reported_not_raised(self, fresh_state, tmp_path):
        got, meta = bp.get_blueprint(None, tmp_path / "nope.pdf")
        assert got is None and meta["source"] == "unavailable"

    def test_validate_rejects_bad_shapes(self):
        assert not bp.validate(None)
        assert not bp.validate({"questions": []})
        assert not bp.validate({"questions": [{"question_number": "1",
                                               "max_marks": 0}]})
        assert not bp.validate({"questions": [
            {"question_number": "1", "max_marks": 2},
            {"question_number": "1", "max_marks": 3},
        ]})
        assert bp.validate({"questions": [
            {"question_number": "1", "max_marks": 2, "criteria": ""}]})


# -------------------------------------------------------- 4. reconciliation

def _model_result():
    return legacy.normalise_result(json.loads(MARKING_JSON))


def _blueprint():
    return json.loads(BLUEPRINT_JSON)


class TestApplyBlueprint:
    def test_missing_question_becomes_review_not_silent_zero(self):
        result = _model_result()          # questions 1 and 2 only
        out, report = bp.apply_blueprint(result, _blueprint())
        numbers = [q["question_number"] for q in out["questions"]]
        assert numbers == ["1", "2", "3"]
        assert report["missing_questions"] == ["3"]
        q3 = out["questions"][2]
        assert q3["status"] == "REVIEW"
        assert "UNMARKED" in q3["review_reason"].upper()
        # the gap counts against the total so a dropped question can never
        # inflate the percentage again
        earned, available, pct = legacy.calculate_totals(out)
        assert available == 12
        assert pct == pytest.approx(4 / 12 * 100)

    def test_max_marks_are_forced_to_the_mark_scheme(self):
        result = _model_result()
        # model claims question 2 is worth 4; blueprint says 4 — adjust the
        # blueprint instead to prove the correction direction
        blue = _blueprint()
        blue["questions"][1]["max_marks"] = 6
        out, report = bp.apply_blueprint(result, blue)
        q2 = next(q for q in out["questions"] if q["question_number"] == "2")
        assert q2["max_marks"] == 6
        assert report["max_marks_corrected"] == [
            {"question": "2", "model": 4, "mark_scheme": 6}]

    def test_inflated_model_max_is_clamped_down(self):
        result = _model_result()
        result["questions"][1]["awarded_marks"] = 4
        result["questions"][1]["max_marks"] = 9   # model inflated the maximum
        blue = _blueprint()
        blue["questions"][1]["max_marks"] = 4
        out, _ = bp.apply_blueprint(result, blue)
        q2 = next(q for q in out["questions"] if q["question_number"] == "2")
        assert q2["awarded_marks"] == 4
        assert q2["max_marks"] == 4

    def test_sub_parts_merge_onto_one_blueprint_question(self):
        result = legacy.normalise_result({
            "questions": [
                {"question_number": "1", "max_marks": 3, "awarded_marks": 3},
                {"question_number": "14(a)", "max_marks": 3, "awarded_marks": 2},
                {"question_number": "14(b)", "max_marks": 3, "awarded_marks": 1},
            ],
        })
        blue = {"questions": [
            {"question_number": "1", "max_marks": 3, "criteria": ""},
            {"question_number": "14", "max_marks": 6, "criteria": ""},
        ], "total_marks": 9}
        out, report = bp.apply_blueprint(result, blue)
        numbers = [q["question_number"] for q in out["questions"]]
        assert numbers == ["1", "14"]
        assert report["merged_questions"] == ["14"]
        q14 = out["questions"][1]
        assert q14["awarded_marks"] == 3   # 2 + 1, clamped to 6
        assert q14["max_marks"] == 6

    def test_prefix_matching_does_not_swallow_neighbour(self):
        assert bp._matches("14(a)", "14")
        assert bp._matches("14", "14")
        assert not bp._matches("14", "1")
        assert not bp._matches("1", "14")

    def test_unknown_question_is_kept_and_flagged_review(self):
        result = _model_result()
        result["questions"].append({
            "question_number": "99", "max_marks": 2, "awarded_marks": 1,
            "topic": "", "student_answer": "", "working_summary": "",
            "loss_reason": "", "mark_breakdown": [], "correct_method": "",
            "full_mark_solution": "",
        })
        out, report = bp.apply_blueprint(result, _blueprint())
        assert report["extra_questions"] == ["99"]
        q99 = next(q for q in out["questions"] if q["question_number"] == "99")
        assert q99["status"] == "REVIEW"
        assert q99["awarded_marks"] == 1   # never deleted

    def test_no_blueprint_marks_everything_valid(self):
        result = _model_result()
        out, report = bp.apply_blueprint(result, None)
        assert report["applied"] is False
        assert all(q["status"] == "VALID" for q in out["questions"])


# -------------------------------------------------------- 5. verification

class TestNeverZero:
    def test_missing_breakdown_never_changes_marks(self):
        result = legacy.normalise_result({
            "questions": [{"question_number": "1", "max_marks": 3,
                           "awarded_marks": 3, "mark_breakdown": []}],
        })
        bp.apply_blueprint(result, None)
        verification = verify_result(result)
        assert result["questions"][0]["awarded_marks"] == 3
        assert verification["earned"] == 3
        assert verification["status"] == "VALID"

    def test_unmarked_blueprint_question_is_anomaly_and_review(self):
        result = _model_result()
        out, report = bp.apply_blueprint(result, _blueprint())
        verification = verify_result(out, report)
        assert verification["status"] == "REVIEW"
        assert any("UNMARKED" in a for a in verification["anomalies"])
        assert verification["review_questions"] == ["3"]
        # marked questions keep their marks
        assert out["questions"][0]["awarded_marks"] == 3

    def test_clean_result_reports_valid(self):
        result = legacy.normalise_result(json.loads(MARKING_JSON))
        blue = {"total_marks": 7, "questions": [
            {"question_number": "1", "max_marks": 3, "criteria": ""},
            {"question_number": "2", "max_marks": 4, "criteria": ""},
        ]}
        out, report = bp.apply_blueprint(result, blue)
        verification = verify_result(out, report)
        assert verification["status"] == "VALID"
        assert verification["verified"] is True
        assert verification["earned"] == 4
        assert verification["available"] == 7


# -------------------------------------------------------- 6. in-job key rotation

class TestMidJobKeyRotation:
    def test_preferred_key_is_used_first(self, fresh_state, monkeypatch):
        from app.config import get_secrets
        from app.gemini.client_proxy import KeyAwareClient
        from app.gemini.key_manager import KeyManager, fingerprint

        keys = ["key-alpha-0123456789", "key-bravo-0123456789"]
        get_secrets().set_gemini_keys(keys)
        km = KeyManager()
        fp_a, fp_b = (fingerprint(k) for k in keys)
        client = KeyAwareClient(km, preferred_fp=fp_a)

        used = []

        class FakeSDK:
            def __init__(self, key):
                self.key = key

            class _M:
                pass

            @property
            def models(self):
                outer = self

                class M:
                    def generate_content(self, *, model, contents, config=None, **kw):
                        used.append(outer.key)
                        return FakeResponse('{"ok": true}')

                return M()

        monkeypatch.setattr(client, "_sdk_client_for_key",
                            lambda key: FakeSDK(key))
        client.models.generate_content(model="m1", contents=["x"])
        assert used == [keys[0]]          # sticky on the leased key

    def test_429_rotates_to_the_other_key_without_changing_model(
            self, fresh_state, monkeypatch):
        from app.config import get_secrets
        from app.gemini.client_proxy import KeyAwareClient
        from app.gemini.key_manager import KeyManager, fingerprint

        keys = ["key-alpha-0123456789", "key-bravo-0123456789"]
        get_secrets().set_gemini_keys(keys)
        km = KeyManager()
        fp_a, fp_b = (fingerprint(k) for k in keys)
        client = KeyAwareClient(km, preferred_fp=fp_a)

        attempts = []

        class FakeSDK:
            def __init__(self, key):
                self.key = key

            @property
            def models(self):
                outer = self

                class M:
                    def generate_content(self, *, model, contents, config=None, **kw):
                        attempts.append((outer.key, model))
                        if outer.key == keys[0]:
                            raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
                        return FakeResponse('{"ok": true}')

                return M()

        monkeypatch.setattr(client, "_sdk_client_for_key",
                            lambda key: FakeSDK(key))
        response = client.models.generate_content(model="m1", contents=["x"])

        assert response.text == '{"ok": true}'
        # the FIRST key failed and the SECOND served the SAME model call —
        # no model fallback was needed.
        assert attempts == [(keys[0], "m1"), (keys[1], "m1")]
        # the failing key is cooling down so later jobs skip it
        status = km.status()["keys"]
        by_fp = {k["fingerprint"]: k for k in status}
        assert by_fp[fp_a]["cooldown_remaining"] > 0
        assert by_fp[fp_b]["status"] == "HEALTHY"
        # safe identifiers only — no key material anywhere
        assert all("key-" not in json.dumps(k) for k in status)

    def test_files_are_reuploaded_after_mid_job_rotation(
            self, fresh_state, monkeypatch, tmp_path):
        """Gemini files are scoped to their uploading key: a 429 rotation must
        re-upload the inputs under the new key instead of dying on a cross-key
        403 PERMISSION_DENIED."""
        from app.config import get_secrets
        from app.gemini.client_proxy import KeyAwareClient
        from app.gemini.key_manager import KeyManager, fingerprint

        keys = ["key-alpha-0123456789", "key-bravo-0123456789"]
        get_secrets().set_gemini_keys(keys)
        km = KeyManager()
        fp_a, fp_b = (fingerprint(k) for k in keys)
        client = KeyAwareClient(km, preferred_fp=fp_a)

        pdf = tmp_path / "submission.pdf"
        pdf.write_bytes(b"%PDF-1.4 student work")

        class Remote:
            def __init__(self, name, owner):
                self.name = name
                self.state = type("S", (), {"name": "ACTIVE"})()
                self.owner = owner

        uploads = []   # (key, remote)
        generates = []

        class Files:
            def __init__(self, key):
                self.key = key

            def upload(self, *, file):
                remote = Remote(f"files/{'a' if self.key == keys[0] else 'b'}"
                                f"{len(uploads)}", self.key)
                uploads.append((self.key, remote))
                return remote

            def get(self, *, name):
                return Remote(name, self.key)

            def delete(self, *, name):
                return None

        class FakeSDK:
            def __init__(self, key):
                self.key = key
                self.files = Files(key)

            @property
            def models(self):
                outer = self

                class M:
                    def generate_content(self, *, model, contents, config=None, **kw):
                        # Pre-flight quota probe before a paid re-upload
                        # (bandwidth §5/§6): tiny request, not a marking call.
                        if contents == "ping":
                            if outer.key == keys[0]:
                                raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
                            return FakeResponse('{"ok": true}')
                        generates.append((outer.key, model))
                        if outer.key == keys[0]:
                            raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
                        # a file owned by the other key must never reach us
                        owners = {c.owner for c in contents if hasattr(c, "owner")}
                        assert owners == {keys[1]}, owners
                        return FakeResponse('{"ok": true}')

                return M()

        monkeypatch.setattr(client, "_sdk_client_for_key",
                            lambda key: FakeSDK(key))

        remote = client.upload_file(str(pdf))
        assert remote.owner == keys[0]

        response = client.models.generate_content(
            model="m1", contents=[remote, "mark this"])

        assert response.text == '{"ok": true}'
        # the input was uploaded twice: once with KEY_1, once after rotation
        assert [k for k, _ in uploads] == [keys[0], keys[1]]
        # the SAME model was retried on the second key — no model fallback
        assert generates == [(keys[0], "m1"), (keys[1], "m1")]


# ------------------------------------------------- 5. page-scoped verification
class TestInventoryVerificationSampling:
    """The authoritative page-scoped read must request the same sampling.

    Eight simultaneous corrections once produced four different totals because
    every verification read of the same mark scheme invented a different
    structure at the provider's default temperature. The call site is the only
    thing that can enforce that, so it is asserted directly.
    """

    def test_verification_read_pins_deterministic_sampling(
            self, fresh_state, tmp_path, monkeypatch):
        from app.engine import scheme_inventory as si

        seen: dict = {}

        def fake_extract(client, path, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("captured the call")

        monkeypatch.setattr(si, "extract_inventory", fake_extract)
        path = tmp_path / "scheme.pdf"
        path.write_bytes(b"%PDF-1.4 fake mark scheme content")

        out = bp.verify_with_inventory(object(), path)

        assert seen.get("config_extra") == dict(legacy.DETERMINISTIC_SAMPLING)
        assert out["ok"] is False
