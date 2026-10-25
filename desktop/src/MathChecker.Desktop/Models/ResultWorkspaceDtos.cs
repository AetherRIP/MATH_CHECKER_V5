namespace MathChecker.Desktop.Models;

/// <summary>
/// The per-question and per-submission shapes the Marking Desk needs, taken
/// from <c>legacy_marking.normalise_result</c> and <c>api/viewer_routes.py</c>.
/// A missing optional field stays null/default; nothing is inferred here.
/// </summary>
public sealed class MarkBreakdownItemDto
{
    public string? Code { get; set; }
    public bool Awarded { get; set; }
    public string? Reason { get; set; }
}

public sealed class SubquestionDto
{
    public string? Id { get; set; }
    public string? Label { get; set; }
    public string? ParentId { get; set; }
    public double? MaxMarks { get; set; }
    public double? AwardedMarks { get; set; }

    /// <summary>"MARKED" or "UNMARKED".</summary>
    public string? Status { get; set; }

    public List<string> Answers { get; set; } = new();
    public List<string> MarkCodes { get; set; } = new();

    public bool IsMarked => string.Equals(Status, "MARKED", StringComparison.OrdinalIgnoreCase);

    /// <summary>
    /// Anything the marker did not return as MARKED is UNMARKED, never a
    /// confirmed zero: the engine writes the row when it declared the part but
    /// received nothing for it.
    /// </summary>
    public bool IsUnmarked => !IsMarked;

    /// <summary>The identifier exactly as the engine stored it ("7a", "(ii)").</summary>
    public string LabelText => !string.IsNullOrWhiteSpace(Label) ? Label!
        : !string.IsNullOrWhiteSpace(Id) ? Id!
        : "Unnamed part";

    public string MarkText => IsUnmarked
        ? "—"
        : $"{Format(AwardedMarks)} / {Format(MaxMarks)}";

    public string VerdictText => IsUnmarked ? "Unmarked"
        : MaxMarks is null or <= 0 ? "Marked"
        : (AwardedMarks ?? 0) >= MaxMarks ? "Full marks"
        : (AwardedMarks ?? 0) <= 0 ? "No marks"
        : "Partial";

    /// <summary>ok | warn | err | muted — the desk's badge tone.</summary>
    public string Tone => IsUnmarked ? "muted" : VerdictText switch
    {
        "Full marks" => "ok",
        "No marks" => "err",
        "Partial" => "warn",
        _ => "muted",
    };

    public string AnswersText => Answers.Count > 0
        ? string.Join(" or ", Answers.Where(a => !string.IsNullOrWhiteSpace(a)))
        : "Not stated in the scheme";

    public bool HasAnswers => Answers.Any(a => !string.IsNullOrWhiteSpace(a));

    public string MarkCodesText => MarkCodes.Count > 0
        ? string.Join(", ", MarkCodes.Where(c => !string.IsNullOrWhiteSpace(c)))
        : "No marking points listed";

    public bool HasMarkCodes => MarkCodes.Any(c => !string.IsNullOrWhiteSpace(c));

    private static string Format(double? value) => value is null ? "—" : value.Value.ToString("0.##");
}

/// <summary>
/// Present only when the engine had a defensible mathematical reason to
/// disagree with the supplied mark scheme. Absence is the normal case.
/// </summary>
public sealed class SchemeCheckDto
{
    public bool DisagreesWithScheme { get; set; }
    public bool StudentAnswerValid { get; set; }
    public string? SchemeAnswer { get; set; }
    public string? IndependentlyVerifiedAnswer { get; set; }
    public string? DiscrepancyType { get; set; }
    public string? Evidence { get; set; }
    public double? Confidence { get; set; }
    public double? JustifiedMarks { get; set; }

    /// <summary>The engine's label ("incorrect_answer") written for a teacher:
    /// "Incorrect answer". The raw value is never shown.</summary>
    public string DiscrepancyTypeText => Core.UI.PayloadFacts.StatusLabel(DiscrepancyType);

    public bool HasDiscrepancyType => !string.IsNullOrWhiteSpace(DiscrepancyType);
}

/// <summary>One row of <c>result_json.questions</c>.</summary>
public sealed class QuestionMarkDto
{
    // Keys the engine writes (legacy_marking.normalise_result).
    public string? QuestionNumber { get; set; }
    public double? MaxMarks { get; set; }
    public double? AwardedMarks { get; set; }

