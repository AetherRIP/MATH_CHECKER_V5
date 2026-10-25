namespace MathChecker.Desktop.Models;

// The whole-assignment preview (/assignments/{id}/correct-all/preview). Every
// number is computed by the backend from verified Classroom state, and the
// blockers are the backend's own gate — the page shows them instead of
// re-deriving whether a correction may start.

public sealed class CorrectAllCountsDto
{
    public int Students { get; set; }
    public int Submitted { get; set; }
    public int AlreadyCorrected { get; set; }
    public int NeedsReview { get; set; }
    public int NotCorrected { get; set; }
    public int Processing { get; set; }
    public int Invalid { get; set; }
    public int Missing { get; set; }
    public int Draft { get; set; }
    public int Unknown { get; set; }
}

public sealed class CorrectAllStudentDto
{
    public string? SubmissionId { get; set; }
    public string? StudentId { get; set; }
    public string? StudentName { get; set; }

    /// <summary>Present on already-corrected / needs-review entries: what that
    /// paper scored last time. Absent on the papers still to be corrected.</summary>
    public string? ResultId { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Percentage { get; set; }
    public bool NeedsReview { get; set; }
}

public sealed class CorrectAllPreviewDto
{
    public string? CourseworkId { get; set; }
    public string? CourseId { get; set; }
    public string? AssignmentTitle { get; set; }
    public CorrectAllCountsDto Counts { get; set; } = new();
    public List<CorrectAllStudentDto> Eligible { get; set; } = new();
    public List<CorrectAllStudentDto> AlreadyCorrected { get; set; } = new();
    public List<CorrectAllStudentDto> NeedsReview { get; set; } = new();
    public int WillCorrect { get; set; }
    public bool CanStart { get; set; }
    public List<string> Blockers { get; set; } = new();
    public bool HasMarkScheme { get; set; }
    public bool Allowlisted { get; set; }
    public string? SyncWarning { get; set; }
}

/// <summary>What the backend actually queued, so the page reports the real
/// outcome instead of a generic "done".</summary>
/// <summary>A paper the teacher ticked that the backend refused to queue, with
/// its own reason. A partial selection is reported, never silently shrunk.</summary>
public sealed class CorrectAllNotEligibleDto
{
    public string? SubmissionId { get; set; }
    public string? Reason { get; set; }
}

public sealed class CorrectAllResultDto
{
    public int Enqueued { get; set; }
    public List<string> Jobs { get; set; } = new();

    /// <summary>How many papers the request asked for; null for a
    /// whole-assignment run that named none.</summary>
    public int? Requested { get; set; }
    public List<CorrectAllNotEligibleDto> NotEligible { get; set; } = new();
    public int Duplicates { get; set; }
    public List<string> Processing { get; set; } = new();
    public List<string> SkippedMissing { get; set; } = new();
    public List<string> SkippedDraft { get; set; } = new();
    public List<string> Invalid { get; set; } = new();
    public List<string> Unknown { get; set; } = new();
    public List<CorrectAllFailureDto> EnqueueFailed { get; set; } = new();
    public int TotalStudents { get; set; }
    public string? SyncWarning { get; set; }
}

public sealed class CorrectAllFailureDto
{
    public string? Student { get; set; }
    public string? Error { get; set; }
}
