using System.Text.Json;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// Contract tests for the per-question and per-submission shapes the Marking
/// Desk reads. Every fixture uses the keys the Python engine actually writes
/// (legacy_marking.normalise_result), not a convenient invention.
/// </summary>
public class ResultWorkspaceDtoTests
{
    private const string DetailBody = """
        {
          "id": "r4",
          "student_name": "Amara O.",
          "earned": 14,
          "available": 20,
          "percentage": 70.0,
          "result_json": {
            "paper_info": {"paper": "Paper 3", "session": "June 2026"},
            "questions": [
              {
                "question_number": "4(b)",
                "max_marks": 3,
                "awarded_marks": 2,
                "topic": "Algebra",
                "student_answer": "x = 4.5",
                "working_summary": "solves correctly",
                "loss_reason": "final value not rounded",
                "mark_breakdown": [
                  {"code": "M1", "awarded": true, "reason": "correct substitution"},
                  {"code": "A1", "awarded": false, "reason": "final value"}
                ],
                "scheme_check": {
                  "disagrees_with_scheme": true,
                  "student_answer_valid": true,
                  "scheme_answer": "4.6",
                  "independently_verified_answer": "4.5",
                  "discrepancy_type": "rounding",
                  "evidence": "truncation is valid here",
                  "confidence": 0.82,
                  "justified_marks": 3
                },
                "status": "REVIEW",
                "review_reason": "two sources disagree",
                "teacher_mark": 2,
                "subquestions": [
                  {"id": "4b-i", "label": "(i)", "max_marks": 1, "awarded_marks": 1,
                   "status": "MARKED", "answers": ["4.5"], "mark_codes": ["M1"]},
                  {"id": "4b-ii", "label": "(ii)", "max_marks": 2, "awarded_marks": 1,
                   "status": "UNMARKED"}
                ],
                "unmarked_subquestions": ["(ii)"]
              }
            ]
          },
          "verification_json": {
            "verified": true,
            "status": "REVIEW",
            "anomalies": ["one question was not marked"],
            "warnings": [],
            "review_questions": ["4(b)"],
            "partial": true,
            "coverage": {"expected": 60, "marked": 38, "requires_review": 2,
                         "unanswered": 20, "verified": false},
            "paper_total_marks": 60,
            "unresolved_marks": 22
          }
        }
        """;

    [Fact]
    public void Question_reads_marks_breakdown_scheme_check_and_subquestions()
    {
        var detail = JsonSerializer.Deserialize<ResultDetailDto>(DetailBody, Json.Options)!;

        var question = Assert.Single(detail.Questions());

        Assert.Equal("4(b)", question.NumberText);
        Assert.Equal(2, question.AwardedValue!.Value);
        Assert.Equal(3, question.MaxValue!.Value);
        Assert.Equal("2 / 3", question.MarkText);

        Assert.Equal(2, question.MarkBreakdown.Count);
        Assert.True(question.MarkBreakdown[0].Awarded);
        Assert.Equal("M1", question.MarkBreakdown[0].Code);
        Assert.False(question.MarkBreakdown[1].Awarded);

        Assert.NotNull(question.SchemeCheck);
        Assert.True(question.SchemeCheck!.DisagreesWithScheme);
        Assert.Equal(0.82, question.SchemeCheck.Confidence!.Value, 3);
        Assert.Equal(3d, question.SchemeCheck.JustifiedMarks!.Value, 3);

        Assert.Equal(2, question.Subquestions.Count);
        Assert.True(question.Subquestions[0].IsMarked);
        Assert.False(question.Subquestions[1].IsMarked);
        Assert.Equal("(ii)", Assert.Single(question.UnmarkedSubquestions));

        Assert.Equal("REVIEW", question.Status);
        Assert.Equal("two sources disagree", question.ReviewReason);
        Assert.Equal(2d, question.TeacherMark!.Value, 3);
        Assert.True(question.HasSchemeCheck);
    }