    // Older stored rows used these shorter keys; kept so they still render.
    public string? Number { get; set; }
    public string? Question { get; set; }
    public double? Awarded { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Max { get; set; }

    public string? Topic { get; set; }
    public string? StudentAnswer { get; set; }
    public string? WorkingSummary { get; set; }
    public string? LossReason { get; set; }
    public string? CorrectMethod { get; set; }
    public string? FullMarkSolution { get; set; }

    /// <summary>"VALID" or "REVIEW"; null on results stored before the flag existed.</summary>
    public string? Status { get; set; }

    public string? ReviewReason { get; set; }
    public double? TeacherMark { get; set; }
    public List<MarkBreakdownItemDto> MarkBreakdown { get; set; } = new();
    public List<SubquestionDto> Subquestions { get; set; } = new();
    public List<string> UnmarkedSubquestions { get; set; } = new();
    public SchemeCheckDto? SchemeCheck { get; set; }

    // Presentation helpers used by the existing results table.
    public string NumberText => QuestionNumber ?? Number ?? Question ?? "—";

    public int? AwardedValue => (int?)(AwardedMarks ?? Awarded ?? Earned);
    public int? MaxValue => (int?)(MaxMarks ?? Max ?? Available);

    public string MarkText =>
        $"{AwardedValue?.ToString() ?? "—"} / {MaxValue?.ToString() ?? "—"}";

    public bool HasSchemeCheck => SchemeCheck is not null;
}

/// <summary>Retention setting the backend reports alongside page availability.</summary>
public sealed class RetentionStateDto
{
    public string? KeepSubmissions { get; set; }
    public int KeepDays { get; set; }
    public bool Keeps { get; set; }
}

/// <summary>
/// <c>GET /api/results/{id}/pages</c>. When <see cref="Available"/> is false the
/// backend also supplies a specific <see cref="Reason"/> and teacher-facing
/// <see cref="Message"/>; the Desk must show those verbatim rather than invent
/// a cause.
/// </summary>
public sealed class ResultPagesDto
{
    public string? ResultId { get; set; }
    public bool Available { get; set; }
    public string? Reason { get; set; }
    public string? Message { get; set; }
    public bool Retryable { get; set; }
    public string? JobStatus { get; set; }
    public RetentionStateDto? Retention { get; set; }
    public double? CheckedAt { get; set; }
    public int PageCount { get; set; }
    public string? FileKind { get; set; }
    public string? FileName { get; set; }
    public string? Source { get; set; }
    public int Dpi { get; set; }
    public string? PageUrlTemplate { get; set; }
}

public sealed class CoverageDto
{
    public int Expected { get; set; }
    public int Marked { get; set; }
    public int RequiresReview { get; set; }
    public int Unanswered { get; set; }
    public bool Verified { get; set; }
}

/// <summary>Typed view of <c>verification_json</c>.</summary>
public sealed class VerificationDto
{
    public bool Verified { get; set; }
    public string? Status { get; set; }
    public List<string> Anomalies { get; set; } = new();
    public List<string> Warnings { get; set; } = new();
    public List<string> ReviewQuestions { get; set; } = new();
    public bool Partial { get; set; }
    public double? MarkedPercentage { get; set; }
    public double? PaperTotalMarks { get; set; }
    public double? UnresolvedMarks { get; set; }
    public CoverageDto? Coverage { get; set; }
}

/// <summary>One append-only teacher decision from <c>result_reviews</c>.</summary>
public sealed class ResultReviewEntryDto
{
    /// <summary>"approved" or "overridden".</summary>
    public string? Action { get; set; }

    public double? PreviousEarned { get; set; }
    public double? NewEarned { get; set; }
    public string? PreviousConfidence { get; set; }
    public string? Note { get; set; }
    public double? CreatedAt { get; set; }

    public string ActionText => Action switch
    {
        "approved" => "Approved",
        "overridden" => "Overridden",
        _ => Action ?? "Decision",
    };
}

public sealed class ResultReviewHistoryDto
{
    public string? ResultId { get; set; }
    public List<ResultReviewEntryDto> History { get; set; } = new();
}
