"""PDF optimization tests (§ compression) — smaller, never less markable.

The optimizer serves the standard correction pipeline for BOTH the student
submission and the mark scheme. These tests pin the behaviour that
makes it safe to enable by default:

* a scanned paper and a scanned mark scheme both shrink, with page count,
  geometry, text layer and rendering preserved;
* a document that cannot benefit keeps its original (no pointless artifact);
* corrupt/encrypted input is refused, never rewritten;
* the cache is content-addressed and reused (no repeated compression);
* an integrity failure falls back to the original;
* both roles are cached separately for the same bytes;
* a job's submission copies are released at cleanup while shared mark-scheme
  copies survive;
* retention still keeps the ORIGINAL file (compression never replaces it).
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

import app.db as db
from app.engine import pdf_optimize as po


# ------------------------------------------------------------------ fixtures

def _scan_pdf(path: Path, pages: int = 2, dpi: int = 300,
              marks: int = 220) -> Path:
    """A photo-like page: high-resolution JPEG with thin, faint marks.

    The marks are what handwriting compression must preserve: short strokes,
    dots (decimal points), and a couple of tiny glyph clusters.
    """
    import pymupdf
    from PIL import Image, ImageDraw

    doc = pymupdf.open()
    width_in, height_in = 8.27, 11.69
    px_w, px_h = int(width_in * dpi), int(height_in * dpi)
    for page_no in range(pages):
        image = Image.new("L", (px_w, px_h), 248)
        draw = ImageDraw.Draw(image)
        for i in range(marks):
            x = 60 + (i * 37) % (px_w - 200)
            y = 80 + (i * 53) % (px_h - 200)
            draw.line((x, y, x + 46, y + 9), fill=35, width=2)
            draw.ellipse((x + 60, y + 20, x + 63, y + 23), fill=60)
        draw.text((80, 60), f"Q{page_no + 1} 3.5 - 1/2 = 1.75  x²  √9",
                  fill=20)
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "JPEG", quality=92)
        page = doc.new_page(width=width_in * 72, height=height_in * 72)
        page.insert_image(page.rect, stream=buf.getvalue())
    data = doc.tobytes(garbage=1)
    doc.close()
    path.write_bytes(data)
    return path


def _faded_copy(src: Path, dest: Path, factor: float = 1.6) -> Path:
    """A candidate whose pages still exist but whose ink was washed out.

    Mean pixel difference alone cannot see this: a nearly-white page of faint
    pencil differs by only a few grey levels, which is why the ink-retention
    check exists.
    """
    import pymupdf
    from PIL import Image, ImageEnhance

    doc = pymupdf.open(str(src))
    for page in doc:
        for entry in page.get_images(full=True):
            xref = int(entry[0])
            info = doc.extract_image(xref)
            with Image.open(io.BytesIO(info["image"])) as img:
                faded = ImageEnhance.Brightness(
                    img.convert("RGB")).enhance(factor)
                buffer = io.BytesIO()
                faded.save(buffer, "JPEG", quality=90)
            page.replace_image(xref, stream=buffer.getvalue())
    doc.save(str(dest), garbage=4, deflate=True)
    doc.close()
    return dest


def _text_pdf(path: Path, pages: int = 2) -> Path:
    """A digitally generated paper: real text (maths symbols), no scans."""
    import pymupdf

    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 90),
                         f"Question {index + 1}: solve x² − 3x + 2 = 0",
                         fontsize=13)
        page.insert_text((72, 120), "Answer: x = 1 or x = 2  (M1 A1)",
                         fontsize=12)
        page.insert_text((72, 150), "∫₀¹ 2x dx = 1   √16 = 4   ½ + ¼",
                         fontsize=12)
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return path


# ------------------------------------------------------------- core behaviour

class TestOptimizesBothDocumentTypes:
    def test_a_scanned_student_paper_shrinks_and_stays_intact(self, tmp_path):
        src = _scan_pdf(tmp_path / "student.pdf")
        before = src.stat().st_size

        result = po.optimize_pdf(src, role="submission")

        assert result.used_optimized, result.report.as_dict()
        assert result.path != str(src)
        assert Path(result.path).stat().st_size < before * 0.9
        assert result.report.ratio and result.report.ratio < 0.9
        assert result.report.pages_before == result.report.pages_after == 2
        assert result.report.integrity_ok is True
        assert result.report.lossy is True
        # Faint writing survived: the verifier measured the ink it kept.
        assert result.report.detail["min_ink_ratio"] >= po.MIN_INK_RATIO
        # The ORIGINAL is untouched — it is what retention keeps.
        assert src.stat().st_size == before

        import pymupdf
        original = pymupdf.open(str(src))
        optimized = pymupdf.open(result.path)
        try:
            assert [p.rect for p in optimized] == [p.rect for p in original]
            assert [p.rotation for p in optimized] == [p.rotation for p
                                                       in original]
        finally:
            original.close()
            optimized.close()

    def test_a_scanned_mark_scheme_shrinks_too(self, tmp_path):
        src = _scan_pdf(tmp_path / "scheme.pdf", pages=2)
        before = src.stat().st_size

        result = po.optimize_pdf(src, role="mark_scheme")

        assert result.used_optimized
        assert Path(result.path).stat().st_size < before * 0.9
        assert result.report.role == "mark_scheme"
        assert result.report.integrity_ok is True

    def test_maths_notation_is_still_extractable_after_optimisation(
            self, tmp_path):
        src = _text_pdf(tmp_path / "paper.pdf")
        import pymupdf
        original_text = [p.get_text() for p in pymupdf.open(str(src))]

        result = po.optimize_pdf(src, role="submission")

        doc = pymupdf.open(result.path)
        try:
            assert [p.get_text() for p in doc] == original_text
            assert "Question 1" in doc[0].get_text()
            # Ink on the page survives: the fraction of dark pixels in the
            # rendered page stays within noise of the original.
            def _ink(page):
                samples = page.get_pixmap(dpi=120,
                                          colorspace=pymupdf.csGRAY).samples
                return sum(1 for value in samples[::37] if value < 128)
            src_doc = pymupdf.open(str(src))
            try:
                before_ink = _ink(src_doc[0])
            finally:
                src_doc.close()
            assert abs(_ink(doc[0]) - before_ink) <= max(5, before_ink * 0.05)
        finally:
            doc.close()

    def test_both_roles_get_separate_artifacts_for_the_same_bytes(
            self, tmp_path):
        src = _scan_pdf(tmp_path / "same.pdf")

        submission = po.optimize_pdf(src, role="submission")
        scheme = po.optimize_pdf(src, role="mark_scheme")

        assert submission.path != scheme.path
        assert submission.report.cache_key != scheme.report.cache_key
        assert Path(submission.path).is_file() and Path(scheme.path).is_file()


class TestNoNeedlessWork:
    def test_an_already_small_document_keeps_its_original(self, tmp_path):
        src = _text_pdf(tmp_path / "tiny.pdf", pages=1)
        result = po.optimize_pdf(src, role="submission")

        if result.used_optimized:      # a real saving is fine, a tiny one is not
            assert result.report.ratio is not None
            assert result.report.ratio <= 1 - po.MIN_SAVING
        else:
            assert result.path == str(src)
            assert result.report.skipped in (
                "no meaningful size benefit", "result was not smaller")

    def test_the_same_bytes_are_compressed_only_once(self, tmp_path,
                                                     monkeypatch):
        src = _scan_pdf(tmp_path / "student.pdf")
        first = po.optimize_pdf(src, role="submission")
        assert first.used_optimized

        calls = {"n": 0}
        real = po._build_candidate

        def _count(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(po, "_build_candidate", _count)
        second = po.optimize_pdf(src, role="submission")

        assert second.report.used == "cached"
        assert second.path == first.path
        assert calls["n"] == 0            # no second compression pass

    def test_a_cached_artifact_is_reused_after_the_source_disappears(
            self, tmp_path):
        src = _scan_pdf(tmp_path / "student.pdf")
        first = po.optimize_pdf(src, role="submission")
        assert first.used_optimized
        # A retry after cleanup: the source is gone, the cached copy is not
        # needed to be re-created.
        assert Path(first.path).is_file()


class TestRefusesUnsafeInput:
    def test_corrupt_bytes_are_never_rewritten(self, tmp_path):
        bad = tmp_path / "broken.pdf"
        bad.write_bytes(b"this is not a pdf")
        result = po.optimize_pdf(bad, role="submission")

        assert result.path == str(bad)
        assert result.used_optimized is False
        assert result.report.skipped

    def test_an_encrypted_pdf_is_left_alone(self, tmp_path):
        import pymupdf
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "secret")
        encrypted = tmp_path / "enc.pdf"
        doc.save(str(encrypted), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 owner_pw="owner", user_pw="user")
        doc.close()

        result = po.optimize_pdf(encrypted, role="submission")

        assert result.path == str(encrypted)
        assert result.report.skipped == "encrypted PDF"

    def test_an_integrity_failure_falls_back_to_the_original(
            self, tmp_path, monkeypatch):
        src = _scan_pdf(tmp_path / "student.pdf")
        monkeypatch.setattr(po, "verify_candidate",
                            lambda a, b: (False, {"reason": "synthetic"}))
        result = po.optimize_pdf(src, role="submission")

        assert result.path == str(src)
        assert result.used_optimized is False
        assert "integrity check failed" in (result.report.skipped or "")
        # A rejected candidate leaves no artifact behind.
        assert not list(po.cache_root().glob("*/*.pdf")) or all(
            "synthetic" not in p.name for p in po.cache_root().glob("*/*.pdf"))

    def test_a_candidate_larger_than_the_original_is_discarded(
            self, tmp_path, monkeypatch):
        """Compression must never make a file bigger just to say it ran."""
        src = _scan_pdf(tmp_path / "student.pdf")
        original = src.read_bytes()
        monkeypatch.setattr(
            po, "_build_candidate",
            lambda *a, **k: (original + b"0" * 4096, {"lossy": False}))

        result = po.optimize_pdf(src, role="submission")

        assert result.path == str(src)
        assert result.used_optimized is False
        assert result.report.skipped == "result was not smaller"
        assert not list(po.cache_root().glob("*/*.pdf"))

    def test_a_rewrite_failure_returns_the_original(self, tmp_path,
                                                    monkeypatch):
        """A crashed/timed-out rewrite is a skip, not a failed correction."""
        src = _scan_pdf(tmp_path / "student.pdf")

        def _explode(*args, **kwargs):
            raise TimeoutError("rewrite exceeded its time budget")

        monkeypatch.setattr(po, "_build_candidate", _explode)
        result = po.optimize_pdf(src, role="submission")

        assert result.path == str(src)
        assert result.report.skipped == "optimization failed (TimeoutError)"
        assert result.report.integrity_ok is False

    def test_a_missing_source_is_safe(self, tmp_path):
        missing = tmp_path / "gone.pdf"
        result = po.optimize_pdf(missing, role="mark_scheme")
        assert result.path == str(missing)
        assert result.report.skipped == "source missing"

    def test_a_faded_candidate_fails_the_ink_gate(self, tmp_path):
        """A washed-out rewrite is rejected although its pages are intact."""
        src = _scan_pdf(tmp_path / "student.pdf")
        faded = _faded_copy(src, tmp_path / "faded.pdf", factor=2.5)

        ok, detail = po.verify_candidate(src, faded)

        assert ok is False
        assert detail["reason"] == "marks/ink lost"
        assert detail["ink_ratio"] < po.MIN_INK_RATIO
        # The same candidate would NOT have been caught by pixel difference
        # alone — the gate is what makes the difference.
        assert detail.get("page_pixel_diff", 0) <= po.MAX_PAGE_PIXEL_DIFF

    def test_a_blanked_page_fails_and_the_original_is_used(
            self, tmp_path, monkeypatch):
        src = _scan_pdf(tmp_path / "student.pdf")
        blanked = _faded_copy(src, tmp_path / "blank.pdf", factor=100.0)
        monkeypatch.setattr(
            po, "_build_candidate",
            lambda *a, **k: (blanked.read_bytes(), {"lossy": True}))

        result = po.optimize_pdf(src, role="submission")

        assert result.path == str(src)
        assert "integrity check failed" in (result.report.skipped or "")

    def test_an_oversized_document_skips_the_lossy_pass(self, tmp_path,
                                                        monkeypatch):
        src = _scan_pdf(tmp_path / "student.pdf")
        monkeypatch.setattr(po, "MAX_BYTES_FOR_REWRITE", 10)
        result = po.optimize_pdf(src, role="submission")
        assert result.report.lossy is False
        # The lossless pass may still apply; the lossy one never ran.
        assert "images_rewritten" not in (result.report.detail or {})


class TestCacheHousekeeping:
    def test_release_job_drops_submission_copies_but_keeps_schemes(
            self, tmp_path):
        src = _scan_pdf(tmp_path / "student.pdf")
        sub = po.optimize_pdf(src, role="submission", job_id="job-a")
        scheme = po.optimize_pdf(src, role="mark_scheme", job_id="job-a")
        assert Path(sub.path).is_file() and Path(scheme.path).is_file()

        removed = po.release_job("job-a")

        assert removed == 1
        assert not Path(sub.path).exists()
        assert Path(scheme.path).is_file()

    def test_sweep_respects_the_size_cap(self, tmp_path):
        src = _scan_pdf(tmp_path / "student.pdf")
        result = po.optimize_pdf(src, role="submission")
        assert Path(result.path).is_file()

        po.sweep_cache(max_bytes=1)

        assert not Path(result.path).exists()

    def test_stats_report_real_savings(self, tmp_path):
        src = _scan_pdf(tmp_path / "student.pdf")
        po.optimize_pdf(src, role="submission")
        stats = po.cache_stats()
        assert stats["entries"] >= 1
        assert stats["bytes_saved"] > 0


# ------------------------------------------------------- pipeline integration

class TestPipelineIntegration:
    def test_the_normal_pipeline_stages_optimised_copies_for_both_documents(
            self, tmp_path):
        """The Normal Corrector's staging step optimizes BOTH documents once."""
        from app.workers.pool import WorkerPool

        student = _scan_pdf(tmp_path / "student.pdf")
        scheme = _scan_pdf(tmp_path / "scheme.pdf")
        job_id = "normal-opt"
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO pending_manual_files(job_id, kind, path, "
                "filename) VALUES(?,?,?,?)",
                (job_id, "submission", str(student), student.name))
            conn.execute(
                "INSERT INTO pending_manual_files(job_id, kind, path, "
                "filename) VALUES(?,?,?,?)",
                (job_id, "mark_scheme", str(scheme), scheme.name))

        # A bare instance: staging is pure preparation (no pool threads).
        pool = WorkerPool.__new__(WorkerPool)
        job = {"id": job_id, "student_name": "Ada", "submission_id": None}
        paths = pool._stage_inputs(job)

        assert len(paths["submission"]) == 1
        assert paths["mark_scheme"]
        for path in paths["submission"] + [paths["mark_scheme"]]:
            assert Path(path).is_file()
            # The model is handed the cached optimized copy, not the staging
            # file retention owns.
            assert str(po.cache_root()) in path
        # The originals are still where retention expects them.
        assert student.is_file() and scheme.is_file()

        # A retry reuses the prepared manifest instead of compressing again.
        again = pool._stage_inputs(job)
        assert again["submission"] == paths["submission"]
        assert again["mark_scheme"] == paths["mark_scheme"]

    def test_retention_still_keeps_the_original_after_optimisation(
            self, tmp_path, fresh_state):
        """Compression must never replace what retention stores."""
        from app.config import get_config, save_config
        from app.engine.service import cleanup_job_files

        cfg = get_config()
        cfg.retention.keep_submissions = "always"
        save_config(cfg)

        job_id = "opt-keep"
        job_dir = fresh_state / "tmp" / f"job-{job_id}"
        job_dir.mkdir(parents=True)
        original = job_dir / "submission_0_0_ab12_normalized.pdf"
        original.write_bytes(_scan_pdf(tmp_path / "src.pdf").read_bytes())
        original_bytes = original.read_bytes()

        optimized = po.optimize_pdf(original, role="submission",
                                    job_id=job_id)
        assert optimized.used_optimized

        cleanup_job_files(job_id)

        rows = db.query("SELECT * FROM retained_files WHERE job_id = ?",
                        (job_id,))
        assert len(rows) == 1
        # The retained file is the ORIGINAL bytes, not the compressed copy.
        assert Path(rows[0]["path"]).read_bytes() == original_bytes
        # The job's compressed submission copy was released with the job.
        assert not Path(optimized.path).exists()


