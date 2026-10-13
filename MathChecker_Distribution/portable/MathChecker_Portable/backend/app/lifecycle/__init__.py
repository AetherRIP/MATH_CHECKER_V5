"""Server lifecycle: start on demand, stay up while in use, stop when idle.

The website is a local application, so the only reliable way to satisfy
"opening the site starts the service" is a tiny always-on process in front of
the heavy one:

* :mod:`app.lifecycle.supervisor` — a few-hundred-line, stdlib-only TCP relay
  that owns the public port, starts the backend on the first connection and
  keeps it as a single child. It never imports FastAPI, the database or any
  engine, so its own footprint stays small while it waits.
* :mod:`app.lifecycle.sessions` — which browser sessions are using the site
  right now (in memory only: a heartbeat must not touch the database).
* :mod:`app.lifecycle.monitor` — the backend's own decision to stop: nobody
  active for the grace period *and* no work in progress.
* :mod:`app.lifecycle.activity` / :mod:`app.lifecycle.busy` — the real
  in-process signals that say whether work is in progress.
* :mod:`app.lifecycle.control` — how the backend asks its own process to exit
  gracefully (uvicorn shut down its lifespan, workers drained).
"""

from __future__ import annotations

__all__ = ["activity", "busy", "control", "monitor", "sessions", "supervisor"]