    [Fact]
    public void Verification_is_typed_and_cached()
    {
        var detail = JsonSerializer.Deserialize<ResultDetailDto>(DetailBody, Json.Options)!;

        var verification = detail.Verification();

        Assert.NotNull(verification);
        Assert.True(verification!.Verified);
        Assert.True(verification.Partial);
        Assert.Equal("4(b)", Assert.Single(verification.ReviewQuestions));
        Assert.Equal(60d, verification.PaperTotalMarks!.Value, 3);
        Assert.Equal(22d, verification.UnresolvedMarks!.Value, 3);
        Assert.Equal(38, verification.Coverage!.Marked);
        Assert.Equal(60, verification.Coverage.Expected);
        Assert.Equal(2, verification.Coverage.RequiresReview);
        Assert.Equal(20, verification.Coverage.Unanswered);

        // Same instance on the second read: bindings call this repeatedly.
        Assert.Same(verification, detail.Verification());
    }

    [Fact]
    public void Verification_is_null_when_the_row_has_none()
    {
        const string body = """{"id":"r1","result_json":{"questions":[]},"earned":1,"available":2}""";
        var detail = JsonSerializer.Deserialize<ResultDetailDto>(body, Json.Options)!;

        Assert.Null(detail.Verification());
        Assert.Empty(detail.Questions());
    }

    [Fact]
    public void Pages_payload_keeps_the_backends_reason_and_retention()
    {
        const string body = """
            {"result_id":"r1","available":false,"reason":"retention_disabled",
             "message":"Submission retention is OFF, so the original is deleted when a correction finishes.",
             "retryable":false,"job_status":"COMPLETED",
             "retention":{"keep_submissions":"off","keep_days":0,"keeps":false},
             "checked_at":1791815824.0,"page_count":0,"file_kind":null,"file_name":null,
             "source":null,"dpi":150,
             "page_url_template":"/api/results/r1/pages/{page}.png"}
            """;

        var pages = JsonSerializer.Deserialize<ResultPagesDto>(body, Json.Options)!;

        Assert.False(pages.Available);
        Assert.Equal("retention_disabled", pages.Reason);
        Assert.Equal("COMPLETED", pages.JobStatus);
        Assert.Contains("retention is OFF", pages.Message!);
        Assert.False(pages.Retention!.Keeps);
        Assert.Equal(150, pages.Dpi);
        Assert.EndsWith("/pages/{page}.png", pages.PageUrlTemplate!);
    }

    [Fact]
    public void Pages_payload_carries_the_real_page_count()
    {
        const string body = """
            {"result_id":"r1","available":true,"reason":null,"message":null,
             "retryable":false,"job_status":"COMPLETED",
             "retention":{"keep_submissions":"always","keep_days":0,"keeps":true},
             "checked_at":1791815824.0,"page_count":7,"file_kind":"pdf",
             "file_name":"answers.pdf","source":"retained","dpi":150,
             "page_url_template":"/api/results/r1/pages/{page}.png"}
            """;

        var pages = JsonSerializer.Deserialize<ResultPagesDto>(body, Json.Options)!;

        Assert.True(pages.Available);
        Assert.Equal(7, pages.PageCount);
        Assert.Equal("pdf", pages.FileKind);
        Assert.Equal("retained", pages.Source);
        Assert.True(pages.Retention!.Keeps);
    }

    [Fact]
    public void Review_history_reads_the_decision_action()
    {
        const string body = """
            {"result_id":"r1","history":[
              {"action":"approved","previous_earned":14,"new_earned":14,
               "previous_confidence":"LOW","note":"Reviewed in the desk","created_at":1791815824.0},
              {"action":"overridden","previous_earned":12,"new_earned":14,
               "previous_confidence":"HIGH","note":null,"created_at":1791815700.0}]}
            """;

        var history = JsonSerializer.Deserialize<ResultReviewHistoryDto>(body, Json.Options)!;

        Assert.Equal(2, history.History.Count);
        Assert.Equal("Approved", history.History[0].ActionText);
        Assert.Equal("Overridden", history.History[1].ActionText);
        Assert.Equal(1791815824.0, history.History[0].CreatedAt!.Value, 3);
    }
}
