
# ------------------------------------------------------- unified workspace
# (§17-§20, §47-§49): ONE report pipeline. The JSON preview and the PDF
# export are built from the SAME filters -> SAME model -> SAME renderer, so
# a teacher can never see one report on screen and a different one on paper.

REPORT_TYPES = ("class", "missing", "assignment", "student", "performance")

#: The teacher's low-grade threshold options (§24). The default lives in one
#: place only - ``reporting.class_report.DEFAULT_LOW_THRESHOLD``.
THRESHOLD_OPTIONS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)


def _parse_threshold(raw: str | None) -> float | None:
    """``None`` disables the low-grade section; anything else is a number."""
    from ..reporting.class_report import DEFAULT_LOW_THRESHOLD
    text = str(raw if raw is not None else "").strip()
    if not text:
        return DEFAULT_LOW_THRESHOLD
    if text.lower() in {"off", "none", "disabled", "no"}:
        return None
    try:
        value = float(text)
    except ValueError:
        raise AppError("The low-grade threshold must be a number (or 'off').",
                       status=400, code="bad_threshold")
    if value < 0 or value > 100:
        raise AppError("The low-grade threshold must be between 0 and 100.",
                       status=400, code="bad_threshold")
    return value


def _all_coursework_ids(course_id: str | None) -> list[str]:
    if course_id:
        rows = db.query("SELECT id FROM coursework WHERE course_id = ? "
                        "ORDER BY update_time DESC", (course_id,))
    else:
        rows = db.query("SELECT id FROM coursework ORDER BY update_time DESC")
    return [str(r["id"]) for r in rows]


def _selected_ids(assignments: str | None, course_id: str | None) -> list[str]:
    """Resolve the teacher's assignment selection to real coursework ids.

    ``all`` includes every assignment in scope; anything else is a
    comma-separated list, and an id outside the scope is rejected loudly
    rather than silently dropped (§22, §46).
    """
    text = str(assignments or "all").strip()
    if text.lower() in {"all", "*"}:
        return _all_coursework_ids(course_id)
    wanted = [s.strip() for s in text.split(",") if s.strip()]
    if not wanted:
        raise AppError("No assignments selected. Choose one or more "
                       "assignments, or 'all'.",
                       status=400, code="no_assignments_selected")
    owned = set(_all_coursework_ids(course_id))
    unknown = [cid for cid in wanted if cid not in owned]
    if unknown:
        raise AppError(
            "Assignment(s) not found in this class: " + ", ".join(unknown),
            status=404, code="assignment_not_found")
    return wanted


def _parse_classes(classes: str | None) -> list[str] | None:
    """``None`` means every teacher-assigned class (§49)."""
    text = str(classes or "all").strip()
    if text.lower() in {"all", "*"}:
        return None
    labels = [s.strip() for s in text.split(",") if s.strip()]
    if not labels:
        return None
    return labels


def assemble_report(*, report_type: str = "class",
                    assignments: str = "all",
                    classes: str = "all",
                    course_id: str | None = None,
                    threshold: str | None = None,
                    student_id: str | None = None):
    """Build the ONE report model every preview and export is derived from.

    Filters are applied here and nowhere else: the class filter, the
    assignment selection, the student scope and the low-grade threshold all
    resolve before a single row reaches the renderer.
    """
    from ..reporting.class_report import assemble

    rtype = (report_type or "class").lower()
    if rtype not in REPORT_TYPES:
        raise AppError(f"Unknown report type: {report_type}",
                       status=400, code="bad_report_type")
    coursework_ids = _selected_ids(assignments, course_id)
    wanted = set(coursework_ids)
    limit = _parse_threshold(threshold)
    labels = _parse_classes(classes)

    rows = [r for r in _report_rows(course_id, None)
            if str(r.get("coursework_id") or "") in wanted]
    if rtype == "student" or student_id:
        if not student_id:
            raise AppError("A student report needs a student.",
                           status=400, code="student_required")
        rows = [r for r in rows if r.get("student_id") == student_id]
        if not rows:
            raise AppError("That student has no tracked submissions for the "
                           "selected assignments.",
                           status=404, code="student_not_found")

    coursework_list = []
    for cid in coursework_ids:
        cw = _assignment(cid)
        if cw:
            coursework_list.append(dict(cw))
    course_row = _course(course_id) if course_id else None
    course = {
        "id": (course_row["id"] if course_row
               else (coursework_list[0]["course_id"]
                     if coursework_list else "all")),
        "name": (course_row["name"] if course_row else "All classes"),
        "courses": coursework_list,
    }
    return assemble(course, rows, coursework_ids, class_labels=labels,
                    generated_at=time.time(), threshold=limit)


