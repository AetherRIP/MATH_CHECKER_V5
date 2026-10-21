# V5 feature inventory → desktop migration

Every capability below exists in this checkout. Columns follow the brief:
feature, current location, dependencies, migration strategy, verification,
status.

Status values:

* **verified** — exercised against a running V5 backend from the desktop client
  (or by an automated test that calls the same code path).
* **implemented** — wired to the real endpoint, not yet exercised end-to-end.
* **blocked** — needs a credential/account this machine does not have.
* **not applicable** — browser-only plumbing with no desktop equivalent.

Endpoint paths are relative to `/api`. "Desktop view" names the WPF view.

## A. Application foundation

| Feature | Current location | Dependencies | Migration strategy | Verification | Status |
|---|---|---|---|---|---|
| Process entry / single instance | `main.py` (`probe_port`, `--stop`, `write_instance_record`) | stdlib socket | Reuse: desktop attaches when `/api/health` answers, else spawns `main.py --serve PORT` | `BackendHostTests`, live attach test | implemented |
| Configuration + secret store | `backend/app/config.py`, `data/config.toml`, `data/secrets.toml` | tomllib | Reuse (backend owns it); desktop keeps only its own paths/port in `%APPDATA%` | `DesktopSettingsTests` | verified |
| Data-folder resolution | `backend/app/data_location.py` | OS paths | Reuse; desktop passes `MC5_DATA_DIR` only when configured | manual run against demo data | verified |
| Health / readiness | `/health`, `main.py:open_ui_when_ready` | HTTP | Reuse; polled with timeout, surfaced in status bar | `BackendHostTests` (healthy/stall/exit) | verified |
| Logging + diagnostics | `backend/app/logging_setup.py`, desktop `Core/Diagnostics/FileLog` | none | New client log with secret redaction; backend log path shown on Diagnostics page | `LogRedactionTests` | verified |
| Graceful shutdown | `main.py serve()` finally, `lifecycle/control` | none | Desktop kills only the tree it started; temp/log handles flushed | `BackendHost.StopAsync` test + UI close walkthrough (client exits, no orphan `python.exe`) | verified |
| Startup wizard (first run) | `/setup/status`, `/setup/complete`, `pages/SetupWizard.tsx` | none | Native wizard: teacher name, school, keys, classroom note | live call on demo backend | implemented |

## B. Correction workflow

| Feature | Current location | Dependencies | Migration strategy | Verification | Status |
|---|---|---|---|---|---|
| Pipeline facts before upload | `/corrections/pipelines` | none | Reuse | live call | implemented |
| Unified correction upload (auto/standard/large) | `/corrections/unified`, `routes_all.py:unified_correction` | Gemini, PyMuPDF | Reuse via multipart; native file pickers + drag-drop | live multipart against demo backend | implemented |
| Manual correction (submission + scheme + paper) | `/corrections/manual` | Gemini | Reuse | live call shape only | implemented |
| Job queue + live stages | `/jobs`, `/jobs/stream` (SSE), `/jobs/{id}/cancel`, `/jobs/{id}/retry` | worker pool | Reuse; SSE reader with 2 s polling fallback | `SseParserTests`, poll test | implemented |
| Batch progress, attempts, errors | `workers/pool.py`, `jobs/manager.py` | none | Reuse (`/jobs`) | dashboard/job log | implemented |
| Per-question marks + feedback | `engine/legacy_marking.py` → `result_json`; `/results/{id}` | Gemini | Reuse; optional per-question table | live result detail | implemented |
| Teacher override / accept | `/results/{id}/override` | none | Reuse | partial-correction error path shown | implemented |
| Review queue | `/review`, `pages/ReviewPage.tsx` | none | Reuse | live call | implemented |
| Scheme discrepancies + teacher decisions | `/scheme/summary`, `/scheme/discrepancies/{id}`, `/decision`, `/propose` | SQLite | Reuse; dedicated `SchemeReviewView` with the backend's six decisions | UI walkthrough (summary + both grids + backend payload) | verified (empty state; no discrepancy exists in demo data) |
| Staged bulk-correction proposals (approve/reject) | `/scheme/proposals*` | none | Reuse; separate explicit approval step | UI walkthrough (empty state) | verified (empty state; nothing staged in demo data) |
| Remark + version history + decision | `/results/{id}/remark`, `/versions`, `/remark/decision` | Gemini | Reuse | live call | blocked (needs a Classroom submission) |
| Retry one downstream step | `/results/{id}/retry` | Classroom | Reuse | live 400/409 shown honestly | blocked (needs Classroom) |
| Export PDF / CSV / MD | `/results/{id}/export?format=` | reportlab/fpdf | Reuse; download to chosen folder, then ShellExecute | live download test | implemented |

