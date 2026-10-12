"""The shipped Tiny Agent's backend address (deployment hygiene).

``agent/agent.py`` is a standalone entry point that ships beside the app, so it
is loaded by path here rather than imported as a package module. These tests
pin the behaviour an operator depends on: a wrong address fails LOUDLY with the
fix in the message, and the address that a real deployment must not use
(loopback, or plain http across the network) is called out on startup.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_AGENT_PATH = (Path(__file__).resolve().parents[2] / "agent" / "agent.py")


@pytest.fixture(scope="module")
def agent_mod():
    assert _AGENT_PATH.is_file(), _AGENT_PATH
    spec = importlib.util.spec_from_file_location("mc_agent_entry", _AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)          # defines only; main() is not run
    return module


def test_address_is_normalised(agent_mod):
    assert agent_mod.validate_backend(" http://teacher-pc:8642/ ") \
        == "http://teacher-pc:8642"
    assert agent_mod.validate_backend("https://maths.example.org") \
        == "https://maths.example.org"


@pytest.mark.parametrize("bad, hint", [
    ("http://teacher-pc:8642/api", "/api"),        # a pasted web address
    ("http://teacher-pc:8642/api/distributed/ping", "/api/distributed/ping"),
    ("http://teacher-pc:8642/?x=1", "?x=1"),
])
def test_a_path_is_rejected_with_the_reason(agent_mod, bad, hint):
    with pytest.raises(SystemExit) as excinfo:
        agent_mod.validate_backend(bad)
    message = str(excinfo.value)
    assert hint in message
    assert "scheme://host:port" in message


@pytest.mark.parametrize("bad", ["teacher-pc:8642", "ftp://host:1", "not a url",
                                 "http://"])
def test_a_missing_or_wrong_scheme_is_rejected(agent_mod, bad):
    with pytest.raises(SystemExit) as excinfo:
        agent_mod.validate_backend(bad)
    assert "not a valid http(s) address" in str(excinfo.value)


def test_an_empty_address_says_where_to_set_it(agent_mod):
    with pytest.raises(SystemExit) as excinfo:
        agent_mod.validate_backend("")
    message = str(excinfo.value)
    assert "agent.json" in message and "--backend" in message


def test_loopback_is_called_out_because_a_second_laptop_needs_a_real_address(
        agent_mod):
    notes = agent_mod.backend_warnings("http://127.0.0.1:8642")
    assert len(notes) == 1
    assert "THIS computer (loopback)" in notes[0]
    assert "http://teacher-pc:8642" in notes[0]


def test_plain_http_across_the_network_is_called_out(agent_mod):
    notes = agent_mod.backend_warnings("http://192.168.1.50:8642")
    assert len(notes) == 1
    assert "unencrypted" in notes[0]


def test_https_over_the_network_is_quiet(agent_mod):
    assert agent_mod.backend_warnings("https://maths.example.org") == []


def test_the_default_address_is_configurable_by_environment(agent_mod,
                                                            monkeypatch):
    monkeypatch.setenv("MC_BACKEND_URL", "http://backend-pc:9000")
    assert agent_mod.default_config()["backend_url"] == "http://backend-pc:9000"
    monkeypatch.delenv("MC_BACKEND_URL", raising=False)
    # The shipped fallback stays a loopback dev address, which
    # backend_warnings() loudly flags when it is actually used.
    assert agent_mod.default_config()["backend_url"] == "http://127.0.0.1:8642"
    assert agent_mod.default_config()["interval_seconds"] == 300


def test_the_agent_never_reads_a_password(agent_mod):
    """The config carries a session token, never user credentials."""
    cfg = agent_mod.default_config()
    assert "password" not in {k.lower() for k in cfg}
    assert cfg["auth_token"] == "" or isinstance(cfg["auth_token"], str)
    assert cfg["trusted_update_keys"] == {}     # pinned later, never generated here
