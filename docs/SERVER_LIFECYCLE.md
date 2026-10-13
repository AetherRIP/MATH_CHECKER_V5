# Server lifecycle — start on demand, stay up while in use, stop when idle

Math Checker is a **locally hosted** application: one process serves the UI and
the correction engine, and the teacher reaches it at `http://127.0.0.1:8642`.
That process used to run from the moment it was started until somebody stopped
it. It now keeps itself alive only while somebody is using the website, stops
after everybody has left, and starts again by itself when the site is opened.

Nothing in the correction engine, the workers, Classroom, the AI providers, the
database or the portable build was replaced. The lifecycle is a small layer
around the same application.

---

## 1. The two halves

| | what it does | where |
|---|---|---|
| **STRATEGY** | decides that nobody is here and stops the process | inside the backend — `backend/app/lifecycle/` |
| **START** | holds the app's address and starts the backend when a page asks | the relay — `backend/app/lifecycle/supervisor.py` |

A browser can never start a program on the teacher's computer, so the start
half must exist outside the browser. It is a ~600-line, **stdlib-only TCP
relay** that owns the public port, keeps exactly **one** backend child behind it
on a private port, and starts that child on the first request that needs it.
It imports no FastAPI, no database, no engine and no worker pool, which is what
makes "always listening" affordable while the heavy process is asleep.

```
browser ──► relay (public port, always up, ~10 MB)  ──►  backend (private port,
                 │                                        only while in use)
                 └── answers GET /api/health and /api/lifecycle/status
                     ITSELF while the backend is asleep
```

The backend listens on `127.0.0.1:<private>` only. Everything — the browser, LAN
mode, a Cloudflare tunnel — arrives through the one entry point that can also
start the app again.

### What "nobody is here" actually means

The backend keeps an in-memory registry of browser sessions, one per **tab**
(`sessionStorage` id), fed by a small heartbeat (`GET /api/lifecycle/heartbeat`,
30 s, no body, no database write). A session is inactive when its heartbeat has
been missing for `session_timeout_seconds` (90 s by default), so a crashed
browser, a slept laptop or a pulled cable ends up in the same state as a closed
tab. `pagehide` sends an explicit goodbye (`keepalive`), but the timeout is the
real safety net because browsers do not always deliver unload events.

The state machine (`backend/app/lifecycle/monitor.py`):

```
serving            at least one session is beating
counting_down      nobody is active; the grace period is running
waiting_for_work   the grace period expired but real work is still in flight
held               a reason that never expires forbids stopping
shutting_down      a clean exit has been requested (cancellable for a moment)
always_on          the feature is switched off
```

Only a running correction, a report being written, a large-document job, an
automation step holding a live lease, an open database write transaction or an
in-flight file operation can delay the stop, and only up to
`max_shutdown_wait_seconds`. After that the process still stops cleanly — which
is the documented safe checkpoint: boot recovery requeues in-flight corrections
and long documents on the next start, exactly as it does after a crash.

### The stop is a shutdown, not a kill

The monitor does not kill anything. It sets uvicorn's `should_exit`, so uvicorn
finishes in-flight requests and runs the application's normal lifespan shutdown:
worker pool drained, schedulers stopped, tunnel stopped, temp workspaces
cleaned, database closed. The relay sees the child exit with code 0 and goes
back to sleep. That is why an idle stop cannot corrupt a job, a result or a
setting.

### Reconnection

* A heartbeat arriving during the countdown **cancels** it, and one arriving
  inside the last-chance window also cancels an already-requested exit.
* A heartbeat that fails is retried by the UI (3 s, backing off to 10 s) before
  the page admits the app is away — one dropped request is not evidence.
* When the app comes back, the page says so and the shell re-reads the session
  and the dashboard immediately rather than waiting out its own interval.
* A hidden tab keeps beating, just less often — up to twice the gap, never later
  than 60 % of the session timeout, because slowing it past the timeout would
  let the app stop under a teacher who is coming back to it.

---

## 2. Configuration

`data/config.toml` (all times in seconds):

```toml
[lifecycle]
enabled = true                  # stop the app when nobody is using it
supervisor = true               # start it again automatically when the site is opened
heartbeat_seconds = 30          # how often each tab says it is still there
session_timeout_seconds = 90    # a silent tab counts as gone after this
idle_grace_seconds = 120        # keep running this long after the last session ends
max_shutdown_wait_seconds = 900 # limit on waiting for jobs to reach a safe point
last_chance_seconds = 5         # window in which a returning visitor cancels the exit
backend_port = 0                # 0 = pick the first free port above the public one
```

The same values are editable in **Settings → Server lifecycle**, where the card
also shows the *real* state (`serving` / `counting down` / `held`) read from the
running backend every 5 s. Values are clamped identically in the file and in the
screen (heartbeat ≥ 5 s, session timeout ≥ 2 × heartbeat, grace ≤ 24 h), so the
page cannot accept what the file rejects. `enabled` applies within a few
seconds; `supervisor` is a process and needs a restart — the UI says so.

