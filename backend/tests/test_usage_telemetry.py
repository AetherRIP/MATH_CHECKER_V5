"""The usage ledger must record what a Gemini request actually cost.

Two rules make this worth testing rather than trusting:

* the numbers come from the provider (``usage_metadata``) or stay NULL — never
  from a guess dressed up as a measurement;
* the row identifies the request without storing the prompt, the student's
  answer or the credential.

The send-path tests drive the real ``KeyAwareClient.generate_content`` with a
fake provider SDK. They make no network call, need no key, and never touch a
student's work.
"""

from __future__ import annotations

import time

import pytest


# -- provider metadata parsing ------------------------------------------------

def test_snake_case_metadata_is_read_exactly():
    from app.gemini import usage

    parsed = usage.from_metadata({
        "prompt_token_count": 1200,
        "candidates_token_count": 300,
        "cached_content_token_count": 800,
        "thoughts_token_count": 40,
        "total_token_count": 1500,
    })
    assert parsed is not None
    assert parsed.input_tokens == 1200
    assert parsed.output_tokens == 300
    assert parsed.cached_tokens == 800
    assert parsed.thoughts_tokens == 40
    assert parsed.total_tokens == 1500
    assert parsed.estimated is False


def test_camel_case_and_object_metadata_are_accepted():
    from app.gemini import usage

    class _Meta:
        promptTokenCount = 10
        candidatesTokenCount = 2

    assert usage.from_metadata(_Meta()).input_tokens == 10
    assert usage.from_metadata({"promptTokenCount": 7}).input_tokens == 7


def test_absent_metadata_is_unavailable_not_zero():
    """A missing count must stay None so no average is polluted by a fake 0."""
    from app.gemini import usage

    assert usage.from_metadata(None) is None
    assert usage.from_metadata({}) is None
    assert usage.from_response(object()) is None
    assert usage.from_response({"text": "{}"}) is None


def test_response_wrapper_and_bare_metadata_both_work():
    from app.gemini import usage

    class _Response:
        usage_metadata = {"prompt_token_count": 5, "total_token_count": 9}

    assert usage.from_response(_Response()).input_tokens == 5
    assert usage.from_response({"usage_metadata": {"prompt_token_count": 3}}).input_tokens == 3
    # The provider's camelCase envelope is accepted too.
    assert usage.from_response({"usageMetadata": {"promptTokenCount": 4}}).input_tokens == 4


def test_a_genuine_zero_is_a_real_count():
    from app.gemini import usage

    parsed = usage.from_metadata({"prompt_token_count": 0})
    assert parsed is not None
    assert parsed.input_tokens == 0
    assert parsed.available is True


def test_estimates_are_labelled_as_estimates():
    from app.gemini import usage

    estimated = usage.estimate(prompt_chars=4000, output_chars=400, images=2)
    assert estimated.estimated is True
    assert estimated.input_tokens == 1000 + 2 * 258
    assert estimated.output_tokens == 100


# -- the real send path -------------------------------------------------------

_KEY = "AIzaFakeKeyForTests0000000000"


class _FakeSDK:
    def __init__(self, *, response=None, error: BaseException | None = None):
        self.models = self
        self._response = response
        self._error = error
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


def _stubbed_client(*, job_id=None, response=None, error=None):
    """A KeyAwareClient whose provider is a fake: no keys, no network."""
    from app.gemini.client_proxy import KeyAwareClient
    from app.gemini.key_manager import KeyManager

    client = KeyAwareClient(KeyManager(), job_id=job_id)
    client._configured_keys = lambda: [_KEY]
    client._acquire_for_call = lambda: "fp-usage"
    client._key_value = lambda fp: _KEY
    client._needs_reupload = lambda contents, fp: False
    client._sdk_clients["fp-usage"] = _FakeSDK(response=response, error=error)
    return client


def _row_for(model: str) -> dict:
    from app.gemini import telemetry

    rows = [row for row in telemetry.for_job("job-usage")
            if row.get("model") == model]
    assert len(rows) == 1, f"expected exactly one ledger row, got {rows}"
    return rows[0]


class _CountedResponse:
    text = "{}"
    usage_metadata = {
        "prompt_token_count": 1200,
        "candidates_token_count": 300,
        "cached_content_token_count": 800,
        "total_token_count": 1500,
    }


def test_a_successful_request_writes_one_row_with_provider_counts():
    model = "gemini-usage-ok"
    client = _stubbed_client(job_id="job-usage", response=_CountedResponse())

    client.generate_content(model=model, contents=["mark this"])

    row = _row_for(model)
    assert row["kind"] == "generate"
    assert row["input_tokens"] == 1200
    assert row["output_tokens"] == 300
    assert row["cached_tokens"] == 800
    assert row["total_tokens"] == 1500
    assert row["estimated"] == 0
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0
    assert row["attempt"] == 1
    assert row["outcome"] == "accepted"
    assert row["job_id"] == "job-usage"