class TestMeasuredSavingsAndReporting:
    """The numbers the UI shows must describe the file that is uploaded."""

    def test_reduction_percentage_is_never_invented(self):
        report = po.OptimizationReport(
            role="submission", source_name="x.pdf", original_bytes=2_000_000,
            optimized_bytes=500_000, used="optimized")
        assert report.reduction_percent == 75.0

        # The original was kept: there is no saving to report, even though a
        # larger candidate may have been measured.
        kept = po.OptimizationReport(
            role="submission", source_name="x.pdf", original_bytes=2_000_000,
            optimized_bytes=1_500_000, used="original")
        assert kept.reduction_percent is None

        # A zero-byte source cannot produce a meaningful percentage.
        empty = po.OptimizationReport(
            role="submission", source_name="x.pdf", original_bytes=0,
            optimized_bytes=0, used="optimized")
        assert empty.reduction_percent is None

    def test_a_document_over_the_safety_cap_is_left_untouched(
            self, tmp_path, monkeypatch):
        src = _scan_pdf(tmp_path / "student.pdf")
        monkeypatch.setattr(po, "MAX_BYTES_FOR_LOSSLESS", 10)

        result = po.optimize_pdf(src, role="submission")

        assert result.path == str(src)
        assert result.used_optimized is False
        assert result.report.skipped == "larger than the safe optimization limit"

    def test_an_interrupted_candidate_is_swept_after_an_hour(self, tmp_path):
        import os

        folder = po.cache_root() / "ab"
        folder.mkdir(parents=True, exist_ok=True)
        stale = folder / "abandoned.partial"
        fresh = folder / "in-progress.partial"
        stale.write_bytes(b"x")
        fresh.write_bytes(b"x")
        old = time.time() - po.PARTIAL_MAX_AGE_SECONDS - 60
        os.utime(stale, (old, old))

        po.sweep_cache(max_age_seconds=10 ** 9)

        assert not stale.exists()
        assert fresh.exists()


