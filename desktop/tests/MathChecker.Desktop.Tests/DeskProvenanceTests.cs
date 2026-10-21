using MathChecker.Desktop.Models;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

public class DeskProvenanceTests
{
    [Fact]
    public void A_teacher_mark_wins_over_every_engine_warning()
    {
        var question = new QuestionMarkDto
        {
            Status = "REVIEW",
            ReviewReason = "Ambiguous working",
            TeacherMark = 3,
            SchemeCheck = new SchemeCheckDto { DisagreesWithScheme = true },
        };

        var provenance = DeskProvenance.ForQuestion(question, resultVerified: false);

        Assert.Equal(QuestionState.Yours, provenance.State);
        Assert.Equal("Your mark", provenance.Label);
        Assert.Equal("✓", provenance.Glyph);
        Assert.False(provenance.IsAttention);
    }

    [Fact]
    public void A_review_flag_is_not_marked_and_keeps_the_reason()
    {
        var question = new QuestionMarkDto
        {
            Status = "REVIEW",
            ReviewReason = "Handwriting unclear",
        };

        var provenance = DeskProvenance.ForQuestion(question, resultVerified: false);

        Assert.Equal(QuestionState.Unmarked, provenance.State);
        Assert.Equal("Not marked", provenance.Label);
        Assert.Equal("amber", provenance.Tone);
        Assert.True(provenance.IsAttention);
        Assert.Contains("Handwriting unclear", provenance.AttentionText);
    }

    [Fact]
    public void A_scheme_check_is_shown_even_when_the_question_was_marked()
    {
        var question = new QuestionMarkDto
        {
            SchemeCheck = new SchemeCheckDto
            {
                DisagreesWithScheme = true,
                SchemeAnswer = "7",
                Evidence = "The scheme adds incorrectly",
                DiscrepancyType = "arithmetic",
            },
        };

        var provenance = DeskProvenance.ForQuestion(question, resultVerified: false);

        Assert.Equal(QuestionState.SchemeCheck, provenance.State);
        Assert.Equal("Scheme check", provenance.Label);
        Assert.Contains("arithmetic", provenance.AttentionText);
    }

    [Fact]
    public void Reported_and_explicitly_unmarked_subquestions_are_partial()
    {
        var reported = new QuestionMarkDto { UnmarkedSubquestions = new List<string> { "(ii)" } };
        var explicitStatus = new QuestionMarkDto
        {
            Subquestions = new List<SubquestionDto>
            {
                new() { Label = "(iii)", Status = "UNMARKED" },
            },
        };

        Assert.Equal(QuestionState.Partial,
            DeskProvenance.ForQuestion(reported, resultVerified: false).State);
        Assert.Equal(QuestionState.Partial,
            DeskProvenance.ForQuestion(explicitStatus, resultVerified: false).State);
        Assert.Contains("(ii)",
            DeskProvenance.ForQuestion(reported, resultVerified: false).AttentionText);
    }

    [Fact]
    public void A_subquestion_without_a_status_is_not_a_missing_mark()
    {
        var question = new QuestionMarkDto
        {
            Subquestions = new List<SubquestionDto>
            {
                new() { Label = "(i)", Status = null },
            },
        };

        Assert.Equal(QuestionState.Ai,
            DeskProvenance.ForQuestion(question, resultVerified: false).State);
    }

    [Fact]
    public void Verified_is_the_neutral_fallback_only_when_nothing_else_applies()
    {
        Assert.Equal(QuestionState.Verified,
            DeskProvenance.ForQuestion(new QuestionMarkDto(), resultVerified: true).State);
        Assert.Equal(QuestionState.Ai,
            DeskProvenance.ForQuestion(new QuestionMarkDto(), resultVerified: false).State);
    }

    [Fact]
    public void Paper_provenance_uses_reviews_before_confidence_flags()
    {
        var detail = new ResultDetailDto
        {
            Confidence = "LOW",
            ReviewRequired = 1,
            ManualOverride = 1,
        };

        Assert.Equal("Approved by you",
            DeskProvenance.ForPaper(detail, null,
                new ResultReviewEntryDto { Action = "approved" }).Label);
        Assert.Equal("Overridden",
            DeskProvenance.ForPaper(detail, null,
                new ResultReviewEntryDto { Action = "overridden" }).Label);
        Assert.Equal("Needs review",
            DeskProvenance.ForPaper(detail, null, null).Label);
    }

    [Fact]
    public void Paper_provenance_reports_incomplete_coverage_with_the_counts()
    {
        var verification = new VerificationDto
        {
            Partial = true,
            Coverage = new CoverageDto { Expected = 60, Marked = 38, RequiresReview = 2 },
        };

        var provenance = DeskProvenance.ForPaper(new ResultDetailDto(), verification, null);

        Assert.Equal("Incomplete · 38/60 marked", provenance.Label);
        Assert.Equal("amber", provenance.Tone);
        Assert.False(provenance.IsApproved);
    }

    [Fact]
    public void Paper_provenance_falls_back_to_verified_then_ai()
    {
        var verified = new VerificationDto { Verified = true };

        Assert.Equal("Verified",
            DeskProvenance.ForPaper(new ResultDetailDto(), verified, null).Label);
        Assert.Equal("AI marks", DeskProvenance.ForPaper(new ResultDetailDto(), null, null).Label);
    }
}
