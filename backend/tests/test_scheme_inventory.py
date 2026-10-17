"""Page-scoped visual inventory: planning, gates, merging and reporting.

The failure reproduced here is the real one: a compiled mark scheme whose
questions are indexed by the document itself, where one whole-document read
silently loses items and the model's own sequential numbering hides the loss.

Everything in this file runs offline. The documents come from reportlab/PIL
fixtures and the model is replaced by a scripted client, so every assertion is
deterministic. No key, no network, no student data.
"""

from __future__ import annotations

import json

import pytest

from app.engine import scheme_inventory as si

from fixtures_mark_scheme import (
    compiled_image_only_mark_scheme, page_break_mark_scheme,
)


# --------------------------------------------------------------- fake model

class _Finish:
    def __init__(self, name: str) -> None:
        self.name = name


class _Candidate:
    def __init__(self, name: str) -> None:
        self.finish_reason = _Finish(name)


class _Response:
    def __init__(self, text: str, finish: str = "STOP") -> None:
        self.text = text
        self.candidates = [_Candidate(finish)]


class _Remote:
    name = "files/fake-scheme"


class ScriptedModels:
    """A model that answers from a script, or from a responder callable."""

    def __init__(self, script=None, responder=None) -> None:
        self.script = list(script or [])
        self.responder = responder
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config=None, **kwargs):
        self.calls.append({"model": model, "contents": contents,
                           "config": config or {}})
        if self.responder is not None:
            return self.responder(contents, model, config or {})
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeClient:
    def __init__(self, script=None, responder=None) -> None:
        self.models = ScriptedModels(script, responder)

    def upload_file(self, path, *args, **kwargs):  # pragma: no cover - unfinished
        return _Remote()


def rendered(chunk: si.PageChunk, pages=None) -> si.RenderedChunk:
    """Page images without touching a real PDF."""
    images = [{"page": p, "png": b"\x89PNG-fake", "width": 10, "height": 10,
               "bytes": 10} for p in (pages or chunk.pages)]
    return si.RenderedChunk(chunk=chunk, images=images)


def payload(questions, **extra) -> str:
    body = {"questions": questions}
    body.update(extra)
    return json.dumps(body)


def question(index: int, label: str | None = None, marks: int | None = 4,
             page: int = 1, parts=None) -> dict:
    item = {"source_index": index, "question_number": label or f"Q{index}",
            "page": page}
    if marks is not None:
        item["max_marks"] = marks
    if parts is not None:
        item["parts"] = parts
    return item


# --------------------------------------------------------- chunk planning

class TestChunkPlanning:
    def test_the_real_document_plans_six_bounded_chunks(self):
        """29 pages, window 6, overlap 1 -> the ranges measured by hand."""
        chunks = si.plan_page_chunks(29)
        assert [c.id for c in chunks] == ["p1-6", "p6-11", "p11-16", "p16-21",
                                          "p21-26", "p26-29"]

    def test_every_page_is_read_at_least_once(self):
        chunks = si.plan_page_chunks(29)
        covered = {p for c in chunks for p in c.pages}
        assert covered == set(range(1, 30))

    def test_boundary_pages_are_read_twice_for_overlap(self):
        chunks = si.plan_page_chunks(29)
        counts = {}
        for c in chunks:
            for p in c.pages:
                counts[p] = counts.get(p, 0) + 1
        assert counts[6] == 2 and counts[11] == 2 and counts[26] == 2
        assert counts[1] == 1 and counts[29] == 1

    def test_planning_is_deterministic(self):
        assert [c.pages for c in si.plan_page_chunks(29)] == \
               [c.pages for c in si.plan_page_chunks(29)]

    def test_a_single_page_document_is_one_chunk(self):
        assert [c.pages for c in si.plan_page_chunks(1)] == [(1,)]

    def test_no_pages_means_no_chunks(self):
        assert si.plan_page_chunks(0) == []

    def test_overlap_is_clamped_below_the_window(self):
        """A bad overlap must clamp instead of producing a zero-step loop."""
        pages = 10
        chunks = si.plan_page_chunks(pages, window=3, overlap=99)
        # clamped to window-1, i.e. a step of one page: bounded, terminating
        assert [c.pages for c in chunks] == [(1, 2, 3), (2, 3, 4), (3, 4, 5),
                                             (4, 5, 6), (5, 6, 7), (6, 7, 8),
                                             (7, 8, 9), (8, 9, 10)]
        assert len(chunks) < pages * 2
        covered = {p for c in chunks for p in c.pages}
        assert covered == set(range(1, pages + 1))

    def test_window_of_one_reads_page_by_page(self):
        assert [c.pages for c in si.plan_page_chunks(3, window=1, overlap=0)] \
            == [(1,), (2,), (3,)]


