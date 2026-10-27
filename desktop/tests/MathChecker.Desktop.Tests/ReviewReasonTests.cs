using System.Text.Json;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// A review must say what happened, at which stage, and what to do — not
/// "needs review". These tests pin the translation of the backend's coded
/// reasons into what the teacher actually reads, and the fact that older rows
/// (which carry prose only) still render.
/// </summary>
public class ReviewReasonTests
{
    private const string QueueBody = """
        {"items":[
          {"id":"r1","student_name":"Student A","student_id":"s1","coursework_id":"cw-1",
           "earned":3,"available":5,"percentage":60.0,"confidence":"LOW",
           "confidence_reasons":["Verification requested review for this correction."],
           "review_required":1,"manual_override":0,"created_at":1790000000,
           "assignment_title":"Algebra",
           "review_reasons":[
             {"code":"QUESTION_UNMARKED","stage":"mapping","severity":"review",
              "question":"7",
              "detail":"Question 7 is in the mark scheme but the marking run returned nothing for it.",
              "action":"Re-correct the paper; if it persists, check the scan covers that page."},
             {"code":"MARK_SCHEME_UNVERIFIED","stage":"mark_scheme","severity":"advisory",
              "question":null,
              "detail":"The mark scheme structure was never independently confirmed.",
              "action":"Recheck the mark scheme once."}
           ]},
          {"id":"r2","student_name":"Student B","student_id":"s2",
           "earned":2,"available":5,"percentage":40.0,"confidence":"LOW",
           "confidence_reasons":["Verification requested review."],
           "review_required":1,"manual_override":0,"created_at":1790000000,
           "assignment_title":"Algebra"}
        ],"total":2}
        """;

    [Fact]
    public void A_coded_reason_reads_as_a_sentence_and_a_stage()
    {
        using var document = JsonDocument.Parse(QueueBody);
        var queue = JsonSerializer.Deserialize<ReviewListDto>(document.RootElement.GetRawText(), Json.Options);

        var row = Assert.Single(queue!.Items, i => i.Id == "r1");
        Assert.True(row.HasReviewReasons);
        Assert.Equal("2 reasons", row.ReasonsHeading);

        var unmarked = row.ReviewReasons[0];
        Assert.Equal("Question not marked · Q7", unmarked.Title);
        Assert.True(unmarked.NeedsHuman);
        Assert.Equal("Needs you", unmarked.SeverityText);
        Assert.Equal("question mapping", unmarked.StageText);
        Assert.Contains("covers that page", unmarked.ActionText);

        // The advisory is shown, and is not dressed up as a decision for the teacher.
        var advisory = row.ReviewReasons[1];
        Assert.False(advisory.NeedsHuman);
        Assert.Equal("Note", advisory.SeverityText);
        Assert.Equal("mark scheme", advisory.StageText);

        // The queue's Reason column must name the questions, not the prose.
        Assert.Contains("Q7: Question not marked", row.ReasonsText);
        Assert.DoesNotContain("Verification requested review", row.ReasonsText);
    }

    [Fact]
    public void A_row_saved_before_codes_existed_still_explains_itself()
    {
        using var document = JsonDocument.Parse(QueueBody);
        var queue = JsonSerializer.Deserialize<ReviewListDto>(document.RootElement.GetRawText(), Json.Options);

        var row = Assert.Single(queue!.Items, i => i.Id == "r2");
        Assert.False(row.HasReviewReasons);
        Assert.Equal("Verification requested review.", row.ReasonsText);
    }
}