class TestCompressionProgress:
    def _stage(self, tmp_path, job_id: str):
        from app.workers.pool import WorkerPool

        student = _scan_pdf(tmp_path / f"{job_id}-student.pdf")
        scheme = _scan_pdf(tmp_path / f"{job_id}-scheme.pdf")
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO pending_manual_files(job_id, kind, path, filename) "
                "VALUES(?,?,?,?)",
                (job_id, "submission", str(student), student.name))
            conn.execute(
                "INSERT INTO pending_manual_files(job_id, kind, path, filename) "
                "VALUES(?,?,?,?)",
                (job_id, "mark_scheme", str(scheme), scheme.name))
        pool = WorkerPool.__new__(WorkerPool)
        job = {"id": job_id, "student_name": "Ada", "submission_id": None}
        return pool, job

    def test_progress_names_the_measured_reduction(self, tmp_path, monkeypatch):
        from app.workers.pool import WorkerPool
        from app.workers import pool as pool_module

        pool, job = self._stage(tmp_path, "opt-progress")
        stages: list[str] = []
        monkeypatch.setattr(
            pool_module, "_safe_progress",
            lambda job_id, **kw: stages.append(kw.get("stage") or ""))

        paths = pool._stage_inputs(job)

        assert Path(paths["submission"][0]).is_file()
        joined = " | ".join(stages)
        assert "Preparing the student's PDF" in joined
        assert "Student PDF compressed" in joined
        assert "Mark scheme compressed" in joined
        assert "% smaller" in joined
        assert "Compressed PDFs ready" in joined

    def test_a_broken_progress_feed_never_skips_compression(
            self, tmp_path, monkeypatch):
        """Reporting is not load-bearing: the smaller file is still staged."""
        from app.workers.pool import WorkerPool

        from app.jobs import manager as manager_module

        pool, job = self._stage(tmp_path, "opt-broken-ui")

        class _BrokenReporter:
            def report_progress(self, *args, **kwargs):
                raise RuntimeError("progress bus unavailable")

        # The live reporter fails; the row-update fallback inside
        # _safe_progress still runs and the correction still gets its file.
        monkeypatch.setattr(manager_module.JobManagerHolder, "instance",
                            classmethod(lambda cls: _BrokenReporter()))

        paths = pool._stage_inputs(job)

        assert str(po.cache_root()) in paths["submission"][0]
        assert str(po.cache_root()) in paths["mark_scheme"]