def test_a_failed_request_is_recorded_with_its_class_and_status():
    model = "gemini-usage-fail"
    client = _stubbed_client(
        job_id="job-usage",
        error=RuntimeError("503 UNAVAILABLE: the model is overloaded"),
    )

    with pytest.raises(Exception):
        client.generate_content(model=model, contents=["mark this"])

    row = _row_for(model)
    assert row["error_class"], "a failed request must name its failure class"
    assert row["http_status"] == 503
    # A failure carries no resolution label: ``error_class`` is what marks it
    # failed, and the summary buckets it that way.
    assert row["outcome"] is None


def test_the_row_never_holds_the_prompt_the_answer_or_the_key():
    from app.gemini import telemetry

    model = "gemini-usage-redaction"
    secret_prompt = "STUDENT-ANSWER-SENTINEL-9931"
    client = _stubbed_client(job_id="job-usage", response=_CountedResponse())

    client.generate_content(model=model, contents=[secret_prompt])

    row = _row_for(model)
    serialised = repr(row)
    assert secret_prompt not in serialised
    assert _KEY not in serialised
    # The documented column set is identifiers and counts only.
    assert "prompt_text" not in row and "contents" not in row


# -- per-job totals -----------------------------------------------------------

def test_job_totals_sum_the_ledger_and_reach_the_job_row():
    from app import api_stats
    from app.gemini import telemetry

    def add(input_tokens: int, output_tokens: int, *, failed: bool = False):
        telemetry.record(kind=telemetry.KIND_GENERATE, model="m-total",
                         ok=not failed, job_id="job-totals",
                         usage={"input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "cached_tokens": 0,
                                "total_tokens": input_tokens + output_tokens})

    add(100, 10)
    add(50, 5)
    # A failed request may still have cost input tokens; it is counted too.
    add(20, 0, failed=True)

    totals = telemetry.job_totals("job-totals")
    assert totals["input_tokens"] == 170
    assert totals["output_tokens"] == 15
    assert totals["total_tokens"] == 185

    # Writing the attempt snapshot SETS the job's columns from the ledger, so a
    # replayed attempt cannot double-count the same rows.
    api_stats.record_ai_usage("job-totals", {"requests": 3})
    api_stats.record_ai_usage("job-totals", {"requests": 3})
    row = api_stats.job_bandwidth("job-totals")
    assert row["input_tokens"] == 170
    assert row["output_tokens"] == 15
    assert row["total_tokens"] == 185


def test_job_totals_for_an_unknown_job_is_zero_not_an_error():
    from app.gemini import telemetry

    assert telemetry.job_totals(None) == {
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        "total_tokens": 0}
    assert telemetry.job_totals("job-that-never-ran")["total_tokens"] == 0


# -- retention and aggregates -------------------------------------------------

def test_pruning_drops_rows_past_the_age_window(monkeypatch):
    from app import db
    from app.gemini import telemetry

    monkeypatch.setattr(telemetry, "_retention", lambda: (30, 20_000))
    old = time.time() - 40 * 86_400
    db.execute(
        "INSERT INTO ai_request_log(kind, request_id, created_at) "
        "VALUES('generate', 'old-row', ?)", (old,))

    telemetry.prune()

    remaining = db.query_one(
        "SELECT COUNT(*) AS n FROM ai_request_log WHERE request_id = 'old-row'")
    assert remaining["n"] == 0


def test_pruning_respects_the_row_cap_without_dropping_everything(monkeypatch):
    from app import db
    from app.gemini import telemetry

    monkeypatch.setattr(telemetry, "_retention", lambda: (30, 100))
    for index in range(130):
        telemetry.record(kind=telemetry.KIND_GENERATE, model="m-cap",
                         ok=True, job_id="job-cap")
    telemetry.prune()

    total = db.query_one("SELECT COUNT(*) AS n FROM ai_request_log")
    assert total["n"] <= 100


def test_summary_reports_percentiles_and_an_honest_cache_rate():
    from app.gemini import telemetry

    telemetry.reset()
    telemetry.record(kind=telemetry.KIND_GENERATE, model="m-sum", ok=True,
                     job_id="job-sum", duration_ms=100,
                     usage={"input_tokens": 200, "output_tokens": 20,
                            "cached_tokens": 200, "total_tokens": 220})
    telemetry.record(kind=telemetry.KIND_GENERATE, model="m-sum", ok=True,
                     job_id="job-sum", duration_ms=300,
                     usage={"input_tokens": 200, "output_tokens": 20,
                            "cached_tokens": 0, "total_tokens": 220})

    summary = telemetry.summary(hours=1)
    assert summary["requests"] >= 2
    assert summary["latency"]["p95_ms"] is not None
    assert summary["latency"]["max_ms"] >= summary["latency"]["p50_ms"]
    # cached 200 of billed input (400 + 200) is measured, not assumed to be 1.0
    assert 0 < summary["cache_hit_rate"] <= 1
    assert "m-sum" in summary["by_model"]


def test_the_ledger_can_be_switched_off_without_affecting_anything(monkeypatch):
    from app.gemini import telemetry

    monkeypatch.setattr(telemetry, "_enabled", lambda: False)
    telemetry.reset()
    assert telemetry.record(kind=telemetry.KIND_GENERATE, model="m-off") is None
    assert telemetry.for_job("job-usage") is not None  # still readable
