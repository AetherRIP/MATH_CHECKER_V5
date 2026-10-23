using System.Globalization;

namespace MathChecker.Desktop.Models;

/// <summary>
/// Typed view of <c>GET /api/students/{id}</c>.
///
/// Two sources stay separate and labelled, exactly as the backend reports
/// them: Google Classroom owns identity and submission states, the local
/// correction database owns marks. A missing figure stays <c>null</c>; it is
/// never turned into a zero or a percentage by this layer.
/// </summary>
public sealed class StudentProfileDto
{
    public string StudentId { get; set; } = string.Empty;
    public string? OfficialName { get; set; }
    public string? Email { get; set; }
    public string? Nickname { get; set; }
    public string? ClassName { get; set; }
    public string? DisplayName { get; set; }
    public string? ClassLabel { get; set; }
    public StudentCourseDto? Course { get; set; }
    public StudentSourcesDto Sources { get; set; } = new();
    public StudentClassroomDto Classroom { get; set; } = new();
    public StudentPerformanceDto Performance { get; set; } = new();
    public StudentBehaviorDto Behavior { get; set; } = new();
    public StudentCorrectionsDto Corrections { get; set; } = new();
    public double? FetchedAt { get; set; }

    public string Name => !string.IsNullOrWhiteSpace(DisplayName) ? DisplayName!
        : !string.IsNullOrWhiteSpace(OfficialName) ? OfficialName!
        : StudentId;

    /// <summary>One or two letters for the compact avatar; never an image the
    /// backend does not have.</summary>
    public string Initials
    {
        get
        {
            var parts = Name.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length == 0)
            {
                return "?";
            }

            var first = parts[0][0];
            var second = parts.Length > 1 ? parts[1][0] : '\0';
            return (second == '\0' ? first.ToString() : $"{first}{second}").ToUpperInvariant();
        }
    }

    public string ClassText => string.IsNullOrWhiteSpace(ClassLabel) ? "Not assigned" : ClassLabel!;

    /// <summary>True when a teacher-set class exists; otherwise the label may
    /// just be the Classroom course.
    /// </summary>
    public bool HasAssignedClass => !string.IsNullOrWhiteSpace(ClassName);

    public bool HasClassroomTracking => Classroom.Available;

    public string IdentitySource => Source(Sources.Identity);
    public string SubmissionSource => Source(Sources.SubmissionStatus);
    public string ScoresSource => Source(Sources.Scores);
    public string CorrectionSource => Source(Sources.CorrectionDetail);

    public string NicknameText => string.IsNullOrWhiteSpace(Nickname) ? "Not set" : Nickname!;

    private static string Source(string? value) =>
        string.IsNullOrWhiteSpace(value) ? "Source unavailable" : "SOURCE: " + value;

    /// <summary>The Classroom course, when the profile reports one.</summary>
    public string CourseText
    {
        get
        {
            if (Course is null)
            {
                return string.Empty;
            }

            var name = Course.Name ?? string.Empty;
            var section = Course.Section ?? string.Empty;
            if (name.Length > 0 && section.Length > 0)
            {
                return $"{name} · {section}";
            }

            return name.Length > 0 ? name : section;
        }
    }
}

public sealed class StudentCourseDto
{
    public string? CourseId { get; set; }
    public string? Name { get; set; }
    public string? Section { get; set; }
}

public sealed class StudentSourcesDto
{
    public string? Identity { get; set; }
    public string? SubmissionStatus { get; set; }
    public string? Scores { get; set; }
    public string? CorrectionDetail { get; set; }
    public string? Nickname { get; set; }
    public string? ClassName { get; set; }
}

public sealed class StudentClassroomDto
{
    public bool Available { get; set; }
    public int AssignmentsTotal { get; set; }
    public int Submitted { get; set; }
    public int NotSubmitted { get; set; }
    public int Unknown { get; set; }
    public double? SubmittedPercentage { get; set; }
    public List<StudentAssignmentDto> Assignments { get; set; } = new();
}

public sealed class StudentAssignmentDto
{
    public string? SubmissionId { get; set; }
    public string? CourseworkId { get; set; }
    public string? Title { get; set; }
    public string? Status { get; set; }
    public string? StatusLabel { get; set; }
    public bool? Late { get; set; }

    /// <summary>Resolved by the backend with the same rule the statistics use.</summary>
    public bool Submitted { get; set; }

    public bool Outstanding { get; set; }
    public bool Returned { get; set; }
    public int Attachments { get; set; }
    public double? AssignedGrade { get; set; }
    public double? MaxPoints { get; set; }
    public string? DueDate { get; set; }
    public string? CreationTime { get; set; }
    public string? UpdateTime { get; set; }
    public bool? Stale { get; set; }
    public string? DataQuality { get; set; }
    public double? LastSyncedAt { get; set; }
    public StudentAssignmentResultDto? Result { get; set; }
    public StudentAssignmentJobDto? Job { get; set; }

    public string TitleText => string.IsNullOrWhiteSpace(Title) ? CourseworkId ?? "Assignment" : Title!;

    public string StatusText => string.IsNullOrWhiteSpace(StatusLabel) ? Status ?? "Unknown" : StatusLabel!;

    public string UpdatedText => ProfileText.Iso(UpdateTime ?? CreationTime);

    public string DueText => string.IsNullOrWhiteSpace(DueDate) ? "—" : DueDate!;

    public string LateText => Late == true ? "Late" : "On time";

    public string ClassroomGradeText => AssignedGrade is null
        ? "Not graded"
        : $"{ProfileText.Mark(AssignedGrade)} / {ProfileText.Mark(MaxPoints)}";

    public string ScoreText => Result is null
        ? "—"
        : $"{ProfileText.Mark(Result.Earned)} / {ProfileText.Mark(Result.Available)}";