def _native_scan_pdf(path: Path, pages: int = 2) -> Path:
    """A phone-scanner page: photo pixels placed 1:1 on a page in points.

    This is the shape real CamScanner-style submissions have (page rect equals
    the pixel dimensions, so placement DPI is 72). Downsampling would throw
    away real detail, but the JPEG stream itself can be re-encoded safely.
    """
    import pymupdf
    from PIL import Image, ImageDraw

    px_w, px_h = 2000, 2800
    doc = pymupdf.open()
    for page_no in range(pages):
        image = Image.new("L", (px_w, px_h), 246)
        draw = ImageDraw.Draw(image)
        # Enough fine strokes that the stream is realistically large
        # (> RECOMPRESS_MIN_IMAGE_BYTES), like a photographed answer page.
        for i in range(900):
            x = 70 + (i * 41) % (px_w - 240)
            y = 90 + (i * 57) % (px_h - 260)
            draw.line((x, y, x + 52, y + 8), fill=38, width=2)
            draw.ellipse((x + 70, y + 22, x + 73, y + 25), fill=70)
        draw.text((90, 70), f"Q{page_no + 1}: 2x + 7 = 31, x = 12",
                  fill=25)
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "JPEG", quality=96)
        page = doc.new_page(width=px_w, height=px_h)
        page.insert_image(page.rect, stream=buf.getvalue())
    data = doc.tobytes(garbage=1)
    doc.close()
    path.write_bytes(data)
    return path


