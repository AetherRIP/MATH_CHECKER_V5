"""Render the real report endpoints to PDFs for a visual design review.

Uses the LIVE app routes against the live database, so what you look at is
exactly what a teacher downloads. Read-only: it calls GET endpoints only.

    python scripts/sample_reports.py [out_dir]

Default output is ``dev_local/reports/_samples`` (dev scratch, never shipped).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def main(argv: list[str]) -> int:
    default_out = (Path(__file__).resolve().parents[2]
                   / "dev_local" / "reports" / "_samples")
    out = Path(argv[1]).resolve() if len(argv) > 1 else default_out
    out.mkdir(parents=True, exist_ok=True)
    db.init_db()

    result = db.query_one(
        "SELECT id FROM results ORDER BY created_at DESC LIMIT 1")
    course = db.query_one("SELECT course_id FROM coursework LIMIT 1")
    cw = db.query_one("SELECT id FROM coursework LIMIT 1")
    targets = []

    # `with` runs the app's lifespan, which is what installs the correction
    # service the export routes need.
    with TestClient(app) as api:
        if result:
            for detail in ("summary", "standard", "detailed"):
                r = api.get(f"/api/results/{result['id']}/export?format=pdf"
                            f"&detail={detail}")
                if r.status_code == 200:
                    path = out / f"correction_{detail}.pdf"
                    path.write_bytes(r.content)
                    targets.append(path)
                else:
                    print(f"correction {detail}: HTTP {r.status_code} "
                          f"{r.text[:200]}")

        if course:
            r = api.get(f"/api/reports/class/{course['course_id']}"
                        f"/export?format=pdf")
            if r.status_code == 200:
                path = out / "class_report.pdf"
                path.write_bytes(r.content)
                targets.append(path)
            else:
                print(f"class report: HTTP {r.status_code} {r.text[:200]}")

        if cw:
            r = api.get(f"/api/reports/assignment/{cw['id']}/export?format=pdf")
            if r.status_code == 200:
                path = out / "assignment_report.pdf"
                path.write_bytes(r.content)
                targets.append(path)
            else:
                print(f"assignment report: HTTP {r.status_code} {r.text[:200]}")

    for path in targets:
        print(f"{path.name}: {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
