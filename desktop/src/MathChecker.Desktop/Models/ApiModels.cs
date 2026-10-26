using System.Text.Json;
using System.Text.Json.Serialization;

namespace MathChecker.Desktop.Models;

// The DTOs mirror the JSON actually produced by backend/app/api/*.py (shapes
// captured from a running backend and checked against the handlers). Unknown
// fields are ignored by System.Text.Json, so a backend that adds a field never
// breaks the desktop client.

public static class EpochTime
{
    private static readonly DateTime Epoch = new(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

    /// <summary>Backend timestamps are unix seconds (float). 0/null => null.</summary>
    public static DateTimeOffset? ToLocal(double? seconds)
    {
        if (seconds is null or <= 0)
        {
            return null;
        }

        try
        {
            return new DateTimeOffset(Epoch.AddSeconds(seconds.Value)).ToLocalTime();
        }
        catch (ArgumentOutOfRangeException)
        {
            return null;
        }
    }

    public static string Format(double? seconds, string empty = "—") =>
        ToLocal(seconds)?.ToString("yyyy-MM-dd HH:mm") ?? empty;

    /// <summary>Local time of day, for a stage that happened minutes ago.</summary>
    public static string Clock(double? seconds, string empty = "—") =>
        ToLocal(seconds)?.ToString("HH:mm:ss") ?? empty;
}

/// <summary>Byte counts in the unit a person would say out loud.</summary>
public static class SizeText
{
    public static string Of(long bytes)
    {
        if (bytes < 0)
        {
            return "—";
        }

        if (bytes < 1024)
        {
            return $"{bytes} B";
        }

        var kilobytes = bytes / 1024d;
        return kilobytes < 1024
            ? $"{kilobytes:0.#} KB"
            : $"{kilobytes / 1024d:0.##} MB";
    }
}

public sealed class HealthDto
{
    public string? Status { get; set; }
}

public sealed class ApiStatsDto
{
    public int Requests { get; set; }
    public int Successes { get; set; }
    public int Failures { get; set; }
    public int RateLimitEvents { get; set; }
    public int TimeoutErrors { get; set; }
}

public sealed class ApiHealthDto
{
    public int Healthy { get; set; }
    public int Cooldown { get; set; }
    public int Invalid { get; set; }
    public int Total { get; set; }
    public bool HasKeys { get; set; }
    public ApiStatsDto? Aggregate { get; set; }
}

public sealed class ClassroomSummaryDto
{
    public bool Configured { get; set; }
    public bool Connected { get; set; }
    public string? Email { get; set; }
    public double? LastSync { get; set; }
    public int Courses { get; set; }
    public int Allowlisted { get; set; }
    public int PendingSubmissions { get; set; }
}

public sealed class DashboardDto
{
    public int CorrectionsToday { get; set; }
    public int Pending { get; set; }
    public int Processing { get; set; }
    public int Completed { get; set; }
    public int Failed { get; set; }
    public Dictionary<string, int> Jobs { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public ApiHealthDto? ApiHealth { get; set; }
    public ClassroomSummaryDto? Classroom { get; set; }
    public string? Role { get; set; }
    public string? Account { get; set; }
    public bool MockMode { get; set; }
    public string? Version { get; set; }
    public string? BuildNumber { get; set; }
    public string? BuildDate { get; set; }
    public bool DemoMode { get; set; }
    public bool FirstRun { get; set; }
}

public sealed class AttentionItemDto
{
    public string? Type { get; set; }
    public string? Tone { get; set; }
    public string? Icon { get; set; }
    public string? Title { get; set; }
    public string? Detail { get; set; }
    public string? Route { get; set; }
    public int Count { get; set; }
}

public sealed class AttentionFeedDto
{
    public List<AttentionItemDto> Items { get; set; } = new();
    public int Total { get; set; }
}

public sealed class JobDto
{
    public string Id { get; set; } = string.Empty;
    public string? SubmissionId { get; set; }
    public string? CourseworkId { get; set; }
    public string? CourseId { get; set; }
    public string? StudentId { get; set; }
    public string? StudentName { get; set; }
    /// <summary>Canonical nickname (or Classroom name) resolved by the backend.</summary>
    public string? DisplayName { get; set; }
    public string? Status { get; set; }
    public string? StageDetail { get; set; }
    public double Progress { get; set; }
    public int Attempts { get; set; }
    public int MaxAttempts { get; set; }
    public string? WorkerId { get; set; }
    public int IsRemark { get; set; }
    public string? Error { get; set; }
    public string? ErrorCode { get; set; }
    public double? CreatedAt { get; set; }
    public double? StartedAt { get; set; }
    public double? FinishedAt { get; set; }
    public double? UpdatedAt { get; set; }
    public string? ResultId { get; set; }

    /// <summary>The uploaded paper's own filename and page count, as the
    /// backend recorded them when the job was queued (null when unknown: the
    /// UI must then say "Not available" rather than guess).</summary>
    public string? PaperName { get; set; }
    public int? PageCount { get; set; }

    /// <summary>Stages the backend actually reported, oldest first (detail
    /// endpoint only).</summary>
    public List<StageEventDto> StageHistory { get; set; } = new();

    /// <summary>The stored correction (detail endpoint, completed jobs only).
    /// Read for the final marks; never used before the job finished.</summary>
    public JsonElement? Result { get; set; }

    public JsonElement? Bandwidth { get; set; }

    /// <summary>Provider requests this correction actually sent, counted
    /// where they are sent (detail endpoint). Zero for jobs recorded before
    /// the ledger existed — which is shown as "not recorded", never as 0.</summary>
    public JsonElement? AiUsage { get; set; }

    /// <summary>Generation attempts across every worker attempt of this job
    /// (list endpoint).</summary>
    public int AiRequests { get; set; }

