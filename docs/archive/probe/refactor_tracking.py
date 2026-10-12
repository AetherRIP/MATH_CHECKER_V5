"""One-shot refactor for tracking_routes.py.

Moves the two nested SSE generators to module level, de-indented, converted
from `yield _sse(x)` (SSE string) to `report(x)` (raw dict), and replaces the
route bodies with thin registry-backed wrappers.  Written to disk and run once.
"""

from __future__ import annotations

import ast
import re
import sys

PATH = "backend/app/classroom/tracking_routes.py"

with open(PATH, encoding="utf-8") as fh:
    src = fh.read()
lines = src.split("\n")


def find_headers() -> list[int]:
    return [i for i, l in enumerate(lines) if l == "    def gen():"]


def matching_paren(text: str, open_idx: int) -> int:
    """Index of the ')' matching text[open_idx] == '(', string-aware."""
    depth = 0
    i = open_idx
    quote = None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError("unbalanced parens")


def convert(body_lines: list[str]) -> list[str]:
    """De-indent by 4 and turn each `yield _sse(<dict>)` into `report(<dict>)`."""
    text = "\n".join(raw[4:] if raw.startswith("    ") else raw
                     for raw in body_lines)
    pos = 0
    while True:
        m = re.compile(r"yield _sse\(").search(text, pos)
        if not m:
            break
        text = text[: m.start()] + "report(" + text[m.end():]
        pos = m.start() + len("report(")
    assert "yield" not in text, "a bare yield survived:\n" + text
    assert "_sse(" not in text
    return text.split("\n")


blocks = find_headers()
if len(blocks) != 2:
    sys.exit(f"expected 2 generators, found {len(blocks)}")

ASSIGNMENT_HEAD = '''def _assignment_work(course_id: str, coursework_id: str, report) -> None:
    """Roster + every submission state for ONE assignment (§12/§17).

    The SAME staged work this endpoint has always done (roster → submission
    pages → save); the only change is that each frame goes to ``report``
    instead of being yielded, so one implementation now feeds both the SSE
    stream and the polling routes. On API failure the stored rows stay
    untouched and the failure is reported as an honest ``error`` frame.
    """
'''

ASSIGNMENT_ROUTE = '''@router.api_route("/classroom/courses/{course_id}/assignments/{coursework_id}/sync",
                  methods=["GET", "POST"])
async def sync_tracking(course_id: str, coursework_id: str):
    """Full-state tracking sync with REAL progress (SSE).

    GET as well as POST: the browser's EventSource can only issue GETs —
    registering POST-only made the UI's "Sync Google Classroom" button 404
    silently (the reason assignments looked empty).

    The work runs on a worker thread and streams through the progress
    registry, so progress also survives a tunnel/proxy that buffers an event
    stream (see ``sync_runs`` — the reason this page could sit on
    "Connecting…" for the entire sync).
    """
    _require_classroom()
    run, started = sync_runs.start(
        "assignment", {"course_id": course_id, "coursework_id": coursework_id},
        lambda report: _assignment_work(course_id, coursework_id, report))
    return _stream_run(run, attach=not started)
'''

FULL_HEAD = '''def _full_work(report) -> None:
    """FULL Google Classroom synchronization with real staged progress.

    Flow (§3/§22): verify/refresh OAuth → courses → assignments → students
    (roster per course, fetched ONCE) → submissions for every assignment
    (all states + grades + attachments) → local DB (idempotent upserts) →
    done. Progress frames mirror the actual API calls being made.

    Failure honesty (§22): a fatal step (auth/courses) reports ``error`` with
    the failing stage — never ``done``. A per-part failure (one course/one
    assignment) collects into ``summary.errors`` and the ``done`` frame is
    marked ``partial`` so the UI never claims full success.
    """
'''

FULL_ROUTE = '''@router.api_route("/classroom/sync", methods=["GET", "POST"])
async def full_sync():
    """Run the full sync, streaming its progress as SSE.

    Delegates to the progress registry so the identical progress can also be
    polled (``/classroom/sync/start`` + ``/classroom/sync/progress``) by a
    client whose stream is buffered by a tunnel or proxy.
    """
    _require_classroom()
    run, started = sync_runs.start("full", {}, _full_work)
    return _stream_run(run, attach=not started)
'''

# Process the LAST block first so earlier indices stay valid.
for which, head, route in ((1, FULL_HEAD, FULL_ROUTE),
                           (0, ASSIGNMENT_HEAD, ASSIGNMENT_ROUTE)):
    gen_at = blocks[which]
    # The enclosing route starts at its @router decorator above the generator.
    route_at = gen_at
    while not lines[route_at].startswith("@router.api_route("):
        route_at -= 1
    # ...and ends at the StreamingResponse return.
    ret_at = gen_at
    while not lines[ret_at].startswith("    return StreamingResponse(gen(),"):
        ret_at += 1
    body = convert(lines[gen_at + 1:ret_at])
    replacement = head.split("\n")[:-1] + body + [""] + route.split("\n")[:-1] + [""]
    lines[route_at:ret_at + 2] = replacement

new = "\n".join(lines)
with open(".probe/new_tracking.py", "w", encoding="utf-8", newline="") as fh:
    fh.write(new)
try:
    ast.parse(new)
except SyntaxError as exc:
    print("PARSE ERROR:", exc)
    show = new.split("\n")
    for i in range(max(0, exc.lineno - 14), min(len(show), exc.lineno + 3)):
        print(f"{i + 1:>4}|{show[i]}")
    sys.exit(1)
with open(PATH, "w", encoding="utf-8", newline="") as fh:
    fh.write(new)
print("rewrote", PATH)