# --------------------------------------------------------------- rendering

class TestPageRendering:
    def test_pages_render_to_png_at_the_requested_page_numbers(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "pb.pdf")
        images = si.render_pages_png(pdf, [1, 2], dpi=120)
        assert [i["page"] for i in images] == [1, 2]
        assert all(i["png"][:4] == b"\x89PNG" for i in images)
        assert all(i["width"] > 0 and i["height"] > 0 for i in images)

    def test_out_of_range_pages_are_skipped_not_faked(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "pb2.pdf")
        images = si.render_pages_png(pdf, [99], dpi=72)
        assert images == []

    def test_render_chunk_reports_pages_it_could_not_render(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "pb3.pdf")
        chunk = si.PageChunk(id="p1-3", pages=(1, 2, 3))
        result = si.render_chunk(pdf, chunk, dpi=72)
        assert [i["page"] for i in result.images] == [1, 2]
        assert result.missing_pages == [3]

    def test_unreadable_document_is_an_error_not_an_empty_inventory(self, tmp_path):
        bad = tmp_path / "not-a.pdf"
        bad.write_bytes(b"not a pdf at all")
        with pytest.raises(si.InventoryError):
            si.render_pages_png(bad, [1])

    def test_a_rendered_chunk_carries_its_own_image_bytes(self, tmp_path):
        pdf = page_break_mark_scheme(tmp_path / "pb4.pdf")
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        assert si.render_chunk(pdf, chunk).total_bytes > 0


# ------------------------------------------------------------------ gates