    /// <summary>"2 AI requests · marking 2 · 1 upload" — or empty when the job
    /// has no ledger (older rows, still-running first attempt).</summary>
    public string AiRequestsText
    {
        get
        {
            var usage = AiUsage;
            if (usage is null || usage.Value.ValueKind != JsonValueKind.Object)
            {
                return AiRequests > 0 ? $"{AiRequests} requests" : string.Empty;
            }

            var element = usage.Value;
            var total = Int(element, "requests");
            var stages = new List<string>();
            if (element.TryGetProperty("by_stage", out var byStage)
                && byStage.ValueKind == JsonValueKind.Object)
            {
                foreach (var item in byStage.EnumerateObject())
                {
                    var count = item.Value.ValueKind == JsonValueKind.Number
                        ? item.Value.GetInt32() : 0;
                    if (count > 0)
                    {
                        stages.Add($"{item.Name.Replace('_', ' ')} {count}");
                    }
                }
            }

            var text = total == 1 ? "1 AI request" : $"{total} AI requests";
            if (stages.Count > 0)
            {
                text += " · " + string.Join(", ", stages);
            }

            // Uploads and file lookups are provider calls too, but they are
            // not marking work: named separately so the numbers add up.
            var others = new List<string>();
            if (element.TryGetProperty("by_kind", out var byKind)
                && byKind.ValueKind == JsonValueKind.Object)
            {
                foreach (var item in byKind.EnumerateObject())
                {
                    var count = item.Value.ValueKind == JsonValueKind.Number
                        ? item.Value.GetInt32() : 0;
                    if (count <= 0 || item.Name == "generate")
                    {
                        continue;
                    }

                    var label = item.Name == "file_get" ? "file lookup" : item.Name;
                    others.Add($"{count} {label}{(count == 1 ? string.Empty : "s")}");
                }
            }

            if (others.Count > 0)
            {
                text += " · " + string.Join(", ", others);
            }

            var skipped = Int(element, "skipped");
            if (skipped > 0)
            {
                text += $" · {skipped} not sent (provider cooling)";
            }

            if (element.TryGetProperty("budget_exceeded", out var over)
                && over.ValueKind == JsonValueKind.True)
            {
                text += " · stopped by the request budget";
            }

            return text;
        }
    }

