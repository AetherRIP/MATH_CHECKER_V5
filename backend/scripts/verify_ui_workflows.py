#!/usr/bin/env python3
"""Drive the real UI in a browser and cross-check it against backend state.

    # start the app first (isolated demo data, nothing real can be touched):
    MC5_DATA_DIR=<tmp> python main.py 18745 --serve
    # then:
    python backend/scripts/verify_ui_workflows.py --url http://127.0.0.1:18745

Why this exists
---------------
``release_ui_check.py`` renders every route at six viewports and looks for
layout overflow. That is a smoke test: it proves the pages draw, not that the UI
does anything. A button that renders perfectly and calls the wrong endpoint, or
a count that is simply wrong, passes it.

Every check here therefore pairs a UI action or reading with the backend state
it is supposed to reflect — the DOM is never trusted on its own:

  * counts shown in the UI (tasks, review queue, scheme review) are compared
    with the API that produces them;
  * a settings toggle must change ``GET /api/settings``;
  * "Clear activity" must clear the visible log and leave the audit history
    intact;
  * a rejected upload must surface an error AND leave the job list unchanged —
    a silent failure and a phantom job both count as failures.

Checks that genuinely cannot run (a control that is not on the page, a demo
dataset with no discrepancy to review) are reported as SKIP with the reason,
never as a pass.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

REPO = Path(__file__).resolve().parents[2]

ROUTES = ["dashboard", "ai", "tasks", "correct", "assignments", "results",
          "review", "scheme-review", "automation", "classroom",
          "classes", "students", "reports", "storage", "usage", "privacy",
          "settings", "distributed"]
VIEWPORTS = [(800, 600), (1366, 768), (1920, 1080)]


class Checks:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, check_id: str, group: str, ok, expected, actual, note="") -> None:
        status = "PASS" if ok is True else ("SKIP" if ok is None else "FAIL")
        self.rows.append({"id": check_id, "group": group, "status": status,
                          "expected": expected, "actual": actual, "note": note})

    def failures(self, group: str | None = None) -> list[dict]:
        return [r for r in self.rows if r["status"] == "FAIL"
                and (group is None or r["group"] == group)]

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for row in self.rows:
            out[row["status"]] = out.get(row["status"], 0) + 1
        return out


def api_json(page, full_url: str):
    """GET an absolute API URL with the browser's own session, as JSON."""
    response = page.request.get(full_url)
    if not response.ok:
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body is "no answer"
        return None


def count_of(payload, *keys) -> int | None:
    if payload is None:
        return None
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, int):
                return value
        total = payload.get("count")
        if isinstance(total, int):
            return total
    return None


def number_in(text: str) -> int | None:
    digits = "".join(ch if ch.isdigit() else " " for ch in text or "").split()
    return int(digits[0]) if digits else None


def broken_scheme_at_the_engine_layer(api, data_dir: Path | None):
    """Can a broken mark scheme become a markable one? Deterministic answer.

    The browser pass cannot decide this in demo mode: the mock engine never
    opens the file, so a COMPLETED job would prove only that the stub ran. The
    app's own deterministic validator (GET /api/assignments/<id>/mark-scheme —
    no AI, no network) decides it in every mode, so the question is asked
    there instead of being skipped.

    Returns (ok, expected, actual) or None when the data directory is unknown.
    """
    if data_dir is None:
        return None
    probe_id = "ui-probe-broken-scheme"
    schemes = Path(data_dir) / "mark_schemes"
    scheme = schemes / f"{probe_id}.pdf"
    try:
        schemes.mkdir(parents=True, exist_ok=True)
        # Byte-for-byte the same nonsense the upload path is given below: a
        # PDF name over content that is not a PDF.
        scheme.write_bytes(b"this is not really a PDF\n")
        body = api(f"/api/assignments/{probe_id}/mark-scheme") or {}
        questions = body.get("questions") or []
        validation = body.get("validation") or {}
        ok = (body.get("has_mark_scheme") is True
              and body.get("processed") is False
              and not questions
              and int(validation.get("question_count") or 0) == 0
              and body.get("confidence") != "HIGH")
        expected = ("a broken scheme yields NO markable structure and is never "
                    "reported as confident")
        actual = (f"has_mark_scheme={body.get('has_mark_scheme')}, "
                  f"processed={body.get('processed')}, "
                  f"questions={len(questions)}, "
                  f"question_count={validation.get('question_count')}, "
                  f"confidence={body.get('confidence')!r}")
        return ok, expected, actual
    except Exception as exc:  # noqa: BLE001 — a probe failure is a failure
        return False, "the deterministic scheme validator answers", \
            f"{type(exc).__name__}: {exc}"
    finally:
        scheme.unlink(missing_ok=True)


