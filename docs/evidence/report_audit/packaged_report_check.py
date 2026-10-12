import json, io, sys
sys.path.insert(0, "backend")
from app import db
from app.automation import runner as rm
from app.classroom.report_attachment import render_report_pdf
from app.automation.pdf_validation import validate_report_pdf
from app.config import AutomationConfig
from pypdf import PdfReader
JOB = "89f100a055ac4c56a168ae0bc144ccc4"
job = dict(db.query_one("SELECT * FROM automation_jobs WHERE id=?", (JOB,)))
r = rm.WorkflowRunner.__new__(rm.WorkflowRunner)
r._service=None; r._render=render_report_pdf; r._validate=validate_report_pdf
r._config=AutomationConfig(review_mode=False); r._account_email="t@x.org"
dec = r._report_result(job)
pdf = r._render_report(job)
must, any_of = r._expected_text(job, dec)
v = validate_report_pdf(pdf, expected_text=must, expected_any=any_of)
text = "".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages[:2])
print("packaged-runtime render:", dec["display_name"], "| bytes", len(pdf), "| pages", v["pages"])
print("packaged-runtime validation:", v["ok"], v["reason"])
print("printed names:", {n: (n.lower() in text.lower()) for n in any_of})
assert v["ok"], v
print("PASS: the packaged distribution generates a VALID report for the job that failed in production.")
