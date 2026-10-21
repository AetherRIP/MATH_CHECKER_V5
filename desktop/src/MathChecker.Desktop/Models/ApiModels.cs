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
    public JsonElement? Bandwidth { get; set; }

    public string CreatedText => EpochTime.Format(CreatedAt);
    public string StageText => string.IsNullOrWhiteSpace(StageDetail) ? (Status ?? "") : StageDetail!;
    public bool CanCancel => Status is "QUEUED" or "PREPARING" or "DOWNLOADING" or "UPLOADING"
        or "MARKING" or "VERIFYING" or "SAVING" or "WAITING_FOR_API" or "RETRYING";
    public bool CanRetry => Status is "FAILED" or "CANCELLED";
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

    public string ScoreText => $"{Earned?.ToString("0.#") ?? "—"} / {Available?.ToString("0.#") ?? "—"}";
    public string CreatedText => EpochTime.Format(CreatedAt);
    public string ReasonsText => Json.Flatten(ConfidenceReasons);
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

    public string Name => !string.IsNullOrWhiteSpace(DisplayName) ? DisplayName!
        : !string.IsNullOrWhiteSpace(OfficialName) ? OfficialName! : StudentId;

    public string TotalText => TotalAvailable > 0
        ? $"{TotalEarned:0.#} / {TotalAvailable:0.#} ({TotalEarned / TotalAvailable * 100:0.#}%)"
        : "—";
}

public sealed class StudentListDto
{
    public List<StudentDto> Students { get; set; } = new();
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
}

public sealed class ClassroomSubmissionListDto
{
    public List<ClassroomSubmissionDto> Submissions { get; set; } = new();
    public int Total { get; set; }
}

public sealed class LargePdfConfigDto
{
    public bool Enabled { get; set; }
    public int ThresholdPages { get; set; }
    public int TargetMin { get; set; }
    public int TargetMax { get; set; }
    public int Concurrency { get; set; }
}

public sealed class LargePdfCountsDto
{
    public int Queued { get; set; }
    public int Inspecting { get; set; }
    public int Planning { get; set; }
    public int Ready { get; set; }
    public int Processing { get; set; }
    public int AwaitingReview { get; set; }
    public int Completed { get; set; }
    public int Failed { get; set; }
}

public class LargePdfJobDto
{
    public string Id { get; set; } = string.Empty;
    public string? StudentName { get; set; }
    public string? StudentId { get; set; }
    public string? SubmissionId { get; set; }
    public string? CourseworkId { get; set; }
    public string? Status { get; set; }
    public string? StatusLabel { get; set; }
    public string? StepDetail { get; set; }
    public int PageCount { get; set; }
    public int BatchCount { get; set; }
    public string? ResultId { get; set; }
    public int Attempts { get; set; }
    public string? Error { get; set; }
    public string? ErrorCode { get; set; }
    public bool Retryable { get; set; }
    public bool AwaitingReview { get; set; }
    public double? CreatedAt { get; set; }
    public double? UpdatedAt { get; set; }
    public double? FinishedAt { get; set; }

    public string Name => StudentName ?? StudentId ?? "—";
    public string ProgressText => BatchCount > 0 ? $"{PageCount} pages / {BatchCount} batches" : "—";
    public string CreatedText => EpochTime.Format(CreatedAt);
    public bool CanCancel => Status is not "COMPLETED" and not "FAILED" and not "CANCELLED";
}

public sealed class LargePdfJobsDto
{
    public LargePdfConfigDto? Config { get; set; }
    public LargePdfCountsDto? Counts { get; set; }
    public List<LargePdfJobDto> Jobs { get; set; } = new();
    public JsonElement? Progress { get; set; }
}

public sealed class BatchDto
{
    public int BatchIndex { get; set; }
    public int BatchNumber { get; set; }
    public int PageStart { get; set; }
    public int PageEnd { get; set; }
    public int Pages { get; set; }
    public string? Status { get; set; }
    public string? StatusLabel { get; set; }
    public int? ValidatedQuestions { get; set; }
    public List<string> MissingQuestions { get; set; } = new();
    public bool Ambiguous { get; set; }
    public int Attempts { get; set; }
    public string? Error { get; set; }
    public string? ErrorCode { get; set; }

    public string PagesText => $"p{PageStart}–{PageEnd}";
    public string MissingText => MissingQuestions.Count == 0 ? "—" : string.Join(", ", MissingQuestions);
}

public sealed class LargePdfJobDetailDto : LargePdfJobDto
{
    public List<BatchDto> Batches { get; set; } = new();
    public JsonElement? Summary { get; set; }
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

    public string StatusText => string.IsNullOrWhiteSpace(Status) ? "—" : Status!;
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
    public int Requested { get; set; }
    public int Running { get; set; }
    public int Recommended { get; set; }

    public string Summary => $"Running {Running} of {Requested} requested (recommended {Recommended})";
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
    public string BucketText => EpochTime.ToLocal(Bucket)?.ToString("MM-dd HH:mm") ?? "—";
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
    public JsonElement? LargePdf { get; set; }
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
    public bool LoginRequired { get; set; }
    public bool Authenticated { get; set; }
    public bool HasAccount { get; set; }
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