Turning `enabled` off restores the old behaviour exactly: the app runs until
somebody stops it. Turning `supervisor` off leaves the idle stop in place, and a
stopped app then stays stopped until it is started by hand.

### Command line

| command | what it does |
|---|---|
| `python main.py` | normal launch: the relay (or the plain server if the relay is off) |
| `python main.py --supervise` | the relay explicitly, in the foreground |
| `python main.py --serve PORT` | the backend only, on a private port (the relay uses this) |
| `python main.py --stop` | stops the running instance (unchanged) |

`start.bat` and `Stop.bat` are unchanged in use. The relay records itself — not
the child — in `data/instance.json` on the **public** port, so Stop.bat stops
the whole tree rather than half of it.

---

## 3. Behaviour the tests prove

`backend/tests/test_lifecycle.py` (34 tests, all fast, no sleeps)

* two tabs are two sessions; closing one while another is open changes nothing;
* a silent session expires and is removed;
* the countdown starts only when the last session goes;
* returning during the countdown cancels it, and the app then stays up for
  dozens of intervals;
* a heartbeat inside the last-chance window takes back a requested exit;
* the shutdown happens after the grace period and is requested exactly once;
* a running correction / report / long document defers it, and a wedged
  operation is capped by `max_shutdown_wait_seconds` instead of pinning the app
  open forever;
* a live public address **holds** the app up with no timer at all;
* `enabled = false` means always on, and a live Settings change takes effect on
  the next tick;
* an open `db.transaction()` counts as critical, a failed one does not leak the
  count, and a source that cannot be read counts as *busy*, not *clear*;
* the heartbeat/status/leave API works, a status poll never counts as a user,
  absurd session ids are refused, and the block is validated exactly like the
  config file;
* the monitor is started **and stopped** by the application lifespan, and the
  exit request is attachable and cancellable.

`backend/tests/test_lifecycle_supervisor.py` (13 tests, real relay, real child)

* a second relay cannot own the port (no duplicate servers);
* a status probe does **not** start the backend, a page load does;
* eight simultaneous requests start exactly ONE backend;
* a request that arrives during start-up is held until the app is ready;
* a sleeping backend is started again for the next visitor;
* exit code 0 is a sleep (no back-off), a crash is remembered, and three fast
  failures in a row trigger the back-off instead of a start/stop loop;
* a backend that never answers gets a 503 with a sentence, not a hung tab;
* the relay stays stdlib-only and records itself as the instance.

`backend/tests/test_lifecycle_end_to_end.py` (1 test, ~30 s, the real app)
boots `main.py --serve` behind the relay on a scratch install with second-scale
timings and performs the whole cycle: two sessions → close one → still up →
close the last → countdown → return → cancelled → leave → the app exits by
itself with code 0 → the data and settings are intact → a status probe does not
wake it → opening the site starts it again → a new session works.

Whole backend suite as it stands in this checkout (1651 tests collected):

```
3 failed, 1648 passed, 2 warnings in 382.74s (0:06:22)
```

The three failures are **not** from this work and are not caused by it — they
are in another session's uncommitted edit to
`backend/tests/test_classroom_error_reporting.py`:

* that file now defines the helper `_seed_coursework` **twice** (line 46 and
  line 435, the second one added by the newer block). The later definition wins
  at import, so `TestStaleCourseworkIsArchived` seeds the wrong course and
  assignment ids and trips an unrelated foreign-key error;
* running the file exactly as committed (`git show HEAD:` …) gives
  **14 passed**, which is how this was isolated. Renaming one of the two
  helpers fixes it; nothing else in the suite is affected.

### Verified live, through the real launcher

Beyond the suite, `main.py --supervise` was run for real on a scratch install
and a scratch port, and the console log plus HTTP checks showed the whole story:

```
[20:00:05] starting the backend on port 8711 (pid 3716)
INFO lifecycle: idle monitor started        # the app booted behind the relay
[20:00:09] backend ready
[20:00:09] relay listening on http://127.0.0.1:8710 (backend port 8711)
INFO lifecycle: idle countdown started      # nobody connected
INFO lifecycle: idle shutdown — no active sessions and no work
INFO lifecycle: shutdown requested; workers: pool stopped; app: stopped
```

* `GET /api/health` then answered from the relay — `{"status": "ok",
  "backend": "stopped"}` — and `GET /api/lifecycle/status` reported
  `state: sleeping`. **Neither started the backend.**
* A heartbeat started it again (held until ready) and reported
  `state: serving`, `backend: running`.
* `GET /` served the real built interface through the relay.
* A second launch printed *"already running … not starting a second copy"* and
  exited 0 — and its probe did not wake the app either.
* Two sessions, then closing them one at a time: the app stayed up after the
  first and went back to sleep after the second.
