using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>How one question's mark came to be — first match wins (spec §4.1).</summary>
public enum QuestionState
{
    Yours,
    Unmarked,
    SchemeCheck,
    Partial,
    Verified,
    Ai,
}

/// <summary>
/// A recorded provenance, never an inference: the label, glyph and tone all
/// come from fields the backend stored, so the rail cannot claim "verified"
/// for a question the engine never checked.
/// </summary>
public sealed record QuestionProvenance(
    QuestionState State,
    string Label,
    string Glyph,
    string Tone,
    IReadOnlyList<string> Attention)
{
    public bool IsAttention => Attention.Count > 0;

    public string AttentionText => string.Join(" · ", Attention);
}

/// <summary>Per-paper provenance for the header chip (spec §4.2).</summary>
public sealed record PaperProvenance(string Label, string Glyph, string Tone, bool IsApproved)
{
    public static readonly PaperProvenance Ai = new("AI marks", "✦", "neutral", false);
}

/// <summary>
/// The desk's provenance rules in one place. Two levels only: per question and
/// per paper. Both take recorded fields; nothing here guesses what the engine
/// "probably" meant.
/// </summary>
public static class DeskProvenance
{
    public static QuestionProvenance ForQuestion(QuestionMarkDto question, bool resultVerified)
    {
        ArgumentNullException.ThrowIfNull(question);

        // A teacher mark settles the question: whatever the engine distrusted
        // before, the recorded value is the teacher's now.
        if (question.TeacherMark is not null)
        {
            return new QuestionProvenance(QuestionState.Yours, "Your mark", "✓", "lime",
                Array.Empty<string>());
        }

        if (string.Equals(question.Status, "REVIEW", StringComparison.OrdinalIgnoreCase))
        {
            var reason = string.IsNullOrWhiteSpace(question.ReviewReason)
                ? "The engine asked for a human decision."
                : question.ReviewReason!;
            return new QuestionProvenance(QuestionState.Unmarked, "Not marked", "○", "amber",
                new[] { "Review: " + reason });
        }

        if (question.SchemeCheck is not null)
        {
            var check = question.SchemeCheck;
            var detail = new List<string> { "Scheme check" };
            if (!string.IsNullOrWhiteSpace(check.DiscrepancyType))
            {
                detail.Add(check.DiscrepancyType!);
            }
            else if (!string.IsNullOrWhiteSpace(check.Evidence))
            {
                detail.Add(check.Evidence!);
            }

            return new QuestionProvenance(QuestionState.SchemeCheck, "Scheme check", "⚠", "amber",
                new[] { string.Join(": ", detail) });
        }

        var unmarked = UnmarkedSubquestions(question);
        if (unmarked.Count > 0)
        {
            return new QuestionProvenance(QuestionState.Partial, "Part unmarked", "◐", "amber",
                new[] { "Part unmarked: " + string.Join(", ", unmarked) });
        }

        if (resultVerified)
        {
            return new QuestionProvenance(QuestionState.Verified, "Verified", "✓✓", "mint",
                Array.Empty<string>());
        }

        return new QuestionProvenance(QuestionState.Ai, "AI", "✦", "neutral",
            Array.Empty<string>());
    }

    public static PaperProvenance ForPaper(ResultDetailDto? detail, VerificationDto? verification,
        ResultReviewEntryDto? latestReview)
    {
        if (latestReview is not null)
        {
            if (string.Equals(latestReview.Action, "approved", StringComparison.OrdinalIgnoreCase))
            {
                return new PaperProvenance("Approved by you", "✓", "lime-outline", true);
            }

            if (string.Equals(latestReview.Action, "overridden", StringComparison.OrdinalIgnoreCase))
            {
                return new PaperProvenance("Overridden", "=", "lime", false);
            }
        }

        if (detail is null)
        {
            return PaperProvenance.Ai;
        }

        var coverage = verification?.Coverage;
        if (verification?.Partial == true || coverage is { RequiresReview: > 0 })
        {
            var label = coverage is { Expected: > 0 }
                ? $"Incomplete · {coverage.Marked}/{coverage.Expected} marked"
                : "Incomplete correction";
            return new PaperProvenance(label, "◐", "amber", false);
        }

        var confidence = detail.Confidence?.ToUpperInvariant();
        if (detail.ReviewRequired == 1
            || confidence is "LOW" or "VALIDATION_FAILED" or "LARGE_SCORE_CHANGE")
        {
            return new PaperProvenance("Needs review", "⚠", "amber", false);
        }

        if (verification?.Verified == true)
        {
            return new PaperProvenance("Verified", "✓✓", "mint", false);
        }

        return PaperProvenance.Ai;
    }

    private static List<string> UnmarkedSubquestions(QuestionMarkDto question)
    {
        var unmarked = question.UnmarkedSubquestions
            .Where(label => !string.IsNullOrWhiteSpace(label))
            .Select(label => label!)
            .ToList();

        foreach (var sub in question.Subquestions)
        {
            // A subquestion with no status at all is not evidence of a missing
            // mark: legacy rows stored only marks. Only an explicit non-MARKED
            // status counts.
            if (sub.Status is not null && !sub.IsMarked)
            {
                unmarked.Add(string.IsNullOrWhiteSpace(sub.Label) ? sub.Id ?? "subquestion" : sub.Label!);
            }
        }

        return unmarked.Distinct(StringComparer.Ordinal).ToList();
    }
}