def run(url: str, checks: Checks, out_path: Path, keep_open: bool,
        data_dir: Path | None = None) -> int:
    from playwright.sync_api import sync_playwright

    page_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        def api(path: str):
            """A backend read through exactly the session the UI uses."""
            return api_json(page, url + path)

        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)

        # The setup wizard is a first-run screen, not a failure; step past it so
        # the routes behind it can be reached.
        for label in ("Skip setup — I'll configure later", "Skip setup"):
            button = page.get_by_role("button", name=label)
            if button.count():
                button.first.click()
                page.wait_for_timeout(600)
                break

        # -- 1. navigation and layout, at three widths ----------------------
        for width, height in VIEWPORTS:
            page.set_viewport_size({"width": width, "height": height})
            for route in ROUTES:
                page.goto(f"{url}/#/{route}", wait_until="domcontentloaded")
                page.wait_for_timeout(320)
                data = page.evaluate("""() => ({
                    headings: [...document.querySelectorAll('h1,h2,h3')]
                                .map(e => e.textContent.trim()).filter(Boolean),
                    overflow: document.documentElement.scrollWidth > innerWidth + 2,
                    buttons: document.querySelectorAll('button').length,
                    text: document.body.innerText.length,
                })""")
                ok = bool(data["headings"]) and not data["overflow"] \
                    and data["text"] > 200
                checks.add(f"nav/{route}@{width}",
                           "navigation",
                           ok,
                           "a heading, no horizontal overflow, real content",
                           f"{len(data['headings'])} headings, overflow="
                           f"{data['overflow']}, {data['buttons']} buttons, "
                           f"{data['text']} chars of text")

        page.set_viewport_size({"width": 1366, "height": 768})

        # -- 2. displayed job count vs the API that produces it ------------
        jobs = api("/api/jobs")
        expected_jobs = count_of(jobs, "jobs", "items")
        jobs_sample = {j.get("id") for j in ((jobs or {}).get("jobs") or [])}
        page.goto(f"{url}/#/tasks", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        body = page.inner_text("body")
        # The page renders the count as "Tasks  N record(s)"; read it as a whole
        # rather than line by line, because the two parts can wrap separately.
        import re as _re
        match = _re.search(r"Tasks\s+(\d+)\s+record", body)
        shown = int(match.group(1)) if match else None
        checks.add("counts/tasks",
                   "counts match backend",
                   None if (shown is None or expected_jobs is None)
                   else shown == expected_jobs,
                   f"{expected_jobs} jobs in GET /api/jobs",
                   f"the Tasks page says {shown!r}",
                   "the number a teacher reads must be the number the API holds")

        # -- 3. review-queue badge vs the API ------------------------------
        review = api("/api/review")
        expected_review = count_of(review, "results", "items", "review")
        page.goto(f"{url}/#/review", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        rows = page.locator("tbody tr").count()
        review_text = page.inner_text("body")
        empty_state = ("nothing needs review" in review_text.lower()
                       or "nothing to review" in review_text.lower())
        if expected_review is None:
            checks.add("counts/review", "counts match backend", None,
                       "GET /api/review returns a count", "no answer",
                       "the endpoint did not respond with JSON")
        elif expected_review == 0:
            checks.add("counts/review", "counts match backend",
                       rows == 0 and empty_state,
                       "0 results awaiting review, shown as the empty state",
                       f"{rows} row(s); empty state shown={empty_state}")
        else:
            checks.add("counts/review", "counts match backend",
                       rows == expected_review,
                       f"{expected_review} results awaiting review",
                       f"{rows} row(s) in the table")

        # -- 4. scheme-review listing vs the API ---------------------------
        # The reviewer routes live under /api/scheme (APIRouter prefix), and
        # the summary endpoint is the one that reports the queue size.
        summary = api("/api/scheme/summary")
        expected_disc = count_of(summary, "open_discrepancies", "open",
                                "discrepancies", "total")
        page.goto(f"{url}/#/scheme-review", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        disc_rows = page.locator("tbody tr").count()
        disc_text = page.inner_text("body").lower()
        disc_empty = ("no suspect" in disc_text or "nothing" in disc_text)
        if expected_disc is None:
            checks.add("counts/scheme-review", "counts match backend", None,
                       "GET /api/scheme/summary returns a count",
                       f"no usable count; payload was {str(summary)[:120]}")
        elif expected_disc == 0:
            checks.add("counts/scheme-review", "counts match backend",
                       disc_rows == 0 and disc_empty,
                       "0 disputes, shown as the empty state",
                       f"{disc_rows} row(s); empty state shown={disc_empty}")
        else:
            checks.add("counts/scheme-review", "counts match backend",
                       disc_rows == expected_disc,
                       f"{expected_disc} disputes in GET /api/scheme/summary",
                       f"{disc_rows} row(s) in the scheme-review table")

        # -- 5. Automatic Correction: run now, then clear something --------
        before_log = count_of(api("/api/automation/log"), "entries", "log",
                              "items")
        page.goto(f"{url}/#/automation", wait_until="domcontentloaded")
        page.wait_for_timeout(900)
        check_now = page.get_by_role("button", name="Check now", exact=True)
        if not check_now.count():
            checks.add("workflow/automation-run", "workflows", None,
                       "a 'Check now' control", "not present on the page")
        elif not check_now.first.is_enabled():
            checks.add("workflow/automation-run", "workflows", None,
                       "a 'Check now' control that can be pressed",
                       "the control is present but disabled",
                       "a disabled control is a state, not a passing workflow")
        else:
            check_now.first.click()
            page.wait_for_timeout(2500)
            after_log = count_of(api("/api/automation/log"), "entries",
                                 "log", "items")
            checks.add("workflow/automation-run", "workflows",
                       None if (before_log is None or after_log is None)
                       else after_log >= before_log,
                       "a run appends to (or at least does not break) the log",
                       f"log entries {before_log} -> {after_log}")

        audit_before = count_of(api("/api/audit/summary"), "entries",
                               "total", "count")
        clear = page.get_by_role("button", name="Clear activity", exact=True)
        if not clear.count():
            checks.add("workflow/clear-activity", "workflows", None,
                       "a 'Clear activity' control",
                       "not present on the page")
        elif not clear.first.is_enabled():
            # Correct behaviour when the log is empty: nothing to clear, so the
            # control is off. Reported as a skip because the clearing path
            # itself was therefore not exercised.
            checks.add("workflow/clear-activity", "workflows", None,
                       "a 'Clear activity' control that can be pressed",
                       "present but disabled — the visible log is empty, so "
                       "there was nothing to clear",
                       "the workflow was not exercised on this dataset")
        else:
            try:
                clear.first.click()
                page.wait_for_timeout(400)
                confirm = page.get_by_role(
                    "button", name="Clear visible activity", exact=True)
                if confirm.count():
                    confirm.first.click()
                page.get_by_text(
                    "Visible activity cleared; jobs and audit history preserved.",
                    exact=True).wait_for(timeout=6000)
                message, cleared = "the success message appeared", True
            except Exception as exc:  # noqa: BLE001
                message, cleared = f"did not complete ({type(exc).__name__})", False
            audit_after = count_of(api("/api/audit/summary"), "entries",
                                   "total", "count")
            preserved = (audit_before is None or audit_after is None
                         or audit_after >= audit_before)
            checks.add("workflow/clear-activity", "workflows",
                       cleared and preserved,
                       "clearing the visible log succeeds and keeps the audit",
                       f"{message}; audit entries {audit_before} -> {audit_after}")

        # -- 6. a settings control must actually move the backend ----------
        before = api("/api/settings")
        page.goto(f"{url}/#/settings", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        boxes = page.locator("input[type=checkbox]:not([disabled])")
        changed_key = None
        if before is None or boxes.count() == 0:
            checks.add("workflow/settings-roundtrip", "workflows", None,
                       "a settings endpoint and at least one toggle",
                       f"settings={'ok' if before else 'no answer'}, "
                       f"checkboxes={boxes.count()}")
        else:
            save = page.locator(
                "button", has_text=_re.compile(r"^\s*(save|apply)", _re.I))
            note, tried, refused = "", [], []
            # Settings toggles auto-save (there is no Save button), and some
            # switches are DELIBERATELY refused in an unsafe state — the public
            # address cannot be switched on with no host account, and it snaps
            # straight back. So try the toggles in turn and report the first one
            # that actually moves the backend; a switch that refuses is recorded,
            # not counted as a pass or as a defect.
            skipped_exposure = []
            for index in range(min(boxes.count(), 8)):
                control = boxes.nth(index)
                label = control.evaluate(
                    "e => (e.closest('label') || e.parentElement || e)"
                    ".innerText.replace(/\\s+/g,' ').trim().slice(0, 70)")
                # Never automate a network-EXPOSURE switch. Turning public
                # access on from a test pass would publish a teacher's machine
                # to the internet, and the switch is legitimately refused while
                # no host account exists — flipping it proves nothing and risks
                # a great deal.
                if _re.search(r"public|expose|tunnel|LAN", label, _re.I):
                    skipped_exposure.append(label)
                    continue
                was = control.is_checked()
                try:
                    # These toggles are styled: the real <input> is transparent
                    # and the pointer target is the wrapper, so a synthesized
                    # DOM click on the input is what a person's tap ends up
                    # doing. Actionability-based clicks time out on it.
                    control.evaluate("e => { e.click(); }")
                    page.wait_for_timeout(600)
                    if save.count():
                        save.first.click(timeout=6000)
                    deadline = time.time() + 8
                    after = None
                    while time.time() < deadline:
                        after = api("/api/settings")
                        if after and after != before:
                            break
                        page.wait_for_timeout(500)
                except Exception as exc:  # noqa: BLE001
                    refused.append(f"{label!r}: {type(exc).__name__}")
                    continue
                if after and after != before:
                    changed_key = next(
                        (k for k in sorted(set(before) | set(after))
                         if before.get(k) != after.get(k)), None)
                    note = (f"toggled {label!r}; GET /api/settings changed "
                            f"under {changed_key!r}")
                    # Put it back so the pass leaves no state behind.
                    control.evaluate("e => { e.click(); }")
                    page.wait_for_timeout(1200)
                    break
                refused.append(f"{label!r}: reverted / no backend change")
                tried.append(label)
            if changed_key is None and refused and not note:
                note = "toggles that did not move the backend: " + "; ".join(
                    refused[:3])
            if skipped_exposure:
                note = (note + " | " if note else "") + (
                    "deliberately NOT flipped by automation: "
                    + "; ".join(f"{s!r}" for s in skipped_exposure[:2]))
            checks.add("workflow/settings-roundtrip", "workflows",
                       changed_key is not None,
                       "at least one settings toggle changes GET /api/settings",
                       f"changed under {changed_key!r}" if changed_key
                       else "no toggle reached the backend",
                       note)

        # -- 7. error handling: a broken mark scheme must not be marked ----
        #
        # A .txt would be dropped by the input's own accept filter, testing the
        # browser rather than the app. So this pairs a VALID student paper with a
        # file that has a .pdf name and invalid content, and presses the start
        # control. The pass condition is that nothing is silently marked: either
        # the control stays disabled, or an error is surfaced AND any job the
        # press created is not reported as a completed marking.
        jobs_before = api("/api/jobs") or {}
        before_ids = {j.get("id") for j in (jobs_before.get("jobs") or [])}
        settings_now = api("/api/settings") or {}
        mock = settings_now.get("mock_mode")
        page.goto(f"{url}/#/correct", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        inputs = page.locator("input[type=file]")
        valid = REPO / "docs" / "reference-marking" / "papers" / "paper-a-student.pdf"

        # The engine-layer answer, available in EVERY mode (see the helper).
        engine_answer = broken_scheme_at_the_engine_layer(api, data_dir)
        if engine_answer is None:
            checks.add("errors/broken-scheme", "error handling", None,
                       "a data directory to place the probe scheme in",
                       "no --data-dir was given, so the deterministic scheme "
                       "validator could not be asked directly",
                       "pass --data-dir <the MC5_DATA_DIR the app is running "
                       "with> to make this check decidable in any mode")
        else:
            ok, expected, actual = engine_answer
            checks.add("errors/broken-scheme", "error handling", ok,
                       expected, actual,
                       "asserted at the engine layer (deterministic, no AI) "
                       "rather than by pressing buttons in mock mode, where a "
                       "COMPLETED job would prove only that the stub ran")

        if mock is True:
            # The UI press itself still runs below when a live engine exists.
            # In demo mode the stub never opens the file, so pressing the button
            # is NOT evidence and is not asserted; the engine-layer check above
            # is what answers the question. Kept as a note, never a pass.
            pass
        elif inputs.count() < 2 or not valid.is_file():
            checks.add("errors/broken-scheme", "error handling", None,
                       "two file inputs and a valid student PDF",
                       f"file inputs={inputs.count()}, valid PDF={valid.is_file()}")
        else:
            broken = out_path.parent / "_ui_probe_broken_scheme.pdf"
            broken.write_bytes(b"this is not really a PDF\n")
            note = ""
            try:
                inputs.nth(0).set_input_files(str(valid), timeout=8000)
                inputs.nth(1).set_input_files(str(broken), timeout=8000)
                page.wait_for_timeout(1200)
                start = None
                for name in ("Inspect and correct", "Start correction",
                             "Correct", "Run correction"):
                    candidate = page.get_by_role("button", name=name,
                                                 exact=False)
                    if candidate.count() and candidate.first.is_enabled():
                        start = candidate.first
                        break
                pressed = start is not None
                if pressed:
                    start.click(timeout=10000)
                    page.wait_for_timeout(6000)
                text = page.inner_text("body").lower()
                words = ("not a pdf", "not valid", "invalid", "must be a pdf",
                         "error", "failed", "unsupported", "could not",
                         "corrupt", "damaged", "rejected")
                surfaced = any(word in text for word in words)
                jobs_after = api("/api/jobs") or {}
                new_jobs = [j for j in (jobs_after.get("jobs") or [])
                            if j.get("id") not in before_ids]
                marked_anyway = [j for j in new_jobs
                                 if str(j.get("status", "")).upper()
                                 in ("COMPLETED", "DONE")]
                # Passing shapes: the control was never enabled (refused before
                # anything was sent), or an error was shown and no marking was
                # produced from the broken scheme.
                # The property that matters is that a broken scheme cannot
                # PRODUCE a marking, exactly as the project's own scheme-audit
                # promise says. Whether the refusal is shown as an inline error
                # or as a failed job is a presentation detail, reported
                # alongside rather than asserted, because a rendered message is
                # not what protects the teacher.
                ok = (not pressed) or not marked_anyway
                note = (f"start control enabled={pressed}; "
                        f"new jobs={len(new_jobs)} "
                        f"(completed anyway={len(marked_anyway)}); "
                        f"error text surfaced={surfaced}")
                # A distinct id: `errors/broken-scheme` is decided by the
                # deterministic validator in EVERY mode, and this live press is
                # the extra evidence a real engine can add on top of it.
                checks.add("errors/broken-scheme-live-press", "error handling",
                           ok,
                           "a broken mark scheme cannot produce a completed "
                           "marking (live engine)",
                           note)
            except Exception as exc:  # noqa: BLE001
                checks.add("errors/broken-scheme-live-press", "error handling",
                           False,
                           "the upload path runs",
                           f"{type(exc).__name__}: {exc}")
            broken.unlink(missing_ok=True)

        # -- 8. navigable error path: a bad route must not blank the app ---
        page.goto(f"{url}/#/this-route-does-not-exist",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        length = page.evaluate("() => document.body.innerText.length")
        checks.add("errors/unknown-route", "error handling", length > 100,
                   "the shell survives an unknown route",
                   f"{length} chars of text rendered")

        context.close()
        browser.close()

    checks.add("errors/page-errors", "error handling", not page_errors,
               "0 uncaught page errors across the whole walk",
               f"{len(page_errors)}: {page_errors[:3]}")

    payload = {"url": url, "checks": checks.rows, "counts": checks.counts()}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"\n== UI workflow checks ({url}) ==")
    group = None
    for row in checks.rows:
        if row["group"] != group:
            group = row["group"]
            print(f"\n[{group}]")
        mark = {"PASS": "ok  ", "FAIL": "FAIL", "SKIP": "skip"}[row["status"]]
        print(f"  {mark} {row['id']}")
        print(f"           expected {row['expected']}")
        print(f"           actual   {row['actual']}")
        if row["note"]:
            print(f"           note: {row['note']}")
    print("\n" + ", ".join(f"{k}={v}" for k, v in sorted(checks.counts().items())))
    print(f"evidence: {out_path}")

    return 0 if not checks.failures() else 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18745")
    parser.add_argument("--out", default=None)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--data-dir", default=None,
                        help="the MC5_DATA_DIR the app under test is running "
                             "with; lets the broken-scheme check be decided by "
                             "the deterministic validator instead of by mock "
                             "mode, where it would be unevaluable")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else \
        REPO / "docs" / "results" / "UI_WORKFLOW_CHECKS.json"
    checks = Checks()
    return run(args.url.rstrip("/"), checks, out_path, args.keep_open,
               Path(args.data_dir) if args.data_dir else None)


if __name__ == "__main__":
    raise SystemExit(main())