* `main.py --stop` (what Stop.bat runs, with no port argument) stopped the
  **whole** tree — relay and child — removed `instance.json`, freed both ports,
  and left `app.db`, `config.toml` and the logs untouched.

### Verified live, in a real browser

With the app running behind the relay on a scratch install, the Settings page
was opened in a real Chromium tab and watched:

* the heartbeat fires every `heartbeat_seconds` with a per-tab id and answers
  **200 while the sign-in screen is showing** (the lifecycle paths stay
  reachable without a session, which is what stops the app sleeping under a
  teacher who is on the login page). `/api/dashboard` and `/api/attention`
  answer 401 there as they always did — the heartbeat is the only lifecycle call
  involved;
* the backend reports `serving` with `active_sessions: 1` for that tab;
* the **Server lifecycle** card read its real values (heartbeat 5, timeout 15,
  grace 8) and, as the session ended, moved through
  *“Nobody is using the app — countdown running”* → *“Stopping now”*, with the
  countdown text counting down to 0 s;
* with the whole app stopped, the page showed the *Reconnecting to Math
  Checker…* banner and recovered by itself once the app was started again.

One behaviour worth knowing, seen live: a tab that is still open but whose
session was dropped (or expired while the browser had it suspended) wakes the
app with its own page polls, which then finds no session and idles out again.
With the shipped defaults this does not last: the resumed tab's heartbeat lands
within a second of the wake, so the countdown is cancelled long before the
120 s grace period ends. It is only visible when the grace period is set to a
few seconds, or when session and heartbeat values are pushed far apart.

### Run the lifecycle tests

```
python -m pytest backend/tests/test_lifecycle.py backend/tests/test_lifecycle_supervisor.py -q
python -m pytest backend/tests/test_lifecycle_end_to_end.py -q   # real backend, ~35 s
```

---

## 4. Deployment requirements and limits

* **No new infrastructure.** No service manager, no external process supervisor,
  no cloud dependency. The relay is a child of the same console window the app
  has always run in: closing that window still stops everything.
* **The install must keep the relay running** for the on-demand start to work.
  The existing autostart/boot task (`setup.bat` → `install_autostart.py`) now
  supervises the relay instead of a backend that exits when idle, which is what
  you want — but it does mean a small process is resident after an idle stop.
  Set `supervisor = false` if you would rather have no resident process.
* **A published Public Address holds the app up** for as long as it is
  published. A visitor from the internet cannot start a process on this
  computer, so stopping would break the link with no way back. This is
  deliberate and is reported in the UI (`held`) rather than hidden.
* **LAN mode** works, but the LAN address belongs to the relay, and the backend
  itself stays bound to loopback.
* **The packaged copies under `MathChecker_Distribution/` still contain the
  previous interface and code.** Rebuild the portable package from this checkout
  for the feature to reach a teacher's install.
* **Long-running work still ends.** If a job runs longer than
  `max_shutdown_wait_seconds` with nobody connected, the app stops at that
  checkpoint and the job resumes on the next start (this was already the
  documented recovery behaviour).
* **Heartbeats are not persisted.** The session registry is in memory by design:
  a heartbeat every 30 s per tab must not grow the database or take the write
  lock. After a restart every session is gone, which is correct — the process
  that held them is gone with it.

## 5. Files

Added:

* `backend/app/lifecycle/__init__.py` — package overview
* `backend/app/lifecycle/sessions.py` — in-memory per-tab session registry
* `backend/app/lifecycle/activity.py` — critical-operation counter
* `backend/app/lifecycle/busy.py` — the real busy/hold sources
* `backend/app/lifecycle/monitor.py` — the state machine and status snapshot
* `backend/app/lifecycle/control.py` — how the process asks itself to exit
* `backend/app/lifecycle/routes.py` — heartbeat / leave / status API
* `backend/app/lifecycle/supervisor.py` — the on-demand relay
* `frontend/src/lifecycle.tsx` — heartbeat hook and reconnecting banner
* `backend/tests/test_lifecycle.py`, `test_lifecycle_supervisor.py`,
  `test_lifecycle_end_to_end.py`
* this document

Changed:

* `main.py` — `--serve` / `--supervise`, the relay as the default launch, the
  graceful-exit hook
* `backend/app/main.py` — start/stop the monitor in the lifespan, register the API
* `backend/app/config.py` — `[lifecycle]` block + one shared clamping helper
* `backend/app/db.py` — write transactions register as critical operations
* `backend/app/api/routes_all.py` — the `lifecycle` block in Settings
* `backend/app/auth_gate.py` — the lifecycle paths stay reachable before sign-in
  (the login screen is a browser session too)
* `frontend/src/api.ts` — lifecycle types, heartbeat/leave/status calls
* `frontend/src/App.tsx` — the banner, and a re-read of the shell state when the
  app returns
* `frontend/src/pages/SettingsPage.tsx` — the Server lifecycle card
* `frontend/src/styles.css` — the reconnecting banner