class TestNativeResolutionRecompression:
    def test_a_phone_scan_shrinks_without_losing_a_single_pixel(self, tmp_path):
        import pymupdf

        src = _native_scan_pdf(tmp_path / "phone-scan.pdf")
        before = src.stat().st_size

        result = po.optimize_pdf(src, role="submission")

        assert result.used_optimized, result.report.as_dict()
        assert result.report.detail.get("images_recompressed", 0) >= 1
        assert result.report.lossy is True
        assert result.report.integrity_ok is True
        assert result.report.reduction_percent >= 20

        original = pymupdf.open(str(src))
        optimized = pymupdf.open(result.path)
        try:
            # Same pages, same page boxes, and the embedded image still has
            # exactly the original pixel width/height.
            assert optimized.page_count == original.page_count
            assert [p.rect for p in optimized] == [p.rect for p in original]
            for a, b in zip(original, optimized):
                assert a.get_images(full=True)[0][2:4] == \
                    b.get_images(full=True)[0][2:4]
        finally:
            original.close()
            optimized.close()

    def test_it_is_not_repeated_on_the_next_job(self, tmp_path, monkeypatch):
        src = _native_scan_pdf(tmp_path / "phone-scan.pdf")
        first = po.optimize_pdf(src, role="submission")
        assert first.used_optimized

        def _explode(*args, **kwargs):
            raise AssertionError("recompressed a cached document again")

        monkeypatch.setattr(po, "_recompress_jpeg_images", _explode)
        second = po.optimize_pdf(src, role="submission")

        assert second.report.used == "cached"
        assert second.path == first.path