## C. Classes, students, assignments, Classroom

| Feature | Current location | Dependencies | Migration strategy | Verification | Status |
|---|---|---|---|---|---|
| Class list + aggregates | `/classes` | SQLite | Reuse | live call | implemented |
| Class trend analytics | `/analytics/activity`, `/analytics/class/{id}/trend` | SQLite | Reuse | live call | implemented |
| Student list / profile | `/students`, `/students/{id}` | SQLite | Reuse; native search + class filter | live call + `StudentsPageTests` + UI walkthrough (5 rows) | verified |
| Global search | `/search` | SQLite | Reuse; one search box + result table | UI walkthrough (12 real matches) | verified |
| Nickname / class assignment | `/students/{id}/nickname`, `/details`, `/bulk-details` | none | Reuse | live save | implemented |
| Student report | `/students/{id}/report` | report renderer | Reuse | live call | implemented |
| Assignment list / detail | `/assignments`, `/assignments/{id}` | SQLite | Reuse | live call | implemented |
| Local assignment create | `/assignments/local` | none | Reuse | live create on demo data | implemented |
| Mark-scheme view / pages / override | `/assignments/{id}/mark-scheme*` | PyMuPDF | Reuse; page images saved and opened natively | live call | implemented |
| Attention feed | `/attention`, `frontend/src/attention.tsx` | none | Reuse | live call | verified |
| Classroom status / connect / disconnect | `/classroom/status`, `/connect`, `/disconnect` | Google OAuth | Reuse; connect opens the real consent URL | honest blocked state (no OAuth client here) | blocked |
| Classroom courses / assignments / submissions | `/classroom/courses*` | Google API | Reuse | needs connected account | blocked |
| Correct one submission / correct all | `/classroom/.../correct`, `/correct-all` | Gemini + Google | Reuse with confirmation | needs connected account | blocked |
| Classroom sync + progress + tracking | `classroom/tracking_routes.py` | Google API | Reuse | needs connected account | blocked |
| Push draft grades | `/classroom/grades/push` | Google API | Reuse | needs connected account | blocked |
| Attach report / cleanup | `/results/{id}/attach-report`, `/report-cleanup` | Google Drive | Reuse | needs connected account | blocked |

## D. Reports and documents

| Feature | Current location | Dependencies | Migration strategy | Verification | Status |
|---|---|---|---|---|---|
| Assignment report | `/reports/assignment/{id}`, `reporting/class_report.py` | SQLite | Reuse (render rows natively) | live call | implemented |
| Class report | `/reports/class/{id}` | SQLite | Reuse | live call | implemented |
| Custom report builder | `/reports/options`, `/reports/build` | SQLite | Reuse | live call | implemented |
| Report export | `/reports/*/export`, `/reports/build/export` | PDF renderer | Reuse; save + open | live download | implemented |
| File validation / size limits | `routes_all.py` (50/200 MB, suffixes) | none | Same limits enforced client-side before upload | unit test of validator | implemented |
| Temp-file cleanup | `engine/service.py:cleanup_job_files`, retention worker | none | Reuse (backend-owned) | not applicable to client | not applicable |

## E. Batched large-PDF worker

| Feature | Current location | Dependencies | Migration strategy | Verification | Status |
|---|---|---|---|---|---|
| Job list + config | `/large-pdf/jobs`, `/large-pdf/config` | SQLite | Reuse | live call | implemented |
| Create / process | `/large-pdf/jobs`, `/process` | PyMuPDF + Gemini | Reuse | live create on demo data | implemented |
| Batch retry / job retry / cancel | `/large-pdf/jobs/{id}/...` | none | Reuse | live call | implemented |
| Per-batch inspect/plan/merge detail | `large_pdf/runner.py`, `/large-pdf/jobs/{id}` | none | Reuse (display only) | live call | implemented |

## F. Automation