    private static int Int(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.Number
            ? value.GetInt32()
            : 0;

    /// <summary>"75 / 103 · 72.8%" for a completed job whose result is loaded,
    /// otherwise empty. Marks are never shown from a job that has not
    /// finished marking and validating.</summary>
    public string MarksText
    {
        get
        {
            if (!IsCompleted || Result is null || Result.Value.ValueKind != JsonValueKind.Object)
            {
                return string.Empty;
            }

            var element = Result.Value;
            var earned = Number(element, "earned");
            var available = Number(element, "available");
            var percentage = Number(element, "percentage");
            if (earned is null || available is null)
            {
                return string.Empty;
            }

            var marks = $"{earned:0.#} / {available:0.#}";
            return percentage is null ? marks : $"{marks} · {percentage:0.#}%";
        }
    }

    private static double? Number(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.Number
        && value.TryGetDouble(out var parsed)
            ? parsed
            : null;

    public string CreatedText => EpochTime.Format(CreatedAt);

    /// <summary>The job state written for a person, never the raw enum.</summary>
    public string StatusText => Core.UI.PayloadFacts.StatusLabel(Status);

    public string StageText => string.IsNullOrWhiteSpace(StageDetail) ? StatusText : StageDetail!;

    public string PaperText => string.IsNullOrWhiteSpace(PaperName) ? "Not available" : PaperName!;

    public string PagesText => PageCount is null or <= 0
        ? "Not available"
        : PageCount == 1 ? "1 page" : $"{PageCount} pages";

    /// <summary>What the pipeline is doing, in the words of its own status.
    /// A queued job says it is waiting; it never looks like it is marking.</summary>
    public string PhaseText => Status switch
    {
        "QUEUED" => "Waiting for a worker",
        "STARTING" => "Starting on a worker",
        "PREPARING" => "Preparing PDF",
        "DOWNLOADING" => "Downloading the paper",
        "UPLOADING" => "Sending document to Gemini",
        "MARKING" => "Evaluating student answers",
        "VERIFYING" => "Validating awarded marks",
        "SAVING" => "Saving results",
        "WAITING_FOR_API" => "Waiting for API capacity",
        "RETRYING" => "Retrying after a failure",
        "COMPLETED" => "Correction completed",
        "FAILED" => "Correction failed",
        "CANCELLED" => "Correction cancelled",
        _ => StatusText,
    };

    public bool IsCompleted => Status == "COMPLETED";
    public bool IsFailed => Status == "FAILED";
    public bool IsCancelled => Status == "CANCELLED";
    public bool IsQueued => Status == "QUEUED";
    public bool IsWaitingForApi => Status == "WAITING_FOR_API";

    public string StudentLabel => !string.IsNullOrWhiteSpace(DisplayName) ? DisplayName!
        : !string.IsNullOrWhiteSpace(StudentName) ? StudentName!
        : !string.IsNullOrWhiteSpace(StudentId) ? StudentId! : "Not available";

    /// <summary>False only after the backend recorded a terminal state. A job
    /// that is still being worked on must never be presented as finished.</summary>
    public bool IsTerminal => Status is "COMPLETED" or "FAILED" or "CANCELLED";

    public bool IsActive => !IsTerminal;

    /// <summary>True until the backend measured real work (a queued or just
    /// started job). The bar must show an indeterminate state then, not 0%.</summary>
    public bool IsProgressIndeterminate => IsActive && Progress <= 0.0001;

    /// <summary>The percentage, a word while nothing measurable has finished,
    /// or a dash for a job that ended without completing.</summary>
    public string ProgressText => IsActive && Progress <= 0.0001
        ? "working"
        : Status != "COMPLETED" && !IsActive
            ? "—"
            : $"{Progress * 100:0}%";

    public bool CanCancel => Status is "QUEUED" or "PREPARING" or "DOWNLOADING" or "UPLOADING"
        or "MARKING" or "VERIFYING" or "SAVING" or "WAITING_FOR_API" or "RETRYING"
        or "STARTING";
    public bool CanRetry => Status is "FAILED" or "CANCELLED";
}

/// <summary>One stage the backend recorded for a job, with the time it was
/// seen. Rendered as the job's history; never synthesised in the client.</summary>
public sealed class StageEventDto
{
    public string? Status { get; set; }
    public string? Stage { get; set; }
    public double? Progress { get; set; }
    public double? At { get; set; }

    public string StageText => string.IsNullOrWhiteSpace(Stage)
        ? Core.UI.PayloadFacts.StatusLabel(Status)
        : Stage!;

    public string AtText => At is null ? "—" : EpochTime.Clock(At.Value);
}

public sealed class JobListDto
{
    public List<JobDto> Jobs { get; set; } = new();
}

public sealed class ResultSummaryDto
{
    public string Id { get; set; } = string.Empty;
    public string? JobId { get; set; }
    public string? CourseworkId { get; set; }
    public string? SubmissionId { get; set; }
    public string? StudentId { get; set; }
    public string? StudentName { get; set; }
    public string? DisplayName { get; set; }
    public string? ClassLabel { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Percentage { get; set; }
    public string? ProvisionalGrade { get; set; }
    public string? MarkingModel { get; set; }
    public double? CreatedAt { get; set; }
    public string? Confidence { get; set; }
    public int ReviewRequired { get; set; }
    public int ManualOverride { get; set; }
    public int VersionCount { get; set; }
    public string? AssignmentTitle { get; set; }
    public string? CourseId { get; set; }
    public string? CourseName { get; set; }
    public string? PublishStatus { get; set; }
    public string? Status { get; set; }
    public JsonElement? Partial { get; set; }

    public string Name => !string.IsNullOrWhiteSpace(DisplayName) ? DisplayName!
        : !string.IsNullOrWhiteSpace(StudentName) ? StudentName! : StudentId ?? "—";

    public string ScoreText => $"{Earned?.ToString("0.#") ?? "—"} / {Available?.ToString("0.#") ?? "—"}";

    public string PercentageText => Percentage is null ? "—" : $"{Percentage.Value:0.#}%";

    public string CreatedText => EpochTime.Format(CreatedAt);

    public string StatusLabel => Status switch
    {
        "needs_review" => "Needs review",
        "overridden" => "Overridden",
        "corrected" => "Corrected",
        _ => Confidence ?? "—",
    };
}

public sealed class ResultListDto
{
    public List<ResultSummaryDto> Results { get; set; } = new();
    public int Total { get; set; }
    public int Limit { get; set; }
    public int Offset { get; set; }
}

public sealed class ResultDetailDto
{
    private VerificationDto? _verification;
    private bool _verificationRead;

    public string Id { get; set; } = string.Empty;
    public string? JobId { get; set; }
    public string? CourseworkId { get; set; }
    public string? SubmissionId { get; set; }
    public string? StudentId { get; set; }
    public string? StudentName { get; set; }
    public string? DisplayName { get; set; }
    public string? ClassLabel { get; set; }
    public string? Nickname { get; set; }
    public string? NameSource { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Percentage { get; set; }
    public string? ProvisionalGrade { get; set; }
    public string? MarkingModel { get; set; }
    public string? AuditModel { get; set; }
    public string? AuditWarning { get; set; }
    public string? KeyFingerprint { get; set; }
    public JsonElement? ResultJson { get; set; }
    public JsonElement? VerificationJson { get; set; }
    public string? Confidence { get; set; }
    public JsonElement? ConfidenceReasons { get; set; }
    public int ReviewRequired { get; set; }
    public int ManualOverride { get; set; }
    public int DoubleCheckTriggered { get; set; }
    public string? DoubleCheckStatus { get; set; }
    public double? ProcessingSeconds { get; set; }
    public double? CreatedAt { get; set; }

    public string Name => !string.IsNullOrWhiteSpace(DisplayName) ? DisplayName!
        : !string.IsNullOrWhiteSpace(StudentName) ? StudentName! : StudentId ?? "—";

    public string ScoreText => $"{Earned?.ToString("0.#") ?? "—"} / {Available?.ToString("0.#") ?? "—"}";
    public string PercentageText => Percentage is null ? "—" : $"{Percentage.Value:0.#}%";
    public string CreatedText => EpochTime.Format(CreatedAt);

    /// <summary>
    /// Typed view of <c>verification_json</c>, or null when the row has none.
    /// Parsed once: bindings read this repeatedly.
    /// </summary>
    public VerificationDto? Verification()
    {
        if (_verificationRead)
        {
            return _verification;
        }

        _verificationRead = true;
        if (VerificationJson is not { ValueKind: JsonValueKind.Object } value)
        {
            return null;
        }

        try
        {
            _verification = JsonSerializer.Deserialize<VerificationDto>(value.GetRawText(), Json.Options);
        }
        catch (JsonException)
        {
            // A malformed verification block must not hide the marks.
            _verification = null;
        }

        return _verification;
    }

    /// <summary>Reads <c>result_json.questions</c> when the engine stored a
    /// per-question breakdown, and returns an empty list otherwise.</summary>
    public IReadOnlyList<QuestionMarkDto> Questions()
    {
        if (ResultJson is not { ValueKind: JsonValueKind.Object } root)
        {
            return Array.Empty<QuestionMarkDto>();
        }

        foreach (var property in new[] { "questions", "question_marks", "marks" })
        {
            if (!root.TryGetProperty(property, out var list) || list.ValueKind != JsonValueKind.Array)
            {
                continue;
            }

            var items = JsonSerializer.Deserialize<List<QuestionMarkDto>>(list.GetRawText(), Json.Options);
            if (items is { Count: > 0 })
            {
                return items;
            }
        }

        return Array.Empty<QuestionMarkDto>();
    }
}

public sealed class VersionEntryDto
{
    public int Version { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Percentage { get; set; }
    public string? ProvisionalGrade { get; set; }
    public string? MarkingModel { get; set; }
    public string? Confidence { get; set; }
    public bool Verified { get; set; }
    public double? CreatedAt { get; set; }
    public bool Active { get; set; }
    public string CreatedText => EpochTime.Format(CreatedAt);
    public string ScoreText => $"{Earned?.ToString("0.#") ?? "—"} / {Available?.ToString("0.#") ?? "—"}";
}

public sealed class VersionListDto
{
    public string? ResultId { get; set; }
    public List<VersionEntryDto> Versions { get; set; } = new();
}

public sealed class RemarkStatusDto
{
    public string? ResultId { get; set; }
    public List<VersionEntryDto> History { get; set; } = new();
    public JsonElement? Active { get; set; }
    public int? PendingVersion { get; set; }
    public bool CanUseNew { get; set; }
    public bool AwaitingDecision { get; set; }
    public int AttemptCount { get; set; }
}

public sealed class RemarkStartDto
{
    public bool Remarked { get; set; }
    public string? ResultId { get; set; }
    public string? JobId { get; set; }
    public bool Duplicate { get; set; }
}

public sealed class ReviewListDto
{
    public List<ReviewItemDto> Items { get; set; } = new();
    public int Total { get; set; }
}

/// <summary>
/// One coded reason a correction was escalated: what happened, which pipeline
/// stage produced it, the question it concerns, and what the teacher can do.
/// The backend supplies the codes; this class only turns them into words, so a
/// review never says just "low confidence".
/// </summary>
public sealed class ReviewReasonDto
{
    public string Code { get; set; } = string.Empty;
    public string? Stage { get; set; }
    public string? Severity { get; set; }
    public string? Question { get; set; }
    public string? Detail { get; set; }
    public string? Action { get; set; }

    /// <summary>True when the code was recovered from reasons this result
    /// already stored, rather than recorded by the run itself.</summary>
    public bool Derived { get; set; }

    public string ProvenanceText => Derived
        ? "Recovered from this result's stored reasons"
        : string.Empty;

    /// <summary>The stage line, saying when the code was recovered rather
    /// than recorded, so a reader knows how much to trust the label.</summary>
    public string StageLine => string.IsNullOrWhiteSpace(ProvenanceText)
        ? StageText
        : $"{StageText}  ·  {ProvenanceText}";

    /// <summary>True for a reason that genuinely needs a person.</summary>
    public bool NeedsHuman => string.Equals(Severity, "review", StringComparison.OrdinalIgnoreCase);

    /// <summary>A short heading: the code in plain words, and the question.</summary>
    public string Title => string.IsNullOrWhiteSpace(Question)
        ? HumanCode
        : $"{HumanCode} · Q{Question}";

    public string HumanCode => Code switch
    {
        "QUESTION_UNMARKED" => "Question not marked",
        "SUBQUESTION_UNMARKED" => "Subquestion not marked",
        "SUBQUESTION_CONFLICT" => "Two rows claim the same subquestion",
        "QUESTION_FLAGGED" => "The marking flagged this question",
        "SCHEME_DISAGREEMENT" => "The mark scheme disagrees with the maths",
        "MARK_SCHEME_CONTRADICTED" => "The mark scheme contradicts itself",
        "MARK_SCHEME_UNVERIFIED" => "The mark scheme was never confirmed",
        "MARK_ABOVE_MAXIMUM" => "More marks than the question allows",
        "NEGATIVE_MARK" => "A negative mark",
        "STORED_TOTAL_MISMATCH" => "The stored total did not add up",
        "OVER_AVAILABLE" => "More than the paper's maximum",
        "EXTRA_QUESTION" => "A question the scheme does not declare",
        "MAX_MARKS_CORRECTED" => "A maximum was corrected from the scheme",
        "UNATTRIBUTED_ROWS" => "Rows with no subquestion",
        "UNMATCHED_ROW" => "A row matched no scheme question",
        "AWARD_CLAMPED" => "An award was clamped to its maximum",
        "MISSING_BREAKDOWN" => "No marking-point breakdown",
        "ZERO_MAX_MARKS" => "A question with no marks",
        "MARKER_UNCERTAINTY" => "The marker recorded a doubt",
        "CODE_COUNT_OVER_MAX" => "More marking points than marks",
        "NO_QUESTIONS" => "Nothing was marked",
        "MODEL_FALLBACK" => "Marked by the fallback model",
        "VALIDATION_REVIEW" => "The checks asked for a review",
        "SUBMISSION_QUALITY" => "The submission looks wrong",
        "CORRECTION_REVIEW" => "The correction asked for a review",
        _ => Core.UI.PayloadFacts.KeyLabel(Code),
    };

    public string StageText => Stage switch
    {
        "extraction" => "extraction",
        "mapping" => "question mapping",
        "marking" => "marking",
        "validation" => "validation",
        "mark_scheme" => "mark scheme",
        _ => Stage ?? string.Empty,
    };

    public string SeverityText => NeedsHuman ? "Needs you" : "Note";

    public string ActionText => string.IsNullOrWhiteSpace(Action) ? string.Empty : Action!;

    /// <summary>One line for the queue's Reason column.</summary>
    public string ShortText => string.IsNullOrWhiteSpace(Question)
        ? HumanCode
        : $"Q{Question}: {HumanCode}";
}

public sealed class ReviewItemDto
{
    public string Id { get; set; } = string.Empty;
    public string? StudentName { get; set; }
    public string? StudentId { get; set; }
    public string? CourseworkId { get; set; }
    public string? SubmissionId { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Percentage { get; set; }
    public string? Confidence { get; set; }
    public JsonElement? ConfidenceReasons { get; set; }
    public int ReviewRequired { get; set; }
    public int ManualOverride { get; set; }
    public double? CreatedAt { get; set; }
    public string? AssignmentTitle { get; set; }
    public int AutomationHold { get; set; }
    public JsonElement? Partial { get; set; }

    /// <summary>The coded reasons the engine recorded for this escalation.</summary>
    public List<ReviewReasonDto> ReviewReasons { get; set; } = new();

    public bool HasReviewReasons => ReviewReasons.Count > 0;

    /// <summary>"2 reasons" / "1 reason" — never a bare count.</summary>
    public string ReasonsHeading => ReviewReasons.Count == 1
        ? "1 reason"
        : $"{ReviewReasons.Count} reasons";

    public string ScoreText => $"{Earned?.ToString("0.#") ?? "—"} / {Available?.ToString("0.#") ?? "—"}";
    public string CreatedText => EpochTime.Format(CreatedAt);

    /// <summary>The coded reason when the engine recorded one, otherwise the
    /// confidence prose the older rows carry.</summary>
    public string ReasonsText => ReviewReasons.Count > 0
        ? string.Join(" · ", ReviewReasons.Select(r => r.ShortText))
        : Json.Flatten(ConfidenceReasons);

    /// <summary>"HIGH" is an engine token; a person reads "High confidence".</summary>
    public string ConfidenceText => string.IsNullOrWhiteSpace(Confidence)
        ? "Not reported"
        : Core.UI.PayloadFacts.StatusLabel(Confidence) + " confidence";

    /// <summary>The stored flag is 0/1; the panel says Yes or No.</summary>
    public string ReviewRequiredText => ReviewRequired != 0 ? "Yes" : "No";
}

public sealed class ClassDto
{
    public string Id { get; set; } = string.Empty;
    public string? Name { get; set; }
    public string? Section { get; set; }
    public bool Local { get; set; }
    public int Students { get; set; }
    public int Assignments { get; set; }
    public int Submissions { get; set; }
    public int Submitted { get; set; }
    public int Missing { get; set; }
    public int Checked { get; set; }
    public int Review { get; set; }
    public double? Average { get; set; }
    public int ExternalAssignments { get; set; }
    public double? LastSync { get; set; }

    public string Title => string.IsNullOrWhiteSpace(Section) ? Name ?? Id : $"{Name} — {Section}";
    public string AverageText => Average is null ? "—" : $"{Average.Value:0.#}%";
    public string LastSyncText => EpochTime.Format(LastSync, "never");
}

public sealed class ClassListDto
{
    public List<ClassDto> Classes { get; set; } = new();
    public int Total { get; set; }
}

public sealed class AssignmentDto
{
    public string Id { get; set; } = string.Empty;
    public string? CourseId { get; set; }
    public string? Title { get; set; }
    public double? MaxPoints { get; set; }
    public string? State { get; set; }
    public string? DueDate { get; set; }
    public int Allowlisted { get; set; }
    public string? Description { get; set; }
    public string? AlternateLink { get; set; }
    public string? CourseName { get; set; }
    public string? CourseSection { get; set; }
    public string? CustomTitle { get; set; }
    public string? ClassroomTitle { get; set; }
    public string? SyncState { get; set; }
    public bool DeletedInClassroom { get; set; }
    public bool Local { get; set; }
    public bool Managed { get; set; }
    public int StudentsTotal { get; set; }
    public int Submitted { get; set; }
    public int Draft { get; set; }
    public int Missing { get; set; }
    public int Checked { get; set; }
    public int NeedsReview { get; set; }
    public int Failed { get; set; }
    public int Processing { get; set; }
    public double? Average { get; set; }
    public bool HasMarkScheme { get; set; }

    public string DisplayTitle => !string.IsNullOrWhiteSpace(CustomTitle) ? CustomTitle!
        : !string.IsNullOrWhiteSpace(Title) ? Title! : Id;

    public string CourseLabel => string.IsNullOrWhiteSpace(CourseSection)
        ? CourseName ?? "—" : $"{CourseName} — {CourseSection}";

    public string Kind => Managed ? "Managed" : Local ? "Local" : "External";

    public string AverageText => Average is null ? "—" : $"{Average.Value:0.#}%";
    public string MarkSchemeText => HasMarkScheme ? "yes" : "no";
}

public sealed class AssignmentListDto
{
    public List<AssignmentDto> Assignments { get; set; } = new();
    public int Total { get; set; }
}

public sealed class StudentDto
{
    public string StudentId { get; set; } = string.Empty;
    public string? OfficialName { get; set; }
    public string? Email { get; set; }
    public string? Nickname { get; set; }
    public string? ClassName { get; set; }
    public string? DisplayName { get; set; }
    public string? ClassLabel { get; set; }
    public int Assignments { get; set; }
    public int Submitted { get; set; }
    public bool TrackingAvailable { get; set; }
    public int Corrections { get; set; }
    public double TotalEarned { get; set; }
    public double TotalAvailable { get; set; }
    public int Pending { get; set; }

    // ---- headline facts the backend derives from the real rows -------------
    public string? Role { get; set; }
    public List<string> ClassroomCourses { get; set; } = new();
    public string? LatestStatus { get; set; }
    public string? LatestStatusLabel { get; set; }
    public string? LatestStatusAt { get; set; }
    public string? LatestAssignment { get; set; }
    public string? LatestCorrection { get; set; }
    public string? LatestResult { get; set; }
    public string? LatestClassroomGrade { get; set; }
    public int GradedCount { get; set; }
    public double? LastSyncedAt { get; set; }

    public string Name => !string.IsNullOrWhiteSpace(DisplayName) ? DisplayName!
        : !string.IsNullOrWhiteSpace(OfficialName) ? OfficialName! : StudentId;

    public string TotalText => TotalAvailable > 0
        ? $"{TotalEarned:0.#} / {TotalAvailable:0.#} ({TotalEarned / TotalAvailable * 100:0.#}%)"
        : "—";

    /// <summary>The teacher's own class, or an honest "not assigned".</summary>
    public string ClassText => string.IsNullOrWhiteSpace(ClassLabel) ? "Not assigned" : ClassLabel!;

    /// <summary>Google Classroom courses this student belongs to (real names).</summary>
    public string CoursesText => ClassroomCourses.Count == 0
        ? "No Classroom course"
        : string.Join(" · ", ClassroomCourses);

    /// <summary>Classroom does not return an address for every account type.</summary>
    public string EmailText => string.IsNullOrWhiteSpace(Email) ? "No email provided" : Email!;

    public string SubmittedText => TrackingAvailable
        ? $"{Submitted} of {Assignments}"
        : "Not tracked";

    public string LatestSubmissionText
    {
        get
        {
            var label = LatestStatusLabel ?? Core.UI.PayloadFacts.StatusLabel(LatestStatus);
            if (string.IsNullOrWhiteSpace(label) || label == "—")
            {
                return "No submission yet";
            }

            return string.IsNullOrWhiteSpace(LatestAssignment)
                ? label!
                : $"{label} · {LatestAssignment}";
        }
    }

    /// <summary>Math Checker's newest correction, in the backend's own words.</summary>
    public string CorrectionText => string.IsNullOrWhiteSpace(LatestCorrection)
        ? "Not corrected"
        : LatestResult is null ? LatestCorrection! : $"{LatestCorrection} · {LatestResult}";

    public string ClassroomGradeText => string.IsNullOrWhiteSpace(LatestClassroomGrade)
        ? (GradedCount > 0 ? "Graded, score not recorded" : "Not graded in Classroom")
        : LatestClassroomGrade!;

    public string PendingText => Pending > 0 ? $"{Pending} in progress" : "None";

    /// <summary>Short enough for a row cell: a full timestamp truncated to
    /// "2026-10-…" tells nobody anything.</summary>
    public string SyncText => EpochTime.ToLocal(LastSyncedAt)?.ToString("dd MMM HH:mm") ?? "never";

    /// <summary>One line for the row's detail pane, without inventing data.</summary>
    public string SummaryLine
    {
        get
        {
            var parts = new List<string> { ClassText };
            if (ClassroomCourses.Count > 0)
            {
                parts.Add(string.Join(" · ", ClassroomCourses));
            }

            return string.Join("  ·  ", parts);
        }
    }
}

public sealed class StudentListDto
{
    public List<StudentDto> Students { get; set; } = new();
    public double? LastSync { get; set; }
    public StudentSourceDto? Source { get; set; }
}

/// <summary>Where each part of a People row comes from, stated by the backend.</summary>
public sealed class StudentSourceDto
{
    public string? Identity { get; set; }
    public string? SubmissionStatus { get; set; }
    public string? Scores { get; set; }
    public string? Email { get; set; }

    public string EmailText => string.IsNullOrWhiteSpace(Email)
        ? "source not reported"
        : Email!;
}

public sealed class StudentClassesDto
{
    public List<string> Classes { get; set; } = new();
}

public sealed class ClassroomStatusDto
{
    public bool Connected { get; set; }
    public string? Email { get; set; }
    public double? ConnectedAt { get; set; }
    public string? Problem { get; set; }
    public bool ClientConfigured { get; set; }
    public double? LastSync { get; set; }
    public bool WriteDraftGrades { get; set; }

    public string StateText => Connected ? $"Connected as {Email ?? "unknown account"}"
        : ClientConfigured ? "Not connected" : "OAuth client not configured";
}

public sealed class ClassroomCourseDto
{
    public string Id { get; set; } = string.Empty;
    public string? Name { get; set; }
    public string? Section { get; set; }
    public string? Room { get; set; }
    public string? State { get; set; }
    public string? OwnerId { get; set; }
    public string? AlternateLink { get; set; }

    public string Title => string.IsNullOrWhiteSpace(Section) ? Name ?? Id : $"{Name} — {Section}";
}

public sealed class ClassroomCourseListDto
{
    public List<ClassroomCourseDto> Courses { get; set; } = new();
    public int Total { get; set; }
}

public sealed class ClassroomAssignmentDto
{
    public string Id { get; set; } = string.Empty;
    public string? Title { get; set; }
    public double? MaxPoints { get; set; }
    public string? State { get; set; }
    public string? DueDate { get; set; }
    public JsonElement? Raw { get; set; }

    public string TitleText => Title ?? Id;
    public string PointsText => MaxPoints?.ToString("0.#") ?? "—";
}

public sealed class ClassroomAssignmentListDto
{
    public List<ClassroomAssignmentDto> Assignments { get; set; } = new();
    public int Total { get; set; }
}

public sealed class ClassroomSubmissionDto
{
    public string Id { get; set; } = string.Empty;
    public string? UserId { get; set; }
    public string? StudentName { get; set; }
    public string? State { get; set; }
    public string? Late { get; set; }
    public string? Grade { get; set; }
    public string? CourseworkId { get; set; }

    public string TitleText => StudentName ?? UserId ?? Id;
    public string GradeText => string.IsNullOrWhiteSpace(Grade) ? "—" : Grade!;

    /// <summary>Google returns TURNED_IN / RETURNED; read them as words.</summary>
    public string StateText => Core.UI.PayloadFacts.StatusLabel(State);

    public string LateText => string.IsNullOrWhiteSpace(Late)
        ? "—"
        : string.Equals(Late, "true", StringComparison.OrdinalIgnoreCase) ? "Handed in late" : "On time";
}

public sealed class ClassroomSubmissionListDto
{
    public List<ClassroomSubmissionDto> Submissions { get; set; } = new();
    public int Total { get; set; }
}

public sealed class AutomationStatusDto
{
    public bool Enabled { get; set; }
    public int IntervalMinutes { get; set; }
    public double? LastCheck { get; set; }
    public double? NextCheck { get; set; }
    public bool Running { get; set; }
    public bool WorkerEnabled { get; set; }
    public string? WorkerState { get; set; }
    public bool ThreadRunning { get; set; }
    public int PendingJobs { get; set; }
    public int ActiveJobs { get; set; }
    public int AssignmentsEnabled { get; set; }
    public bool ReviewMode { get; set; }
    public bool DraftOnly { get; set; }
    public bool AttachReport { get; set; }
    public bool SubmitGrade { get; set; }
    public bool ReturnWork { get; set; }
    public string? ReportDetail { get; set; }
    public Dictionary<string, int> Counts { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public List<int> Intervals { get; set; } = new();

    public string LastCheckText => EpochTime.Format(LastCheck, "never");
    public string NextCheckText => EpochTime.Format(NextCheck, "—");
}

public sealed class AutomationJobDto
{
    public string Id { get; set; } = string.Empty;
    public string? StudentName { get; set; }
    public string? StudentId { get; set; }
    public string? CourseworkId { get; set; }
    public string? SubmissionId { get; set; }
    public string? Status { get; set; }
    public string? StatusLabel { get; set; }
    public int StageIndex { get; set; }
    public string? Pipeline { get; set; }
    public string? PipelineLabel { get; set; }
    public string? Error { get; set; }
    public string? ErrorCode { get; set; }
    public bool Resumable { get; set; }
    public string? RetryFrom { get; set; }
    public bool HasResult { get; set; }
    public string? ResultId { get; set; }
    public bool ReportUploaded { get; set; }
    public bool AwaitingApproval { get; set; }
    public bool Approvable { get; set; }
    public string? Grade { get; set; }
    public double? CreatedAt { get; set; }
    public double? UpdatedAt { get; set; }

    public string Name => StudentName ?? StudentId ?? "—";
    public string CreatedText => EpochTime.Format(CreatedAt);
    public bool CanRetry => Resumable;
}

public sealed class AutomationJobsDto
{
    public List<AutomationJobDto> Jobs { get; set; } = new();
}

public sealed class KeyStatusDto
{
    public string? Fingerprint { get; set; }
    public string? Label { get; set; }
    public string? Status { get; set; }
    public string? ErrorKind { get; set; }
    public string? ErrorMessage { get; set; }
    public bool Enabled { get; set; } = true;
    public double? CooldownRemaining { get; set; }
    public double? LastUsed { get; set; }
    public int Requests { get; set; }
    public int Successes { get; set; }

    public string StatusText => Core.UI.PayloadFacts.StatusLabel(Status);
    public string CooldownText => CooldownRemaining is > 0
        ? TimeSpan.FromSeconds(CooldownRemaining.Value).ToString(@"mm\:ss")
        : "—";
}

public sealed class KeysDto
{
    public int Total { get; set; }
    public int Healthy { get; set; }
    public int Cooldown { get; set; }
    public int Invalid { get; set; }
    public int Disabled { get; set; }
    public bool HasKeys { get; set; }
    public List<KeyStatusDto> Keys { get; set; } = new();
}

public sealed class KeyTestDto
{
    public string? Fingerprint { get; set; }
    public string? Status { get; set; }
    public bool AuthOk { get; set; }
    public bool GenerateOk { get; set; }
    public double? ResponseTimeMs { get; set; }
    public string? ErrorKind { get; set; }
    public string? Message { get; set; }
}

public sealed class WorkerStateDto
{
    /// <summary>The count the resize request asked for. Only the resize reply
    /// carries it; the workers listing has no such field.</summary>
    public int Requested { get; set; }

    /// <summary>Workers the pool actually holds, as the resize reply reports it.
    /// The listing calls the same figure <see cref="Size"/>.</summary>
    public int Running { get; set; }

    public int Recommended { get; set; }

    /// <summary>Worker processes in the pool, from GET /workers.</summary>
    public int Size { get; set; }

    /// <summary>Papers being processed right now, from GET /workers.</summary>
    public int Active { get; set; }

    /// <summary>Concurrency the pool is running with, whichever endpoint
    /// answered: the listing's size, or the resize reply's running count.
    /// Reading Running alone left every page showing "0 of 0" after a
    /// refresh, because the listing never sends that field.</summary>
    public int PoolSize => Size > 0 ? Size : Running;

    /// <summary>The provider circuit state the backend reports (a model that is
    /// cooling down after high demand pauses the queue; the dashboard says so
    /// instead of leaving a queued card looking stuck for no reason).</summary>
    public JsonElement? Provider { get; set; }

    public string Summary =>
        $"{Active} active of {PoolSize} workers (recommended {Recommended})";

    public bool ProviderPaused => Provider is { ValueKind: JsonValueKind.Object } provider
        && provider.TryGetProperty("available", out var available)
        && available.ValueKind == JsonValueKind.False;

    public string ProviderText
    {
        get
        {
            if (!ProviderPaused)
            {
                return string.Empty;
            }

            var seconds = Provider!.Value.TryGetProperty("cooldown_remaining", out var value)
                          && value.TryGetDouble(out var parsed)
                ? (int)Math.Ceiling(parsed)
                : 0;
            return seconds > 0
                ? $"Gemini cooling down ~{seconds}s — queued jobs are paused"
                : "Gemini is cooling down — queued jobs are paused";
        }
    }
}

public sealed class UsageDto
{
    public double? Now { get; set; }
    public UsageBucketDto? Hour { get; set; }
    public UsageBucketDto? Day { get; set; }
    public UsageLimitsDto? Limits { get; set; }
    public bool HourlyOver { get; set; }
    public bool DailyOver { get; set; }
    public double? HourRemainingMb { get; set; }
    public double? DayRemainingMb { get; set; }
    public ApiUsageDto? Api { get; set; }
    public List<UsageHistoryDto> History { get; set; } = new();

    public string HourText => Hour is null ? "—" : $"{Hour.Megabytes:0.##} MB";
    public string DayText => Day is null ? "—" : $"{Day.Megabytes:0.##} MB";
}

public sealed class UsageBucketDto
{
    public long Up { get; set; }
    public long Down { get; set; }
    public long Total { get; set; }
    public double Megabytes => Total / 1024d / 1024d;
}

public sealed class UsageLimitsDto
{
    public double? HourlyMb { get; set; }
    public double? DailyMb { get; set; }
    public long HourlyBytes { get; set; }
    public long DailyBytes { get; set; }
}

public sealed class UsageHistoryDto
{
    public double? Bucket { get; set; }
    public long Up { get; set; }
    public long Down { get; set; }
    public double Megabytes => (Up + Down) / 1024d / 1024d;
    public string BucketText => EpochTime.ToLocal(Bucket)?.ToString("dd MMM, HH:mm") ?? "—";

    /// <summary>Byte counts written the way a person reads them.</summary>
    public string UpText => SizeText.Of(Up);
    public string DownText => SizeText.Of(Down);
    public string TotalText => SizeText.Of(Up + Down);
}

public sealed class ApiUsageDto
{
    public int Requests { get; set; }
    public int Successes { get; set; }
    public int Failures { get; set; }
    public int Retries { get; set; }
    public int Failovers { get; set; }
    public int Cooldowns { get; set; }
    public JsonElement? ByClass { get; set; }

    /// <summary>Measured AI requests per correction (per-job ledger).</summary>
    public AiUsageSummaryDto? Ai { get; set; }
}

public sealed class AiUsageSummaryDto
{
    public int Papers { get; set; }
    public int Requests { get; set; }
    public double? Average { get; set; }
    public int? Median { get; set; }
    public int? Max { get; set; }
    public int BudgetExceeded { get; set; }
    public int Skipped { get; set; }
    public bool Unavailable { get; set; }
    public Dictionary<string, int> Stages { get; set; } = new();

    /// <summary>"2.0 per correction (15 corrections, max 6)" — or an explicit
    /// "not measured yet" so the desk never shows a fabricated 0.</summary>
    public string Text => Unavailable
        ? "No correction has recorded its AI requests yet."
        : $"{Average:0.##} per correction · {Requests} requests across {Papers} "
          + $"corrections · max {Max}"
          + (BudgetExceeded > 0
              ? $" · {BudgetExceeded} stopped by the request budget"
              : string.Empty);
}

public sealed class SettingsDto
{
    public string? Role { get; set; }
    public bool MockMode { get; set; }
    public bool Debug { get; set; }
    public WorkerSettingsDto? Worker { get; set; }
    public UsageSettingsDto? Usage { get; set; }
    public TestingSettingsDto? Testing { get; set; }
    public GeminiSettingsDto? Gemini { get; set; }
    public ClassroomSettingsDto? Classroom { get; set; }
    public int GeminiKeyCount { get; set; }
    public bool HasOauthClient { get; set; }
    public TeacherSettingsDto? Teacher { get; set; }
    public ReportBrandingDto? Reports { get; set; }
    public GradesSettingsDto? Grades { get; set; }
    public RetentionSettingsDto? Retention { get; set; }
    public NetworkSettingsDto? Network { get; set; }
    public LifecycleSettingsDto? Lifecycle { get; set; }
}

public sealed class WorkerSettingsDto
{
    public int Concurrency { get; set; }
    public int MaxRetries { get; set; }
}

public sealed class UsageSettingsDto
{
    public double? HourlyMb { get; set; }
    public double? DailyMb { get; set; }
}

public sealed class TestingSettingsDto
{
    public bool Only { get; set; }
    public string? Reason { get; set; }
}

public sealed class GeminiSettingsDto
{
    public List<string> PrimaryModels { get; set; } = new();
    public int RequestTimeoutMs { get; set; }
}

public sealed class ClassroomSettingsDto
{
    public bool Enabled { get; set; }
    public bool WriteDraftGrades { get; set; }
}

public sealed class TeacherSettingsDto
{
    public string? TeacherName { get; set; }
    public string? SchoolName { get; set; }
    public bool Onboarded { get; set; }
}

public sealed class ReportBrandingDto
{
    public string? TeacherTitle { get; set; }
    public string? CenterName { get; set; }
    public string? AcademicYear { get; set; }
    public string? Quote { get; set; }
    public string? LogoPath { get; set; }
    public string? Monogram { get; set; }
    public string? FooterNote { get; set; }
    public string? Accent { get; set; }
    public string? DocumentLabel { get; set; }
}

public sealed class GradesSettingsDto
{
    public List<List<JsonElement>>? Boundaries { get; set; }
    public string? SessionLabel { get; set; }
}

public sealed class RetentionSettingsDto
{
    public string? KeepSubmissions { get; set; }
    public int KeepDays { get; set; }
    public bool CleanupTempOnStart { get; set; }
}

public sealed class NetworkSettingsDto
{
    public string? AccessMode { get; set; }
    public int Port { get; set; }
}

public sealed class LifecycleSettingsDto
{
    public bool Enabled { get; set; }
    public bool Supervisor { get; set; }
    public int HeartbeatSeconds { get; set; }
    public int SessionTimeoutSeconds { get; set; }
}

public sealed class SetupStatusDto
{
    public string? AppName { get; set; }
    public string? Version { get; set; }
    public bool FirstRun { get; set; }
    public SetupTeacherDto? Teacher { get; set; }
    public SetupChecksDto? Checks { get; set; }
}

public sealed class SetupTeacherDto
{
    public string? TeacherName { get; set; }
    public string? SchoolName { get; set; }
    public bool Onboarded { get; set; }
}

public sealed class SetupChecksDto
{
    public bool TeacherProfile { get; set; }
    public bool AiKeys { get; set; }
    public int AiKeyCount { get; set; }
    public bool ClassroomOauthClient { get; set; }
    public bool ClassroomConnected { get; set; }
    public bool StorageWritable { get; set; }
}

public sealed class AuthStatusDto
{
    public bool PublicMode { get; set; }

    /// <summary>The backend's authoritative "you must sign in": public mode, or
    /// a local sign-in requirement backed by an account that exists. It is
    /// false during first-run setup (nothing to sign in with), so a fresh
    /// install goes straight to setup instead of asking for a password nobody
    /// has been given. null when an older backend did not send it.</summary>
    public bool? Required { get; set; }

    /// <summary>The local sign-in setting alone (true by default, even when no
    /// account exists). Never gate the UI on this: use <see cref="Required"/>.</summary>
    public bool LoginRequired { get; set; }

    public bool Authenticated { get; set; }

    /// <summary>The backend's "configured": any account (or a legacy local
    /// password) to authenticate against.</summary>
    [JsonPropertyName("configured")]
    public bool HasAccount { get; set; }

    [JsonPropertyName("username")]
    public string? User { get; set; }
    public string? Role { get; set; }
    public string? CsrfToken { get; set; }
    public string? SetupAllowed { get; set; }
    public JsonElement? Raw { get; set; }
}

public sealed class PrivacyInfoDto
{
    public string? DataDirectory { get; set; }
    public bool ApplicationFilesSeparate { get; set; }
    public List<PrivacyItemDto> Items { get; set; } = new();
    public string? Note { get; set; }
}

public sealed class PrivacyItemDto
{
    public string? Key { get; set; }
    public string? Label { get; set; }
    public bool Present { get; set; }
}

public sealed class DataInventoryDto
{
    public Dictionary<string, int> Counts { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    public long DatabaseBytes { get; set; }
    public DataExportInfoDto? Export { get; set; }

    public string DatabaseText => DatabaseBytes < 1024 * 1024
        ? $"{DatabaseBytes / 1024d:0.#} KB"
        : $"{DatabaseBytes / 1024d / 1024d:0.##} MB";
}

public sealed class DataExportInfoDto
{
    public string? Format { get; set; }
    public int FormatVersion { get; set; }
    public string? Content { get; set; }
    public string? Filename { get; set; }
    public List<string> Sections { get; set; } = new();
}

public sealed class AccessStateDto
{
    public bool? MultiAccount { get; set; }
    public bool? PublicAddressEnabled { get; set; }
    public string? PublicAddress { get; set; }
    public JsonElement? Raw { get; set; }
}

public sealed class AudioPingDto
{
}

public static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        // The backend speaks snake_case (corrections_today, student_name, …),
        // so the policy must be snake_case, not camelCase: case-insensitivity
        // does not bridge an underscore.
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
    };

    /// <summary>Compact human text for a JSON value used in list columns.</summary>
    public static string Flatten(JsonElement? value)
    {
        if (value is null)
        {
            return string.Empty;
        }

        var element = value.Value;
        return element.ValueKind switch
        {
            JsonValueKind.Undefined or JsonValueKind.Null => string.Empty,
            JsonValueKind.String => element.GetString() ?? string.Empty,
            JsonValueKind.Array => string.Join("; ", element.EnumerateArray().Select(FlattenValue)),
            JsonValueKind.Object => string.Join("; ", element.EnumerateObject().Select(p => $"{p.Name}: {FlattenValue(p.Value)}")),
            _ => element.ToString(),
        };
    }

    private static string FlattenValue(JsonElement element) => element.ValueKind switch
    {
        JsonValueKind.String => element.GetString() ?? string.Empty,
        JsonValueKind.Object => Flatten(element),
        JsonValueKind.Array => Flatten(element),
        _ => element.ToString(),
    };

    public static string Pretty(JsonElement element) =>
        JsonSerializer.Serialize(element, new JsonSerializerOptions { WriteIndented = true });
}