    public string PercentageText => Result?.Percentage is null ? "—" : ProfileText.Percent(Result.Percentage);

    public string CorrectionState
    {
        get
        {
            if (Result is not null)
            {
                return "Corrected";
            }

            var status = Job?.Status ?? string.Empty;
            if (string.Equals(status, "FAILED", StringComparison.OrdinalIgnoreCase))
            {
                return "Failed";
            }

            return ProfileText.IsActiveJob(status) ? "Processing" : "Not corrected";
        }
    }

    public bool HasCorrection => Result is not null;

    public string ReportHint => Result is null
        ? "No stored correction for this assignment yet."
        : $"{Result.Earned:0.#} / {Result.Available:0.#} marks from the local correction.";
}

public sealed class StudentAssignmentResultDto
{
    public string? Id { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Percentage { get; set; }
    public bool? Partial { get; set; }
}

public sealed class StudentAssignmentJobDto
{
    public string? Id { get; set; }
    public string? Status { get; set; }
    public string? Error { get; set; }
    public string? ErrorCode { get; set; }
}

public sealed class StudentPerformanceDto
{
    public double? ClassroomAverage { get; set; }
    public double? ClassroomHighest { get; set; }
    public double? ClassroomLowest { get; set; }
    public int ClassroomGraded { get; set; }
    public int ClassroomNotGraded { get; set; }
    public int ClassroomZeros { get; set; }
    public double? McAverage { get; set; }
    public double? McHighest { get; set; }
    public double? McLowest { get; set; }
    public int McZeros { get; set; }
    public double TotalEarned { get; set; }
    public double TotalAvailable { get; set; }
    public double? OverallPercentage { get; set; }
    public int Missing { get; set; }
    public int Late { get; set; }
    public int Corrected { get; set; }
}

public sealed class StudentBehaviorDto
{
    public int OnTime { get; set; }
    public int Late { get; set; }
    public int Missing { get; set; }
    public int Draft { get; set; }
    public int TurnedIn { get; set; }
    public int Returned { get; set; }
}

public sealed class StudentCorrectionsDto
{
    public int Total { get; set; }
    public int Pending { get; set; }
    public int Failed { get; set; }
    public double TotalEarned { get; set; }
    public double TotalAvailable { get; set; }
    public double? OverallPercentage { get; set; }
    public int NeedsReview { get; set; }
    /// <summary>The 10 newest corrections (kept for report-compatible payloads).</summary>
    public List<StudentCorrectionDto> Recent { get; set; } = new();

    /// <summary>Every stored correction, newest first. Falls back to Recent
    /// when talking to a backend that predates this field.</summary>
    public List<StudentCorrectionDto> History { get; set; } = new();

    public List<StudentCorrectionJobDto> Jobs { get; set; } = new();
    public double? LastCorrectedAt { get; set; }
}

public sealed class StudentCorrectionDto
{
    public string Id { get; set; } = string.Empty;
    public string? CourseworkId { get; set; }
    public string? SubmissionId { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Percentage { get; set; }
    public string? ProvisionalGrade { get; set; }
    public string? MarkingModel { get; set; }
    public double? CreatedAt { get; set; }
    public bool NeedsReview { get; set; }
    public string? Status { get; set; }
    public List<string> ReviewQuestions { get; set; } = new();

    /// <summary>Filled from the assignments list, which owns the title.</summary>
    public string? AssignmentTitle { get; set; }

    public string TitleText => string.IsNullOrWhiteSpace(AssignmentTitle)
        ? CourseworkId ?? "Correction"
        : AssignmentTitle!;

    public string ScoreText => $"{ProfileText.Mark(Earned)} / {ProfileText.Mark(Available)}";

    public string PercentageText => ProfileText.Percent(Percentage);

    public string CreatedText => EpochTime.Format(CreatedAt);

    /// <summary>Short date for the progress chart's narrow labels.</summary>
    public string CreatedDayText =>
        EpochTime.ToLocal(CreatedAt)?.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture) ?? "—";

    public string StatusText => NeedsReview || string.Equals(Status, "REVIEW", StringComparison.OrdinalIgnoreCase)
        ? "Needs review"
        : string.IsNullOrWhiteSpace(Status) ? "Marked" : Status!;

    public bool IsReview => NeedsReview || string.Equals(Status, "REVIEW", StringComparison.OrdinalIgnoreCase);
}

public sealed class StudentCorrectionJobDto
{
    public string? Id { get; set; }
    public string? Status { get; set; }
    public string? CourseworkId { get; set; }
    public double? CreatedAt { get; set; }
    public double? FinishedAt { get; set; }
    public string? Error { get; set; }
}

/// <summary>Formatting shared by the profile DTOs, kept in one place so a
/// missing value reads the same on every surface.</summary>
public static class ProfileText
{
    public static string Mark(double? value) =>
        value is null ? "—" : value.Value.ToString("0.##", CultureInfo.InvariantCulture);

    public static string Percent(double? value) =>
        value is null ? "—" : value.Value.ToString("0.#", CultureInfo.InvariantCulture) + "%";

    /// <summary>Google Classroom timestamps are ISO-8601; render them local and
    /// short. An unparseable value is shown verbatim rather than hidden.</summary>
    public static string Iso(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return "—";
        }

        if (DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var parsed))
        {
            return parsed.ToLocalTime().ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture);
        }

        return value!;
    }

    public static bool IsActiveJob(string? status) => status?.ToUpperInvariant() switch
    {
        "QUEUED" or "PREPARING" or "DOWNLOADING" or "UPLOADING" or "MARKING" or
        "VERIFYING" or "SAVING" or "WAITING_FOR_API" or "RETRYING" or "STARTING" or
        "PROCESSING" => true,
        _ => false,
    };
}