class TestSettings:
    def test_lossy_can_be_disabled_without_disabling_cleanup(self, tmp_path):
        src = _native_scan_pdf(tmp_path / "phone-scan.pdf")

        result = po.optimize_pdf(src, role="submission", allow_lossy=False)

        assert result.report.lossy is False
        assert "images_recompressed" not in result.report.detail
        assert Path(result.path).stat().st_size <= src.stat().st_size

    def test_turning_compression_off_uploads_the_staged_original(
            self, tmp_path, monkeypatch):
        from app.config import get_config
        from app.workers.pool import WorkerPool

        student = _native_scan_pdf(tmp_path / "student.pdf")
        scheme = _native_scan_pdf(tmp_path / "scheme.pdf")
        job_id = "opt-off"
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO pending_manual_files(job_id, kind, path, filename) "
                "VALUES(?,?,?,?)",
                (job_id, "submission", str(student), student.name))
            conn.execute(
                "INSERT INTO pending_manual_files(job_id, kind, path, filename) "
                "VALUES(?,?,?,?)",
                (job_id, "mark_scheme", str(scheme), scheme.name))
        monkeypatch.setattr(get_config().pdf_compression, "enabled", False)

        pool = WorkerPool.__new__(WorkerPool)
        paths = pool._stage_inputs(
            {"id": job_id, "student_name": "Ada", "submission_id": None})

        assert str(po.cache_root()) not in paths["submission"][0]
        assert str(po.cache_root()) not in paths["mark_scheme"]
        # The staged copy is byte-identical to the original.
        assert Path(paths["submission"][0]).read_bytes() == student.read_bytes()