| Feature | Current location | Dependencies | Migration strategy | Verification | Status |
|---|---|---|---|---|---|
| Status + worker on/off | `/automation/status`, `/worker` | Classroom | Reuse | live call | implemented |
| Settings (interval, review mode, draft-only…) | `/automation/settings` | none | Reuse | live save | implemented |
| Assignment opt-in | `/automation/assignments` | Classroom | Reuse | live call | blocked (no courses) |
| Job list + approve/retry/reconcile | `/automation/jobs*` | none | Reuse | live call | implemented |
| Bulk upload | `/automation/bulk-upload*` | Drive | Reuse | needs Classroom | blocked |
| Grade boundaries | `/automation/grade-boundaries/{id}` | none | Reuse | live call | implemented |
| Activity log | `/automation/log` | none | Reuse | live call | implemented |

## G. System area

| Feature | Current location | Dependencies | Migration strategy | Verification | Status |
|---|---|---|---|---|---|
| Settings read/write | `/settings` (GET/POST) | TOML | Reuse; native form bound to the same keys | live save round-trip | implemented |
| Gemini key management | `/keys`, `/keys/{fp}/test|disable|re-enable|remove`, `/settings/gemini-keys` | Gemini | Reuse; test-all via SSE | live list + honest empty state | implemented |
| Key test stream | `/keys/test/stream` | Gemini | Reuse (SSE) | `SseParserTests` | implemented |
| Worker pool size | `/workers`, `/workers/resize` | none | Reuse | live call | implemented |
| Internet usage / bandwidth | `/usage` | none | Reuse | live call | implemented |
| Storage inventory / export / import | `/data/inventory`, `/data/export`, `/data/import*` | SQLite | Reuse; save/choose file | live export download + import preview | implemented |
| Privacy info / factory reset | `/privacy/info`, `/privacy/factory-reset`, `/diagnostic-export` | none | Reuse; reset requires typed confirmation | live call (reset NOT run against real data) | implemented |
| Diagnostics | `/diagnostics`, `/diagnostics/logs`, `/diagnostics/db`, `/diagnostics/selfcheck` | logs | Reuse; 403 shown as "developer role required" | live 403 handled | implemented |
| Prompts editor | `/dev/prompts` (GET/POST) | Gemini | Reuse; 403 handled | live 403 handled | implemented |
| Accuracy audit | `/audit/summary`, `/audit/reports*` | files | Reuse (read-only) | live "no audit recorded" state | implemented |
| Multi-school | `/distributed/capabilities`, `/schools`, `/teams`, `/accounts`, `/devices` | SQLite | Reuse; disabled state when `multi_account=false` | live capabilities call | implemented |
| Releases / updates | `/distributed/releases*`, `/updates*` | files | Reuse (read-only overview) | live call | implemented |
| Remote access + public address | `/remote/status`, `/access/state`, `/access/public-address` | tunnel | Reuse; toggle with confirmation | live read verified in the UI walkthrough; enabling the tunnel was **not** exercised | implemented (toggle unverified) |
| Sign-in (public mode) | `/auth/status`, `/auth/login`, `/auth/logout`, `SignIn.tsx` | session cookie | Reuse; cookie held in memory, CSRF header forwarded | local mode needs none; public mode unverified | blocked (no public-mode instance) |

## H. Browser-only plumbing (not migrated)

| Item | Location | Why not migrated |
|---|---|---|
| Hash router, `#/…` deep links, breadcrumbs | `frontend/src/App.tsx` | Replaced by native navigation |
| Theme/`localStorage` preferences | `theme.tsx`, `paneWidths.ts` | Desktop settings file |
| Chunk-reload recovery | `App.tsx: lazyRoute` | No code splitting in a single desktop binary |
| Search box / tooltip layer / toast host | `SearchBox.tsx`, `TooltipLayer.tsx`, `components.tsx` | WPF equivalents (search box wired to `/search`) |
| Service worker / PWA | `frontend/src/workers` | Desktop app is installed, not cached in a browser |

## Summary

* Backend endpoints consumed: **every route that a desktop workflow needs**;
  61 distributed routes and 11 repair routes are read-only or administrator
  surfaces and are only exposed where a screen exists (see above).
* Nothing in `backend/` was modified for this migration except where noted in
  `04-migration-status.md` (currently: nothing).
* Blocked items are all Google-credential dependent and are shown as blocked in
  the UI with the backend's own explanation, never as success.
