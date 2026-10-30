"""Concurrency control must reduce under throttling and never amplify a storm.

The two properties that make it safe are tested here: only capacity failures
shrink the pool (a malformed paper must not throttle healthy work), and the
decision survives a restart, because a restart during an outage otherwise
restores full parallelism into the same 429s.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_control():
    from app.workers import concurrency

    concurrency.concurrency_control().reset()
    yield
    concurrency.concurrency_control().reset()


def _control():
    from app.workers import concurrency

    return concurrency.ConcurrencyControl()


# -- additive increase / multiplicative decrease ------------------------------

def test_it_starts_at_the_configured_ceiling():
    control = _control()
    assert control.limit(4) == 4


def test_a_rate_limit_halves_the_pool_down_to_the_floor():
    control = _control()
    control.limit(4)

    control.note_outcome(ok=False, error_class="RATE_LIMIT")
    assert control.limit(4) == 2

    control.note_outcome(ok=False, error_class="HIGH_DEMAND")
    assert control.limit(4) == 1

    # At the floor it stops shrinking and stops churning.
    control.note_outcome(ok=False, error_class="RATE_LIMIT")
    assert control.limit(4) == 1


def test_a_wrong_request_never_throttles_the_pool():
    """A 400/403 says nothing about capacity."""
    control = _control()
    control.limit(4)
    for error_class in ("INVALID_REQUEST", "AUTHENTICATION_ERROR",
                        "CONTEXT_TOO_LARGE", "PARSING_ERROR"):
        control.note_outcome(ok=False, error_class=error_class)
    assert control.limit(4) == 4


def test_recovery_needs_a_sustained_run_of_successes():
    from app.workers import concurrency

    control = _control()
    control.limit(4)
    control.note_outcome(ok=False, error_class="RATE_LIMIT")
    assert control.limit(4) == 2

    # Not yet: growth needs a full streak.
    control.note_outcome(ok=True)
    assert control.limit(4) == 2

    # The watch window has elapsed (set directly instead of sleeping). One
    # success is already counted above, so three more reach the streak.
    control._last_decrease_at -= concurrency.RECOVERY_HOLD_SECONDS + 1
    for _ in range(concurrency.SUCCESSES_TO_GROW - 2):
        control.note_outcome(ok=True)
    assert control.limit(4) == 2

    control.note_outcome(ok=True)
    assert control.limit(4) == 3


def test_it_never_grows_past_the_ceiling():
    from app.workers import concurrency

    control = _control()
    control.limit(2)
    control.note_outcome(ok=False, error_class="RATE_LIMIT")   # now 1
    control._last_decrease_at -= concurrency.RECOVERY_HOLD_SECONDS + 1
    for _ in range(concurrency.SUCCESSES_TO_GROW * 3):
        control.note_outcome(ok=True)
    assert control.limit(2) == 2


# -- sharing and restart safety ----------------------------------------------

def test_a_restart_does_not_restore_full_parallelism():
    first = _control()
    first.limit(4)
    first.note_outcome(ok=False, error_class="RATE_LIMIT")

    # A new process (the pool after a restart) must see the reduction.
    restarted = _control()
    assert restarted.limit(4) == 2


def test_a_throttle_seen_by_a_worker_is_visible_to_the_pool():
    """The child that sees the 429 writes; the parent pool reads."""
    from app.workers import concurrency

    pool = concurrency.ConcurrencyControl()
    child = concurrency.ConcurrencyControl()
    assert pool.limit(6) == 6

    child.limit(6)
    child.note_outcome(ok=False, error_class="HIGH_DEMAND")

    # Force the pool past its refresh window (no sleeping in tests).
    pool._last_refresh = 0.0
    assert pool.limit(6) == 3


def test_switching_the_controller_off_restores_the_ceiling(monkeypatch):
    from app.workers import concurrency

    monkeypatch.setattr(concurrency, "_enabled", lambda: False)
    control = _control()
    control.limit(4)
    control.note_outcome(ok=False, error_class="RATE_LIMIT")
    assert control.limit(4) == 4
    assert control.snapshot()["enabled"] is False


# -- one retry owner ---------------------------------------------------------

def test_the_sdk_is_told_not_to_retry_underneath_the_proxy():
    """``_MAX_ATTEMPTS`` is the only retry loop; the HTTP layer must not add one."""
    from google.genai import types

    from app.gemini import client_proxy

    options = client_proxy._http_options(types, 600_000)
    assert options.retry_options is not None
    assert options.retry_options.attempts == client_proxy.SDK_RETRY_ATTEMPTS == 1
    assert options.timeout == 600_000


def test_a_real_sdk_client_carries_the_single_attempt_setting():
    from app.gemini.client_proxy import KeyAwareClient
    from app.gemini.key_manager import KeyManager

    client = KeyAwareClient(KeyManager(), pages=15)
    sdk = client._sdk_client_for_key("AIzaFakeKeyForTests0000000000")
    http_options = sdk._api_client._http_options
    assert http_options.retry_options is not None
    assert http_options.retry_options.attempts == 1
    # The per-page timeout is still applied: this change pins retries only.
    from app.gemini.client_proxy import timeout_for_pages

    assert http_options.timeout == timeout_for_pages(15)
