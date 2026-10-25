using System.Text.Json.Serialization;

namespace MathChecker.Desktop.Models;

// The Assignment Details payload (/assignments/{id}/students). Every field here
// is produced by the backend's own read model, so the page shows what the
// database holds instead of deriving a second opinion of its own. Naming is
// snake_case, mapped by Json.Options.

/// <summary>One student on the assignment, with completion and correction kept
/// apart: a student can hand work in and still be uncorrected.</summary>
public sealed class AssignmentStudentDto
{
    public string? StudentId { get; set; }
    public string? Name { get; set; }
    public string? ClassroomName { get; set; }
    public string? Email { get; set; }
    public string? Status { get; set; }
    public string? StatusLabel { get; set; }
    public string? RawState { get; set; }

    /// <summary>COMPLETED | NOT_COMPLETED | MISSING | UNKNOWN.</summary>
    public string? Completion { get; set; }

    public string? CompletionLabel { get; set; }
    public string? CompletionReason { get; set; }
    public bool Submitted { get; set; }
    public string? SubmittedAt { get; set; }
    public bool Late { get; set; }
    public List<AssignmentAttachmentDto> Attachments { get; set; } = new();
    public AssignmentCorrectionDto Correction { get; set; } = new();
    public double? MaxPoints { get; set; }
    public double? AssignedGrade { get; set; }
    public double? DraftGrade { get; set; }
    public string? Quality { get; set; }
    public bool Stale { get; set; }
    public string? SyncError { get; set; }
    public double? LastSyncedAt { get; set; }
    public bool Synthetic { get; set; }
    public string? SubmissionId { get; set; }
}

/// <summary>Where V5's own correction of this submission stands.</summary>
public sealed class AssignmentCorrectionDto
{
    public string? State { get; set; }
    public string? Label { get; set; }
    public string? JobStatus { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Percentage { get; set; }
    public bool Partial { get; set; }
    public string? Error { get; set; }
}

public sealed class AssignmentAttachmentDto
{
    public string? Title { get; set; }
    public string? Link { get; set; }
    public string? MimeType { get; set; }
}

/// <summary>Counts for the summary cards. Computed from the same rows as the
/// table, so the cards and the table can never disagree.</summary>
public sealed class AssignmentSummaryDto
{
    public int Expected { get; set; }
    public int Completed { get; set; }
    public int NotCompleted { get; set; }
    public int Missing { get; set; }
    public int Unknown { get; set; }
    public double? CompletionPercentage { get; set; }
    public int Submitted { get; set; }
    public int Corrected { get; set; }
    public int Failed { get; set; }
    public int Invalid { get; set; }
    public int AwaitingCorrection { get; set; }
    public int Stale { get; set; }
    public int SyncErrors { get; set; }
    public int DuplicateRecords { get; set; }
}

/// <summary>How fresh the roster is, and whether the assignment still exists in
/// Classroom. A page that cannot verify its records says so.</summary>
public sealed class AssignmentSyncDto
{
    public double? LastSync { get; set; }
    public int? StaleAfterSeconds { get; set; }
    public string? AssignmentState { get; set; }
    public bool RosterKnown { get; set; }
}

public sealed class AssignmentStudentsDto
{
    public AssignmentDto Assignment { get; set; } = new();
    public List<AssignmentStudentDto> Students { get; set; } = new();
    public AssignmentSummaryDto Summary { get; set; } = new();
    public AssignmentSyncDto Sync { get; set; } = new();
    public List<string> Warnings { get; set; } = new();

    [JsonIgnore]
    public string AssignmentId => Assignment.Id;
}