def _branding_dict() -> dict:
    from ..reporting import design as D
    brand = D.load_branding()
    return {
        "teacher_name": brand.teacher_name,
        "teacher_title": brand.teacher_title,
        "center": brand.center,
        "year": brand.year,
        "quote": brand.quote,
        "monogram": brand.monogram,
        "logo": bool(brand.logo_path),
    }


@router.get("/reports/options")
def reports_options():
    """Everything the Reports workspace needs to render its controls (§17)."""
    from ..reporting.class_report import DEFAULT_LOW_THRESHOLD, parse_assignment
    from ..student_identity import known_classes
    from ..classroom.auth import auth_state

    # Teacher-assigned classes actually in use, with their real head count.
    classes = []
    for label in known_classes():
        count = db.query_one(
            "SELECT COUNT(*) AS n FROM students WHERE class_name = ?",
            (label,))
        students = count["n"] if count else 0
        if students or label in {"10A", "10B", "10C"}:
            classes.append({"label": label, "students": students or 0})

    assignments = []
    for row in db.query(
            "SELECT w.id, w.course_id, w.title, w.max_points, w.due_date, "
            "c.name AS course_name FROM coursework w "
            "LEFT JOIN courses c ON c.id = w.course_id "
            "ORDER BY w.update_time DESC"):
        item = dict(row)
        code = parse_assignment(item.get("title"),
                                coursework_id=item.get("id"),
                                max_points=item.get("max_points"))
        assignments.append({
            "id": item["id"], "course_id": item["course_id"],
            "course_name": item.get("course_name"),
            "title": item["title"], "max_points": item.get("max_points"),
            "due_date": item.get("due_date"),
            "code": code.code, "chapter": code.chapter,
            "lesson": code.lesson, "topic": code.topic,
            "detail": code.detail, "label": code.label,
        })

    try:
        from ..engine.grading import active_boundaries, describe_boundaries
        boundaries = [[g, m] for g, m in active_boundaries()]
        scale = describe_boundaries(boundaries)
    except Exception:  # noqa: BLE001 - options must never fail
        boundaries, scale = [], ""

    return {
        "report_types": [
            {"value": "class", "label": "Class Report"},
            {"value": "missing", "label": "Missing Homework Report"},
            {"value": "assignment", "label": "Assignment Report"},
            {"value": "student", "label": "Student Report"},
            {"value": "performance", "label": "Performance / Grade Report"},
        ],
        "classes": classes,
        "assignments": assignments,
        "threshold_default": DEFAULT_LOW_THRESHOLD,
        "threshold_options": list(THRESHOLD_OPTIONS),
        "grade_scale": scale,
        "boundaries": boundaries,
        "branding": _branding_dict(),
        "connected": auth_state().connected,
        "statuses": [
            {"value": "missing", "label": "Missing / not turned in"},
            {"value": "draft", "label": "Draft / incomplete"},
            {"value": "low_grade", "label": "Below threshold"},
            {"value": "submitted", "label": "Handed in"},
        ],
    }


@router.get("/reports/build")
def reports_build(report_type: str = "class", assignments: str = "all",
                  classes: str = "all", course_id: str | None = None,
                  threshold: str | None = None, student_id: str | None = None):
    """The preview model: the same data the PDF is generated from (§45)."""
    from ..reporting.class_report import preview_model
    data = assemble_report(report_type=report_type, assignments=assignments,
                           classes=classes, course_id=course_id,
                           threshold=threshold, student_id=student_id)
    model = preview_model(data, report_type=report_type)
    model["branding"] = _branding_dict()
    model["generated_at"] = data.generated_at
    return model


@router.get("/reports/build/export")
def reports_build_export(report_type: str = "class", assignments: str = "all",
                         classes: str = "all", course_id: str | None = None,
                         threshold: str | None = None,
                         student_id: str | None = None,
                         format: str = "pdf"):
    """Export the selected report as an A4 portrait PDF (§4, §43)."""
    if (format or "pdf").lower() != "pdf":
        raise AppError("Only pdf export is available for reports. "
                       "Use the tracking export for CSV/XLSX/JSON.",
                       status=400, code="bad_report_format")
    from ..reporting import design as D
    from ..reporting.class_report import REPORT_TITLES, render_pdf

    rtype = (report_type or "class").lower()
    data = assemble_report(report_type=rtype, assignments=assignments,
                           classes=classes, course_id=course_id,
                           threshold=threshold, student_id=student_id)
    brand = D.load_branding()
    buf = render_pdf(data, brand,
                     title=REPORT_TITLES.get(rtype, "CLASS REPORT"),
                     label="FOR SIGNATURE", report_type=rtype)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = "".join(c for c in (data.course.get("name") or rtype)
                   if c.isalnum() or c in (" ", "-", "_")).strip()
    slug = slug.replace(" ", "_") or rtype
    log_event("reports", "report exported", report_type=rtype,
              assignment_count=len(data.codes),
              students=data.total_students)
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="{rtype}_{slug}_{stamp}.pdf"'})
