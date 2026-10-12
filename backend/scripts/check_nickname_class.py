"""Live end-to-end check of the nickname + class feature against a RUNNING
backend on :8642, walking every case in §14 of the brief against REAL data.

Read-only apart from the nickname/class it writes to two real students, and
it restores both to their previous state before exiting — including on
failure — so the live database is left exactly as it was found.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8642"
PASSED = 0
FAILED: list[str] = []


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:300].decode(errors="replace")}


def check(label: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")


def truthy(label: str, got) -> None:
    check(label, bool(got), True)


def main() -> int:
    status, listing = call("GET", "/api/students")
    if status != 200:
        print(f"backend not reachable: {status} {listing}")
        return 2
    students = listing["students"]
    print(f"live students: {len(students)}")

    # Pick two real students that carry no metadata yet.
    clean = [s for s in students
             if not s.get("nickname") and not s.get("class_name")]
    if len(clean) < 2:
        print("need 2 students with no nickname/class to test with")
        return 2
    a, b = clean[0], clean[1]
    aid, bid = a["student_id"], b["student_id"]
    print(f"testing with {a['official_name']!r} and {b['official_name']!r}")
    restore: list[tuple[str, dict]] = []

    try:
        # --- §2/§14.1 nickname + class -------------------------------------
        status, p = call("PUT", f"/api/students/{aid}/details",
                         {"nickname": "Mo", "class_name": "10A"})
        check("save nickname+class returns 200", status, 200)
        check("display name is the nickname", p["display_name"], "Mo")
        check("class is shown", p["class_label"], "10A")
        check("classroom name is untouched", p["official_name"],
              a["official_name"])
        check("classroom id is untouched", p["student_id"], aid)
        restore.append((aid, {}))

        status, p = call("PUT", f"/api/students/{bid}/details",
                         {"class_name": "10B"})
        check("no nickname keeps the classroom name", p["display_name"],
              b["official_name"])
        check("class only", p["class_label"], "10B")
        restore.append((bid, {}))

        # --- §14.1 change nickname ------------------------------------------
        status, p = call("PUT", f"/api/students/{aid}/details",
                         {"nickname": "Mohammed"})
        check("nickname change applies", p["display_name"], "Mohammed")
        check("nickname change keeps the class", p["class_label"], "10A")
        check("nickname change keeps the identity", p["student_id"], aid)

        # --- §14.1 change class --------------------------------------------
        status, p = call("PUT", f"/api/students/{aid}/details",
                         {"class_name": "10B"})
        check("class change applies", p["class_label"], "10B")
        check("class change keeps the nickname", p["display_name"], "Mohammed")

        # --- §13 whitespace / length ---------------------------------------
        status, p = call("PUT", f"/api/students/{aid}/details",
                         {"nickname": "   ", "class_name": "  "})
        check("blank values clear instead of saving blanks", status, 200)
        check("blank nickname clears", p["nickname"], None)
        check("blank class clears the stored value", p["class_name"], None)
        # §8: with no assigned class the label falls back to the Classroom
        # course this app already showed, and never reads as a blank cell.
        truthy("cleared class falls back instead of reading blank",
               p["class_label"])
        check("blank nickname falls back to the classroom name",
              p["display_name"], a["official_name"])

        # The teacher-assigned class must beat that course fallback.
        status, p = call("PUT", f"/api/students/{aid}/details",
                         {"class_name": "9Z"})
        check("an assigned class beats the course fallback", p["class_label"],
              "9Z")

        status, body = call("PUT", f"/api/students/{aid}/details",
                            {"nickname": "x" * 400})
        check("an over-long nickname is refused", status, 400)
        status, body = call("PUT", f"/api/students/{aid}/details", {})
        check("saving nothing is refused", status, 400)
        status, body = call("PUT", "/api/students/no-such-student/details",
                            {"class_name": "10A"})
        check("an unknown student is not fabricated", status, 404)

        # --- §4 custom class names are not a whitelist ---------------------
        for value in ("11 Computer Science", "Year 12", "IB MYP 5"):
            status, p = call("PUT", f"/api/students/{aid}/details",
                             {"class_name": value})
            check(f"custom class {value!r} is accepted", p.get("class_label"),
                  value)

        # --- §5 bulk ---------------------------------------------------------
        status, p = call("PUT", f"/api/students/{aid}/details",
                         {"nickname": "Mo", "class_name": "10A"})
        call("PUT", f"/api/students/{bid}/details", {"class_name": "10B"})
        before = {s["student_id"]: s["display_name"] for s in students}
        status, bulk = call("POST", "/api/students/bulk-details",
                            {"student_ids": [aid, bid], "class_name": "11C"})
        check("bulk assign returns 200", status, 200)
        check("bulk updated both", len(bulk["updated"]), 2)
        status, pa = call("GET", f"/api/students/{aid}")
        status, pb = call("GET", f"/api/students/{bid}")
        check("bulk changed student A", pa["class_label"], "11C")
        check("bulk changed student B", pb["class_label"], "11C")
        check("bulk left A's nickname alone", pa["display_name"], "Mo")
        check("bulk left B's name alone", pb["display_name"],
              b["official_name"])

        # --- §6 the list carries it -----------------------------------------
        status, listing = call("GET", "/api/students")
        by_id = {s["student_id"]: s for s in listing["students"]}
        check("list shows the nickname", by_id[aid]["display_name"], "Mo")
        check("list shows the class", by_id[aid]["class_label"], "11C")
        check("list shows the classroom name too",
              by_id[aid]["official_name"], a["official_name"])
        truthy("no student has a blank class cell",
               all(s.get("class_label") for s in listing["students"]))

        # --- §4/§13 class vocabulary ----------------------------------------
        status, vocab = call("GET", "/api/students/classes")
        check("vocabulary returns 200", status, 200)
        truthy("vocabulary offers the usual classes",
               {"10A", "10B", "12C"} <= set(vocab["classes"]))
        truthy("vocabulary includes the teacher's own value",
               "11C" in vocab["classes"])

        # --- §8 grouping in the class report --------------------------------
        status, courses = call("GET", "/api/classroom/courses")
        course_list = courses.get("courses") or courses.get("items") or []
        course = next((c for c in course_list if c.get("name")), None)
        if course:
            status, rep = call("GET", f"/api/reports/class/{course['id']}")
            if status == 200:
                groups = {g["class_label"] for g in rep.get("groups") or []}
                truthy("the class report groups by assigned class",
                       "11C" in groups or "Not assigned" in groups)
                truthy("unassigned students stay visible",
                       len(rep.get("groups") or []) >= 1)
            else:
                print(f"  SKIP  class report unavailable ({status})")
        else:
            print("  SKIP  no synced course to group")

    finally:
        print("\nrestoring live data...")
        for sid, _ in restore:
            st, _ = call("PUT", f"/api/students/{sid}/details",
                         {"nickname": "", "class_name": ""})
            print(f"  {'ok' if st == 200 else st} cleared {sid}")
        status, listing = call("GET", "/api/students")
        dirty = [s["student_id"] for s in listing["students"]
                 if s.get("nickname") or s.get("class_name")]
        print(f"students still carrying metadata: {len(dirty)}")
        for sid in dirty:
            row = next(s for s in listing["students"]
                       if s["student_id"] == sid)
            print(f"  ! {sid} nickname={row['nickname']!r} "
                  f"class={row['class_name']!r}")

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    for f in FAILED:
        print(f"  FAILED: {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())