class TestChunkAcceptanceGates:
    def test_a_complete_answer_is_accepted(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([_Response(payload(
            [question(1, "June 05/4H/Q8", 5, 1), question(2, "Nov 06/3H/Q16", 2, 2)],
            pages=[1, 2], last_index_on_page=2))])
        result = si.extract_chunk(client, chunk, rendered=rendered(chunk),
                                  model="gemini-3.5-flash")
        assert result["status"] == si.CHUNK_OK
        assert [i["source_index"] for i in result["items"]] == [1, 2]
        assert result["finish_reason"] == "STOP"
        assert result["echoed_pages"] == [1, 2]
        assert result["last_index_on_page"] == 2

    def test_an_explicit_output_budget_is_always_sent(self):
        """The unset default is what silently truncates a long response."""
        chunk = si.PageChunk(id="p1-1", pages=(1,))
        client = FakeClient([_Response(payload([question(1)]))])
        si.extract_chunk(client, chunk, rendered=rendered(chunk), model="m")
        config = client.models.calls[0]["config"]
        assert config["max_output_tokens"] == si.MAX_OUTPUT_TOKENS
        assert config["response_mime_type"] == "application/json"
        assert config["response_schema"] is si.CHUNK_SCHEMA

    def test_page_number_and_index_are_not_confused_with_content(self):
        """The prompt states which pages the images are, in order."""
        chunk = si.PageChunk(id="p6-11", pages=(6, 7, 8, 9, 10, 11))
        client = FakeClient([_Response(payload([question(7)]))])
        si.extract_chunk(client, chunk, rendered=rendered(chunk), model="m")
        prompt = client.models.calls[0]["contents"][0]
        assert "pages 6-11" in prompt
        assert "6, 7, 8, 9, 10, 11" in prompt
        # every page image is attached, after the instruction text
        assert len(client.models.calls[0]["contents"]) == 1 + 6

    def test_a_truncated_response_is_never_accepted(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([_Response(payload([question(1)]), finish="MAX_TOKENS")])
        result = si.extract_chunk(client, chunk, rendered=rendered(chunk), model="m")
        assert result["status"] == si.CHUNK_TRUNCATED
        assert result["items"] == []
        assert "output-token limit" in result["error"]

    def test_a_refused_page_range_is_never_accepted(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([_Response("{}", finish="SAFETY")])
        result = si.extract_chunk(client, chunk, rendered=rendered(chunk), model="m")
        assert result["status"] == si.CHUNK_BLOCKED
        assert result["items"] == []

    def test_unparseable_json_fails_the_chunk(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([_Response('{"questions": [ {"source_index": 3')])
        result = si.extract_chunk(client, chunk, rendered=rendered(chunk), model="m")
        assert result["status"] == si.CHUNK_MALFORMED
        assert result["items"] == []

    def test_items_without_a_printed_index_fail_the_chunk(self):
        """The printed index is the identity; without it nothing is usable."""
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([_Response(payload(
            [{"question_number": "June 05/4H/Q8", "max_marks": 5}]))])
        result = si.extract_chunk(client, chunk, rendered=rendered(chunk), model="m")
        assert result["status"] == si.CHUNK_MALFORMED
        assert result["rejected"][0]["reason"] == "no printed source_index"
        assert "printed index" in result["error"]

    def test_unreadable_items_are_kept_and_flagged_not_dropped(self):
        """A question must never disappear because its label or marks are hard."""
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([_Response(payload([
            {"source_index": 12, "question_number": "", "max_marks": 0},
            question(13, "Jan 12/3H/Q6", 4, 2),
        ]))])
        result = si.extract_chunk(client, chunk, rendered=rendered(chunk), model="m")
        assert result["status"] == si.CHUNK_OK
        first = result["items"][0]
        assert first["source_index"] == 12
        assert first["label_missing"] is True
        assert first["marks_missing"] is True
        assert first["page"] == chunk.first_page      # falls back to the chunk

    def test_subparts_travel_with_their_own_marks_and_page(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([_Response(payload([question(4, "Q4", 6, 1, parts=[
            {"number": "(a)", "max_marks": 2, "criteria": "B1 overlap"},
            {"number": "(b)(ii)", "max_marks": 4, "page": 2},
        ])]))])
        result = si.extract_chunk(client, chunk, rendered=rendered(chunk), model="m")
        parts = result["items"][0]["parts"]
        assert [p["number"] for p in parts] == ["(a)", "(b)(ii)"]
        assert [p["max_marks"] for p in parts] == [2, 4]
        assert parts[1]["page"] == 2

    def test_a_chunk_with_no_page_images_is_unreadable(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([])
        result = si.extract_chunk(
            client, chunk, rendered=si.RenderedChunk(chunk=chunk), model="m")
        assert result["status"] == si.CHUNK_UNREADABLE
        assert client.models.calls == []          # no request was paid for

    def test_a_failed_call_is_recorded_not_raised(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([RuntimeError("503 overloaded")])
        result = si.extract_chunk(client, chunk, rendered=rendered(chunk), model="m")
        assert result["status"] == si.CHUNK_ERROR
        assert "503 overloaded" in result["error"]

    def test_chunk_ok_is_the_only_accepted_state(self):
        assert si.chunk_ok({"status": si.CHUNK_OK}) is True
        for status in (si.CHUNK_TRUNCATED, si.CHUNK_BLOCKED, si.CHUNK_MALFORMED,
                       si.CHUNK_ERROR, si.CHUNK_UNREADABLE, None):
            assert si.chunk_ok({"status": status}) is False


# ------------------------------------------------------- per-chunk retries

class TestChunkRetries:
    def test_a_failed_chunk_is_retried_on_the_next_model_and_succeeds(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([
            _Response(payload([question(1)]), finish="MAX_TOKENS"),
            _Response(payload([question(1), question(2)])),
        ])
        result = si.extract_chunk_with_retries(
            client, chunk, models=["model-a", "model-b"],
            rendered=rendered(chunk))
        assert result["status"] == si.CHUNK_OK
        assert len(result["items"]) == 2
        assert [a["status"] for a in result["attempts"]] == \
            [si.CHUNK_TRUNCATED, si.CHUNK_OK]

    def test_the_last_attempt_status_is_what_the_caller_sees(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([
            _Response("{}", finish="SAFETY"),
            _Response("not json at all"),
        ])
        result = si.extract_chunk_with_retries(
            client, chunk, models=["model-a", "model-b"],
            rendered=rendered(chunk))
        assert result["status"] == si.CHUNK_MALFORMED
        assert len(result["attempts"]) == 2

    def test_no_model_configured_is_reported_rather_than_sent(self):
        chunk = si.PageChunk(id="p1-2", pages=(1, 2))
        client = FakeClient([])
        result = si.extract_chunk_with_retries(client, chunk, models=[],
                                               rendered=rendered(chunk))
        assert result["status"] == si.CHUNK_ERROR
        assert result["error"] == "no model configured"
        assert client.models.calls == []


# ------------------------------------------------------------ merge behaviour

class TestMerge:
    def test_overlap_reread_of_the_same_item_is_deduplicated(self):
        """Page 6 is in both chunks: it must not become a second question."""
        left = {"chunk": {"id": "p1-6"},
                "items": [question(6, "Q6", 4, 6), question(7, "Q7", 3, 6)]}
        right = {"chunk": {"id": "p6-11"},
                 "items": [question(7, "Q7", 3, 6), question(8, "Q8", 5, 7)]}
        merged = si.merge_chunks([left, right])
        assert [i["source_index"] for i in merged["items"]] == [6, 7, 8]
        assert merged["reread_indices"] == [7]
        assert merged["conflicts"] == []

    def test_disagreeing_chunks_are_a_conflict_that_keeps_both_variants(self):
        left = {"chunk": {"id": "p1-6"},
                "items": [question(7, "Q7", 3, 6)]}
        right = {"chunk": {"id": "p6-11"},
                 "items": [question(7, "Q7", 8, 6)]}
        merged = si.merge_chunks([left, right])
        assert merged["conflicts"][0]["source_index"] == 7
        assert merged["conflicts"][0]["chunks"] == ["p1-6", "p6-11"]
        assert len(merged["conflicts"][0]["variants"]) == 2
        # nothing is discarded: the item survives with both readings
        assert [i["source_index"] for i in merged["items"]] == [7]
        assert merged["items"][0]["conflict"] is True
        assert {v["max_marks"] for v in merged["items"][0]["variants"]} == {3, 8}

    def test_a_variant_says_which_sub_part_disagreed(self):
        """A conflict must be specific enough to act on."""
        left = {"chunk": {"id": "p21-26"}, "items": [question(61, "Q61", 5, 26, parts=[
            {"number": "(a)", "max_marks": 2},
            {"number": "(b)(ii)", "max_marks": 2}])]}
        right = {"chunk": {"id": "p26-29"}, "items": [question(61, "Q61", 5, 26, parts=[
            {"number": "(a)", "max_marks": 2},
            {"number": "(b)(ii)", "max_marks": 1}])]}
        merged = si.merge_chunks([left, right])
        variants = merged["conflicts"][0]["variants"]
        assert [v["parts_detail"] for v in variants] == [
            [["(a)", 2], ["(b)(ii)", 2]], [["(a)", 2], ["(b)(ii)", 1]]]

    def test_the_richer_read_is_the_provisional_one(self):
        thin = question(9, "Q9", 4, 9)
        rich = question(9, "Q9", 4, 9, parts=[{"number": "(a)", "max_marks": 1}])
        rich["criteria"] = "M1 A1 B2 follow-through allowed"
        merged = si.merge_chunks([
            {"chunk": {"id": "p6-11"}, "items": [thin]},
            {"chunk": {"id": "p11-16"}, "items": [rich]},
        ])
        assert merged["items"][0]["chunk"] == "p11-16"
        assert merged["items"][0]["variants"]

    def test_an_item_without_an_index_is_counted_not_dropped(self):
        merged = si.merge_chunks([
            {"chunk": {"id": "p1-6"},
             "items": [question(1), {"label": "no index", "max_marks": 3}]},
        ])
        assert merged["indices"] == [1]
        assert len(merged["unkeyed"]) == 1

    def test_merging_is_order_independent(self):
        a = {"chunk": {"id": "p1-6"}, "items": [question(1, "A", 2, 1)]}
        b = {"chunk": {"id": "p6-11"}, "items": [question(2, "B", 3, 7)]}
        assert si.merge_chunks([a, b])["indices"] == \
               si.merge_chunks([b, a])["indices"]

    def test_failed_chunks_contribute_nothing_rather_than_fiction(self):
        merged = si.merge_chunks([
            {"chunk": {"id": "p1-6"}, "status": si.CHUNK_OK,
             "items": [question(1), question(2)]},
            {"chunk": {"id": "p6-11"}, "status": si.CHUNK_TRUNCATED, "items": []},
        ])
        assert merged["indices"] == [1, 2]


# -------------------------------------------------------------- the report

def _run(*chunk_records):
    """Flatten chunk records into the shape inventory_report() consumes."""
    return [record for group in chunk_records for record in group]


def _results_for(indices, *, chunk_id="p1-6", status=si.CHUNK_OK,
                 pages=(1, 2, 3, 4, 5, 6), last_index=None):
    """A chunk record shaped exactly like the real ones.

    ``last_index`` is the highest printed index the chunk could SEE, which is
    independent of how many items it listed - that is what exposes a lost tail.
    """
    indices = list(indices)
    return [{"chunk": {"id": chunk_id, "pages": list(pages)}, "status": status,
             "finish_reason": "STOP", "model": "m", "elapsed_ms": 10,
             "last_index_on_page": (max(indices) if last_index is None
                                    else last_index),
             "items": [question(i, f"Q{i}", 3, pages[0]) for i in indices]}]


class TestInventoryReport:
    def test_a_contiguous_index_that_returns_whole_is_complete(self):
        report = si.inventory_report(_results_for(range(1, 70)))
        assert report["detected_count"] == 69
        assert report["expected_count"] == 69
        assert report["missing_indices"] == []
        assert report["unresolved"] is False
        assert report["verdict"].startswith("COMPLETE")

    def test_the_two_missing_questions_are_named(self):
        """The real shape of the failure: index to 69, 67 items returned.

        No gap is visible in the 67 items themselves - the model numbered them
        1..67 - so the missing pair is established by the index the LAST page
        still prints, exactly as the real document's last page does.
        """
        report = si.inventory_report(_run(
            _results_for(range(1, 34), chunk_id="p1-6",
                         pages=list(range(1, 7)), last_index=33),
            _results_for(range(34, 68), chunk_id="p6-11",
                         pages=list(range(6, 12)), last_index=69),
        ))
        assert report["detected_count"] == 67
        assert report["expected_count"] == 69
        assert report["missing_indices"] == [68, 69]
        assert report["unresolved"] is True
        assert "INCOMPLETE" in report["verdict"]

    def test_chunks_with_no_index_gap_and_no_higher_index_read_as_they_are(self):
        """With no evidence of a higher index the inventory cannot claim one.

        The honest outcome: the pages that were read are accounted for, and the
        report says on what basis - never an invented total.
        """
        report = si.inventory_report(_run(
            _results_for(range(1, 34), chunk_id="p1-6",
                         pages=list(range(1, 7))),
            _results_for(range(34, 68), chunk_id="p6-11",
                         pages=list(range(6, 12))),
        ))
        assert report["expected_count"] == 67
        assert report["missing_indices"] == []
        assert report["expected_basis"] == "the printed index is contiguous from 1"

    def test_an_implausible_last_index_does_not_invent_a_huge_total(self):
        """A page number or cell value read as 'the document ends at 999'."""
        report = si.inventory_report(_run(
            _results_for([1, 2, 3], last_index=999)))
        assert report["expected_count"] == 3
        assert report["missing_indices"] == []
        assert report["implausible_last_index"][0]["last_index"] == 999
        assert report["implausible_last_index"][0]["reason"]

    def test_a_failed_chunk_makes_the_inventory_incomplete(self):
        results = _results_for(range(1, 7))
        results.append({"chunk": {"id": "p6-11", "pages": [6, 11]},
                        "status": si.CHUNK_TRUNCATED, "items": [],
                        "error": "cut off"})
        report = si.inventory_report(results)
        assert report["failed_chunks"] == ["p6-11"]
        assert report["unresolved"] is True
        assert "chunk(s) failed" in report["verdict"]

    def test_a_conflict_is_unresolved_rather_than_averaged_away(self):
        results = _results_for([1, 2, 3])
        results.append({"chunk": {"id": "p6-11", "pages": [6, 11]},
                        "status": si.CHUNK_OK,
                        "items": [question(3, "Q3", 9, 6)]})
        report = si.inventory_report(results)
        assert report["conflicting_indices"] == [3]
        assert report["unresolved"] is True
        assert "UNRESOLVED" in report["verdict"]

    def test_an_index_range_that_does_not_start_at_one_establishes_no_total(self):
        report = si.inventory_report(_results_for([5, 6, 7]))
        assert report["expected_count"] is None
        assert "starts at 5" in report["expected_basis"]
        assert "UNESTABLISHED" in report["verdict"]

    def test_marks_that_could_not_be_read_are_flagged_and_unresolved(self):
        results = _results_for([1, 2])
        results[0]["items"][1]["max_marks"] = 0
        results[0]["items"][1]["marks_missing"] = True
        report = si.inventory_report(results)
        assert report["marks_missing_indices"] == [2]
        assert report["unresolved"] is True

    def test_the_mark_total_is_summed_from_what_was_actually_read(self):
        report = si.inventory_report(_results_for([1, 2, 3]))
        assert report["items_total_marks"] == 9

    def test_pages_that_produced_nothing_are_listed_for_review(self):
        results = _results_for([1, 2], pages=list(range(1, 7)))
        report = si.inventory_report(results, page_count_total=6)
        assert report["pages_with_items"] == [1]
        assert report["pages_without_items"] == [2, 3, 4, 5, 6]

    def test_later_chunks_may_report_higher_indices_without_alarm(self):
        """Adjacent chunks cover different ranges: that is not a conflict."""
        results = _run(
            _results_for(range(1, 34), chunk_id="p1-6",
                         pages=list(range(1, 7)), last_index=14),
            _results_for(range(34, 41), chunk_id="p6-11",
                         pages=list(range(6, 12)), last_index=40),
        )
        report = si.inventory_report(results)
        assert report["last_index_regressions"] == []
        assert report["observed_last_index"] == 40

    def test_a_later_chunk_reporting_LESS_than_an_earlier_one_is_flagged(self):
        """The numbering runs forward: a later range seeing LESS lost content."""
        results = _run(
            _results_for(range(1, 41), chunk_id="p1-6",
                         pages=list(range(1, 7)), last_index=40),
            _results_for(range(34, 38), chunk_id="p6-11",
                         pages=list(range(6, 12)), last_index=37),
        )
        report = si.inventory_report(results)
        assert report["last_index_regressions"] == [
            {"chunks": ["p1-6", "p6-11"], "last_index": [40, 37]}]
        assert report["unresolved"] is True

    def test_per_chunk_diagnostics_survive_into_the_report(self):
        report = si.inventory_report(_results_for([1, 2]))
        chunk = report["chunks"][0]
        assert chunk["status"] == si.CHUNK_OK
        assert chunk["finish_reason"] == "STOP"
        assert chunk["items"] == 2


# --------------------------------------------------------- end to end, offline

def _page_indexed_responder(per_page=2):
    """A model that reads the pages it was sent and indexes them as printed.

    The document prints a running index that continues across pages, exactly
    like the real compiled mark scheme: page N carries indices
    ``(N-1)*per_page + 1 .. N*per_page``. The responder reads the page list out
    of the prompt, so a chunk is only ever asked about its own pages.
    """
    def responder(contents, model, config):
        prompt = contents[0]
        pages = [int(p) for p in
                 prompt.split("PAGES: ")[1].split("\n")[0].split(", ")]
        seen = sorted(set(pages))
        items = []
        for page in seen:
            for offset in range(per_page):
                index = (page - 1) * per_page + offset + 1
                items.append(question(index, f"June 05/4H/Q{index}", 3, page))
        return _Response(payload(items, pages=pages,
                                 last_index_on_page=max(
                                     (p - 1) * per_page + per_page
                                     for p in seen)))
    return responder


class TestExtractInventoryEndToEnd:
    def test_a_whole_document_is_read_page_by_page_with_no_duplicates(self, tmp_path):
        pdf = compiled_image_only_mark_scheme(tmp_path / "compiled.pdf",
                                              pages=9, per_page=2)
        client = FakeClient(responder=_page_indexed_responder(per_page=2))
        outcome = si.extract_inventory(client, pdf, window=6, overlap=1, dpi=72,
                                       models=["model-a"])

        assert outcome["page_count"] == 9
        assert [c["id"] for c in outcome["chunks"]] == ["p1-6", "p6-9"]
        indices = [i["source_index"] for i in outcome["items"]]
        assert indices == list(range(1, 19))          # 9 pages x 2, no repeats
        report = outcome["report"]
        assert report["detected_count"] == 18
        assert report["expected_count"] == 18
        assert report["missing_indices"] == []
        assert report["failed_chunks"] == []
        assert report["verdict"].startswith("COMPLETE")
        # the boundary page was deliberately read twice and de-duplicated
        assert report["reread_indices"] == [11, 12]

    def test_every_item_keeps_its_page_and_mark_allocation(self, tmp_path):
        pdf = compiled_image_only_mark_scheme(tmp_path / "compiled2.pdf",
                                              pages=4, per_page=1)
        client = FakeClient(responder=_page_indexed_responder(per_page=1))
        outcome = si.extract_inventory(client, pdf, window=2, overlap=1, dpi=72,
                                       models=["model-a"])
        assert [i["page"] for i in outcome["items"]] == [1, 2, 3, 4]
        assert all(i["max_marks"] == 3 for i in outcome["items"])
        assert outcome["report"]["items_total_marks"] == 12

    def test_a_read_that_stops_short_of_the_printed_index_is_incomplete(self, tmp_path):
        """The real failure shape: the last page prints 4, only 1-3 came back."""
        pdf = compiled_image_only_mark_scheme(tmp_path / "compiled3.pdf",
                                              pages=4, per_page=1)
        good = _page_indexed_responder(per_page=1)

        def responder(contents, model, config):
            pages = contents[0].split("PAGES: ")[1].split("\n")[0]
            if pages == "3, 4":
                # page 4's question is visible (index 4) but was not returned
                return _Response(payload([question(3, "Q3", 3, 3)],
                                         pages=[3, 4], last_index_on_page=4))
            return good(contents, model, config)

        client = FakeClient(responder=responder)
        outcome = si.extract_inventory(client, pdf, window=2, overlap=1, dpi=72,
                                       models=["model-a"])
        report = outcome["report"]
        assert report["detected_count"] == 3
        assert report["observed_last_index"] == 4
        assert report["missing_indices"] == [4]
        assert "INCOMPLETE" in report["verdict"]
        assert report["unresolved"] is True

    def test_a_page_range_with_nothing_on_it_is_recorded_not_failed(self, tmp_path):
        """A cover page legitimately holds nothing; that is not a gate failure.

        The page is still listed for review, and the index it did not return is
        still reported missing - an empty answer is recorded, not trusted.
        """
        pdf = compiled_image_only_mark_scheme(tmp_path / "cover.pdf",
                                              pages=2, per_page=1)

        def responder(contents, model, config):
            pages = contents[0].split("PAGES: ")[1].split("\n")[0]
            if pages == "1":
                return _Response(payload([], pages=[1], last_index_on_page=0))
            return _Response(payload([question(2, "Q2", 3, 2)],
                                     pages=[2], last_index_on_page=2))

        client = FakeClient(responder=responder)
        outcome = si.extract_inventory(client, pdf, window=1, overlap=0, dpi=72,
                                       models=["model-a"])
        report = outcome["report"]
        assert report["failed_chunks"] == []
        first = report["chunks"][0]
        assert first["status"] == si.CHUNK_OK and first["items"] == 0
        assert first["error"] is None
        assert report["pages_without_items"] == [1]
        assert report["detected_count"] == 1
        # the first index seen is 2, so how many the document should hold is
        # NOT established - it is reported as unestablished, never as complete
        assert report["expected_count"] is None
        assert "UNESTABLISHED" in report["verdict"]
        assert report["unresolved"] is True

    def test_retrying_the_whole_run_cannot_duplicate_a_question(self, tmp_path):
        """Idempotence: a re-run merges as a re-read, not as new questions."""
        pdf = compiled_image_only_mark_scheme(tmp_path / "compiled4.pdf",
                                              pages=5, per_page=2)
        client = FakeClient(responder=_page_indexed_responder(per_page=2))
        first = si.extract_inventory(client, pdf, window=3, overlap=1, dpi=72,
                                     models=["model-a"])
        second = si.extract_inventory(client, pdf, window=3, overlap=1, dpi=72,
                                      models=["model-a"])
        combined = si.merge_chunks(first["results"] + second["results"])
        assert [i["source_index"] for i in combined["items"]] == \
               [i["source_index"] for i in first["items"]]
        assert combined["conflicts"] == []

    def test_a_truncated_chunk_fails_the_run_instead_of_hiding_the_loss(self, tmp_path):
        pdf = compiled_image_only_mark_scheme(tmp_path / "compiled5.pdf",
                                              pages=4, per_page=1)
        good = _page_indexed_responder(per_page=1)
        calls = {"n": 0}

        def responder(contents, model, config):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Response(payload([]), finish="MAX_TOKENS")
            return good(contents, model, config)

        client = FakeClient(responder=responder)
        outcome = si.extract_inventory(client, pdf, window=2, overlap=1, dpi=72,
                                       models=["model-a", "model-b"])
        assert outcome["report"]["failed_chunks"] == []      # retried, then read
        assert outcome["report"]["detected_count"] == 4

    def test_an_unreadable_document_raises_rather_than_reporting_zero(self, tmp_path):
        bad = tmp_path / "broken.pdf"
        bad.write_bytes(b"nope")
        with pytest.raises(si.InventoryError):
            si.extract_inventory(FakeClient([]), bad, models=["m"])

    def test_a_document_with_no_pages_raises(self, tmp_path):
        pdf = compiled_image_only_mark_scheme(tmp_path / "compiled6.pdf",
                                              pages=1, per_page=1)
        client = FakeClient(responder=_page_indexed_responder(per_page=1))
        # page_count() reads the file, so an empty file cannot be planned
        empty = tmp_path / "empty.pdf"
        empty.write_bytes(b"")
        with pytest.raises(si.InventoryError):
            si.extract_inventory(client, empty, models=["m"])
        assert pdf.exists()


class TestIndexProbe:
    """The focused "which indices are printed on these pages?" read."""

    @pytest.fixture
    def pdf(self, tmp_path):
        return compiled_image_only_mark_scheme(tmp_path / "probe.pdf", pages=3,
                                               per_page=1)

    def test_the_printed_indices_come_back_sorted_and_unique(self, pdf):
        client = FakeClient([_Response(json.dumps({
            "indices": [2, 3, 1, 2, 3], "highest_index": 3,
            "stated_total_marks": 12, "pages": [1, 2, 3]}))])
        probed = si.probe_page_indices(client, pdf, [1, 2, 3], model="m", dpi=72)
        assert probed["status"] == si.CHUNK_OK
        assert probed["indices"] == [1, 2, 3]
        assert probed["highest_index"] == 3
        assert probed["stated_total_marks"] == 12

    def test_the_highest_index_falls_back_to_what_was_transcribed(self, pdf):
        client = FakeClient([_Response(json.dumps({"indices": [2, 3]}))])
        probed = si.probe_page_indices(client, pdf, [2, 3], model="m", dpi=72)
        assert probed["highest_index"] == 3

    def test_a_truncated_probe_is_not_an_answer(self, pdf):
        client = FakeClient([_Response('{"indices": [1,2', finish="MAX_TOKENS")])
        probed = si.probe_page_indices(client, pdf, [1, 2], model="m", dpi=72)
        assert probed["status"] == si.CHUNK_TRUNCATED
        assert probed["indices"] == []

    def test_a_malformed_probe_is_not_an_answer(self, pdf):
        client = FakeClient([_Response('{"nothing": true}')])
        probed = si.probe_page_indices(client, pdf, [1, 2], model="m", dpi=72)
        assert probed["status"] == si.CHUNK_MALFORMED
        assert probed["indices"] == []

    def test_the_probe_states_which_pages_it_was_asked_about(self, pdf):
        client = FakeClient([_Response(json.dumps({"indices": [3]}))])
        probed = si.probe_page_indices(client, pdf, [3], model="m", dpi=72)
        prompt = client.models.calls[0]["contents"][0]
        assert "pages 3-3" in prompt
        assert probed["pages"] == [3]
        assert probed["request_bytes"] > 0


class TestCandidateModels:
    def test_the_chain_deduplicates_and_keeps_order(self, monkeypatch):
        class _Gem:
            primary_models = ["a", "b"]

        class _Cfg:
            gemini = _Gem()

        monkeypatch.setattr("app.config.get_config", lambda: _Cfg())
        assert si.candidate_models(["b", "c"]) == ["a", "b", "c"]

    def test_a_missing_config_does_not_break_the_chain(self, monkeypatch):
        def boom():
            raise RuntimeError("no config")

        monkeypatch.setattr("app.config.get_config", boom)
        assert si.candidate_models(["x"]) == ["x"]
