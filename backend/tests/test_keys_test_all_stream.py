"""Contract tests for ``GET /api/keys/test/stream`` — the "Test all keys" run.

The desktop page and the web page both drive this one SSE endpoint, so its
contract is what the UI can be correct against. These tests pin down:

* the event sequence and the field names each event carries,
* the arithmetic of the summary (passed + failed + skipped == tested == total),
* that the ``done`` summary counts KEYS, not events — the desktop page used to
  count every event as a key and reported "Finished testing 10 keys" for two,
* that no key material ever reaches the client,
* and that a failure which is not a credential rejection (rate limit, timeout,
  local client error) does NOT mark the key invalid in the stored status —
  the "previously working key reported as invalid" class of defect.

The provider is replaced with a deterministic fake, so nothing here touches the
network or spends quota.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

KEY_MATERIAL = [
    "AIzaTest1111111111111111111111",
    "AIzaTest2222222222222222222222",
    "AIzaTest3333333333333333333333",
]


def _parse(body: str) -> list[dict]:
    """Every ``data:`` payload, in order."""
    events = []
    for line in body.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                events.append(json.loads(payload))
    return events


def _state(index: int, fp: str, status: str, *, stages: list[str],
           auth_ok: bool | None, generate_ok: bool | None,
           error_kind: str | None = None, message: str | None = None,
           response_time: int | None = None):
    """A real ``KeyTestState`` with the stage trail a live run would produce."""
    from app.gemini.tester import KeyTestState

    state = KeyTestState(index=index, fingerprint=fp)
    state.status = status
    state.auth_ok = auth_ok
    state.generate_ok = generate_ok
    state.error_kind = error_kind
    state.error_message = message
    state.response_time_ms = response_time
    state.events = [{"stage": stage, "message": f"{stage} reached (test fake)"}
                    for stage in stages]
    return state


@pytest.fixture
def fake_tester(monkeypatch):
    """Replace the provider call with three deterministic outcomes:
    healthy, rate-limited, and a local client error."""
    import app.api.routes_all as routes_all
    from app.gemini.key_manager import fingerprint

    outcomes = {
        1: _state(1, fingerprint(KEY_MATERIAL[0]), "HEALTHY",
                  stages=["connecting", "auth", "generate", "done"],
                  auth_ok=True, generate_ok=True, response_time=123),
        2: _state(2, fingerprint(KEY_MATERIAL[1]), "COOLDOWN",
                  stages=["connecting", "auth", "done"],
                  auth_ok=True, generate_ok=False, error_kind="rate_limited",
                  message="Gemini rate/quota limit hit. The key was temporarily "
                          "removed from rotation; work continues on another "
                          "available key."),
        3: _state(3, fingerprint(KEY_MATERIAL[2]), "ERROR",
                  stages=["done"], auth_ok=False, generate_ok=None,
                  error_kind="local_client_error",
                  message="Could not prepare the Gemini client on this machine."),
    }
    calls: list[int] = []

    def fake_test_key(index: int, key: str, fp: str):
        calls.append(index)
        state = outcomes[index]
        assert state.fingerprint == fp, "the endpoint must pass the real fingerprint"
        assert key == KEY_MATERIAL[index - 1], "the endpoint must pass the real key"
        return state

    monkeypatch.setattr(routes_all, "test_key", fake_test_key)
    return calls


@pytest.fixture
def client(fresh_state):
    from app.config import get_config, get_secrets, save_config

    cfg = get_config()
    cfg.mock_mode = True          # never reach the real provider
    save_config(cfg)
    get_secrets().set_gemini_keys(list(KEY_MATERIAL))

    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_stream_events_and_summary_are_consistent(client, fake_tester):
    response = client.get("/api/keys/test/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _parse(response.text)
    kinds = [e["type"] for e in events]

    assert kinds[0] == "start"
    assert kinds[-1] == "done"
    assert kinds.count("start") == 1
    assert kinds.count("done") == 1
    # One result per key, and stage events for live progress.
    assert kinds.count("key_result") == len(KEY_MATERIAL)
    assert kinds.count("key_stage") >= len(KEY_MATERIAL)
    assert fake_tester == [1, 2, 3], "each configured key must be tested exactly once"

    start = events[0]
    assert start["total"] == len(KEY_MATERIAL)

    # The summary on every key_result must count KEYS, and must add up.
    for position, event in enumerate(e for e in events if e["type"] == "key_result"):
        summary = event["summary"]
        assert summary["tested"] == position + 1
        assert (summary["passed"] + summary["failed"] + summary["skipped"]
                == summary["tested"])

    results = [e for e in events if e["type"] == "key_result"]
    assert [r["index"] for r in results] == [1, 2, 3]
    assert [r["status"] for r in results] == ["HEALTHY", "COOLDOWN", "ERROR"]
    assert [r["error_kind"] for r in results] == [None, "rate_limited",
                                                 "local_client_error"]
    assert results[0]["response_time_ms"] == 123
    for result in results:
        # Everything the desktop row needs, and nothing that identifies a key.
        for field in ("index", "fingerprint", "status", "auth_ok",
                      "generate_ok", "error_kind", "error_message", "summary"):
            assert field in result, field
        assert result["fingerprint"] and len(result["fingerprint"]) <= 64

    # A failing key must still be reported with a diagnostic message.
    assert results[1]["error_message"]
    assert results[2]["error_message"]

    final = events[-1]["summary"]
    assert final == {**final}  # dict, not a JSON string
    assert final["total"] == 3
    assert final["tested"] == 3
    assert final["passed"] == 1
    assert final["failed"] == 2
    assert final["skipped"] == 0
    assert isinstance(final["elapsed_s"], (int, float)) and final["elapsed_s"] >= 0


def test_stage_events_precede_their_own_key_result(client, fake_tester):
    events = _parse(client.get("/api/keys/test/stream").text)
    seen: dict[int, int] = {}
    for position, event in enumerate(events):
        if event["type"] == "key_stage":
            assert {"index", "fingerprint", "stage", "message"} <= set(event), event
            seen.setdefault(event["index"], position)
        elif event["type"] == "key_result":
            assert event["index"] in seen, (
                f"key {event['index']} produced a result with no stage before it")
            assert seen[event["index"]] < position


def test_the_stream_never_carries_key_material(client, fake_tester):
    body = client.get("/api/keys/test/stream").text
    for key in KEY_MATERIAL:
        assert key not in body
    assert "AIza" not in body


def test_a_rate_limited_or_local_error_does_not_invalidate_the_key(client, fake_tester):
    """The reported symptom this pins down: a key that works, sidelined as
    INVALID by a failure that was never a credential rejection."""
    client.get("/api/keys/test/stream")
    status = client.get("/api/keys").json()

    assert status["total"] == 3
    assert status["invalid"] == 0, (
        "a rate limit and a local client error must never be stored as a "
        "rejected credential")

    by_status = {k["fingerprint"]: k["status"] for k in status["keys"]}
    from app.gemini.key_manager import fingerprint

    assert by_status[fingerprint(KEY_MATERIAL[0])] == "HEALTHY"
    # The rate-limited key is cooling down, not invalid.
    assert by_status[fingerprint(KEY_MATERIAL[1])] in ("COOLDOWN", "HEALTHY")
    # The key whose test could not run here keeps whatever status it had.
    assert by_status[fingerprint(KEY_MATERIAL[2])] != "INVALID"


def test_an_invalid_credential_is_still_recorded_as_invalid(client, monkeypatch):
    """The other half of the contract: an explicit rejection must stick."""
    import app.api.routes_all as routes_all
    from app.gemini.key_manager import fingerprint

    def rejecting(index: int, key: str, fp: str):
        return _state(index, fp, "INVALID", stages=["connecting", "auth", "done"],
                      auth_ok=False, generate_ok=None, error_kind="invalid_key",
                      message="Gemini rejected this API key.")

    monkeypatch.setattr(routes_all, "test_key", rejecting)
    client.get("/api/keys/test/stream")

    status = client.get("/api/keys").json()
    assert status["invalid"] == status["total"] == 3
    assert all(k["status"] == "INVALID" for k in status["keys"])
    assert all(k["fingerprint"] == fingerprint(material)
               for k, material in zip(sorted(status["keys"],
                                             key=lambda k: k["fingerprint"]),
                                      sorted(KEY_MATERIAL,
                                             key=fingerprint)))


def test_single_key_test_reports_the_fields_the_page_shows(client, monkeypatch):
    """The "Test selected" path: the page maps exactly these fields, and the
    result must be recorded against the key as well as returned."""
    import app.api.routes_all as routes_all
    from app.gemini.key_manager import fingerprint

    target = fingerprint(KEY_MATERIAL[1])

    def fake(index: int, key: str, fp: str):
        return _state(index, fp, "COOLDOWN", stages=["done"], auth_ok=True,
                      generate_ok=False, error_kind="rate_limited",
                      message="Gemini rate/quota limit hit.", response_time=None)

    monkeypatch.setattr(routes_all, "test_key", fake)

    response = client.post(f"/api/keys/{target}/test")
    assert response.status_code == 200
    body = response.json()
    assert body["fingerprint"] == target
    assert body["status"] == "COOLDOWN"
    assert body["auth_ok"] is True
    assert body["generate_ok"] is False
    assert body["error_kind"] == "rate_limited"
    assert body["message"] == "Gemini rate/quota limit hit."
    assert "AIza" not in response.text

    # Recorded, and a rate limit is not a rejection.
    status = client.get("/api/keys").json()
    assert status["invalid"] == 0
    key = next(k for k in status["keys"] if k["fingerprint"] == target)
    assert key["requests"] >= 1


def test_single_key_test_refuses_an_unknown_fingerprint(client):
    response = client.post("/api/keys/not-a-real-fingerprint/test")
    assert response.status_code == 404


def test_no_configured_keys_is_an_honest_empty_run(fresh_state):
    from app.config import get_config, get_secrets, save_config

    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    get_secrets().set_gemini_keys([])

    from app.main import app
    with TestClient(app) as test_client:
        events = _parse(test_client.get("/api/keys/test/stream").text)

    assert [e["type"] for e in events] == ["start", "done"]
    assert events[0]["total"] == 0
    assert events[1]["summary"]["tested"] == 0
    assert events[1]["summary"]["passed"] == events[1]["summary"]["failed"] == 0


def test_a_client_that_disconnects_early_leaves_no_partial_state(client, monkeypatch):
    """A disconnected run must stop instead of testing on behalf of nobody."""
    import app.api.routes_all as routes_all

    tested: list[int] = []

    def counting(index: int, key: str, fp: str):
        tested.append(index)
        import time as _time
        _time.sleep(0.05)          # give the disconnect a chance to be seen
        return _state(index, fp, "HEALTHY", stages=["done"], auth_ok=True,
                      generate_ok=True, response_time=5)

    monkeypatch.setattr(routes_all, "test_key", counting)

    from app.config import get_secrets
    get_secrets().set_gemini_keys([f"AIzaTest{i:022d}" for i in range(1, 9)])

    with client.stream("GET", "/api/keys/test/stream") as stream:
        next(stream.iter_lines())   # read the first event, then hang up
    # The generator keeps running until the next is_disconnected() check; it
    # must not have walked every key after the client left.
    assert len(tested) < 8, f"the run continued after the client left: {tested}"


def test_testing_one_key_reports_a_message_even_when_it_passes(client):
    """The single-key endpoint must SAY something on success.

    It returned ``message: null`` for a healthy key — the page then had nothing
    to display about a key that had just passed both stages, while the add-keys
    endpoint worded the same outcome for the same user. Two ways of testing one
    key disagreeing about what they report is a contract defect, not a detail.
    """
    from app.gemini.key_manager import fingerprint

    fp = fingerprint(KEY_MATERIAL[0])
    response = client.post(f"/api/keys/{fp}/test")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "HEALTHY"
    assert body["auth_ok"] is True and body["generate_ok"] is True
    assert isinstance(body["message"], str) and body["message"].strip(), \
        "a successful test must explain itself instead of returning null"
    assert "accepted" in body["message"].lower()


def test_testing_an_unknown_key_is_a_404(client):
    response = client.post("/api/keys/does-not-exist/test")
    assert response.status_code == 404
