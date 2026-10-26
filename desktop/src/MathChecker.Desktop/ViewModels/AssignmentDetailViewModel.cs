using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// The Assignment Details page: one assignment, its real metadata, and the
/// expected roster classified as Completed / Not completed / Missing / Unknown
/// straight from the backend's read model.
///
/// It never derives a status of its own: the summary cards and the table are
/// both rendered from the payload the backend computed from the same rows, so a
/// number on a card can never disagree with the list underneath it. Completion
/// (did the student hand work in?) and correction (has V5 marked it?) are shown
/// as separate facts, because a failed correction must not look like a missing
/// submission.
/// </summary>
public sealed class AssignmentDetailViewModel : PageViewModel
{
    private const string AnyStatus = "All statuses";
    private const string AnyCorrection = "All correction states";

    private readonly List<AssignmentStudentRow> _all = new();
    private string? _courseworkId;
    private string _heading = "Assignment details";
    private string _description = string.Empty;
    private string _filter = string.Empty;
    private string _statusFilter = AnyStatus;
    private string _correctionFilter = AnyCorrection;
    private string _sortBy = "Name";
    private AssignmentSummaryDto _summary = new();
    private string _syncText = string.Empty;
    private string _factsTitle = string.Empty;
    private bool _hasWarnings;

    // Mark scheme, allow-listing and whole-assignment correction. These lived on
    // the separate Classroom page; they belong with the assignment they act on.
    private string? _courseId;
    private string _assignmentTitle = "this assignment";
    private string _schemeTitle = "Mark scheme";
    private string _schemeDetail = string.Empty;
    private bool _schemeAttached;
    private bool _schemeChecking;
    private bool _allowlisted;
    private int _schemeRequest;

    public AssignmentDetailViewModel(AppSession session)
        : base(session, "assignment", "Assignment")
    {
        BackCommand = new RelayCommand(() => Session.GoTo("assignments"));
    }

    /// <summary>The roster is Classroom's, so this page offers the one shared
    /// Sync control.</summary>
    public override bool SyncsFromClassroom => true;

    public RelayCommand BackCommand { get; }

    public ObservableCollection<AssignmentStudentRow> Items { get; } = new();
    public ObservableCollection<FactRow> Facts { get; } = new();
    public ObservableCollection<string> Warnings { get; } = new();

    public ObservableCollection<string> StatusFilters { get; } = new()
    {
        AnyStatus, "Completed", "Not completed", "Missing", "Unknown",
    };

    public ObservableCollection<string> CorrectionFilters { get; } = new()
    {
        AnyCorrection, "Corrected", "Being corrected", "Correction failed",
        "Not corrected",
    };

    public ObservableCollection<string> Sorts { get; } = new()
    {
        "Name", "Status", "Submitted",
    };

    /// <summary>PEOPLE — the space, then the page, exactly like the student
    /// profile header ("PEOPLE · PROFILE").</summary>
    public string HeaderEyebrow => "PEOPLE · ASSIGNMENT";

    public string Heading
    {
        get => _heading;
        private set => SetProperty(ref _heading, value);
    }

    public string HeaderDescription
    {
        get => _description;
        private set => SetProperty(ref _description, value);
    }

    public string Filter
    {
        get => _filter;
        set
        {
            if (SetProperty(ref _filter, value))
            {
                ApplyFilter();
            }
        }
    }

    public string StatusFilter
    {
        get => _statusFilter;
        set
        {
            // A rebuilt filter list makes WPF write null back through the
            // two-way binding; that means "no filter", not "no rows".
            var normalized = string.IsNullOrWhiteSpace(value) ? AnyStatus : value;
            if (SetProperty(ref _statusFilter, normalized))
            {
                ApplyFilter();
            }
        }
    }

    public string CorrectionFilter
    {
        get => _correctionFilter;
        set
        {
            var normalized = string.IsNullOrWhiteSpace(value) ? AnyCorrection : value;
            if (SetProperty(ref _correctionFilter, normalized))
            {
                ApplyFilter();
            }
        }
    }

    public string SortBy
    {
        get => _sortBy;
        set
        {
            var normalized = string.IsNullOrWhiteSpace(value) ? "Name" : value;
            if (SetProperty(ref _sortBy, normalized))
            {
                ApplyFilter();
            }
        }
    }

    public string ExpectedText => _summary.Expected.ToString(CultureInfo.InvariantCulture);

    public string CompletedText => _summary.Completed.ToString(CultureInfo.InvariantCulture);

    public string NotCompletedText => _summary.NotCompleted.ToString(CultureInfo.InvariantCulture);

    public string MissingText => _summary.Missing.ToString(CultureInfo.InvariantCulture);

    public string UnknownText => _summary.Unknown.ToString(CultureInfo.InvariantCulture);

    public string CompletionText => _summary.CompletionPercentage is { } percentage
        ? $"{percentage:0.#}% of {ExpectedText} handed in"
        : "No roster to measure";

    public string SubmittedText => _summary.Submitted.ToString(CultureInfo.InvariantCulture);

    public string CorrectedText => _summary.Corrected.ToString(CultureInfo.InvariantCulture);

    public string CorrectionDetailText
    {
        get
        {
            var parts = new List<string>();
            if (_summary.AwaitingCorrection > 0)
            {
                parts.Add($"{_summary.AwaitingCorrection} awaiting correction");
            }

            if (_summary.Failed > 0)
            {
                parts.Add($"{_summary.Failed} failed");
            }

            if (_summary.Invalid > 0)
            {
                parts.Add($"{_summary.Invalid} unreadable");
            }

            return parts.Count == 0 ? "correction is up to date" : string.Join(" · ", parts);
        }
    }

    /// <summary>How fresh the roster is, stated on the page instead of being
    /// inferred from a colour.</summary>
    public string SyncText
    {
        get => _syncText;
        private set => SetProperty(ref _syncText, value);
    }

    public string FactsTitle
    {
        get => _factsTitle;
        private set => SetProperty(ref _factsTitle, value);
    }

    public bool HasWarnings
    {
        get => _hasWarnings;
        private set => SetProperty(ref _hasWarnings, value);
    }

    public bool HasFacts => Facts.Count > 0;

    // ------------------------------------------------ mark scheme and setting up

    /// <summary>The headline: "Attached · verified", "Not attached", …</summary>
    public string SchemeTitle
    {
        get => _schemeTitle;
        private set => SetProperty(ref _schemeTitle, value);
    }

    /// <summary>File, page count, marks and structure — what is actually on disk,
    /// read from the backend, never inferred from the upload button.</summary>
    public string SchemeDetail
    {
        get => _schemeDetail;
        private set
        {
            if (SetProperty(ref _schemeDetail, value))
            {
                OnPropertyChanged(nameof(HasSchemeDetail));
            }
        }
    }

    public bool HasSchemeDetail => !string.IsNullOrWhiteSpace(SchemeDetail);

    public bool SchemeAttached => _schemeAttached;

    public bool SchemeChecking => _schemeChecking;

    /// <summary>True only when a scheme is definitely absent: a read still in
    /// flight or one that failed is not evidence of "not attached".</summary>
    public bool SchemeMissing => !_schemeAttached && !_schemeChecking;

    public string UploadSchemeLabel => _schemeAttached
        ? "Replace mark scheme…"
        : "Upload mark scheme…";

    /// <summary>Whether automatic corrections may pick this assignment up. Read
    /// from the backend's own flag, never assumed from the presence of a scheme.</summary>
    public bool Allowlisted => _allowlisted;

    public string AllowlistText => _allowlisted
        ? "Allow-listed: automatic corrections may pick this assignment up."
        : "Not allow-listed: automatic corrections skip it.";

    public bool CanAllowlist => !_allowlisted && HasCourse;

    private bool HasCourse => !string.IsNullOrWhiteSpace(_courseId);

    /// <summary>The backend refuses a whole-assignment run without a scheme, so
    /// the button is never offered as a dead end.</summary>
    public bool CanCorrectAll => HasCourse && _schemeAttached;

    public AsyncCommand UploadMarkSchemeCommand => Command(async () =>
    {
        if (!HasCourse)
        {
            Error = "This assignment is not linked to a Google Classroom course, so there is no "
                    + "place to attach a mark scheme.";
            return;
        }

        var path = Session.Dialogs.PickOpenFile("Select the mark scheme PDF", "PDF (*.pdf)|*.pdf");
        if (path is null)
        {
            return;
        }

        await Session.Api.UploadMarkSchemeAsync(_courseId!, _courseworkId!, path);
        Status = $"The mark scheme for “{_assignmentTitle}” was uploaded.";
        await LoadSchemeAsync();
        await LoadAsync();
    });

    public AsyncCommand AllowlistCommand => Command(async () =>
    {
        if (!CanAllowlist)
        {
            return;
        }

        await Session.Api.SetAllowlistAsync(_courseId!, _courseworkId!, true);
        _allowlisted = true;
        RaiseSettingUp();
        Status = $"“{_assignmentTitle}” is allow-listed for automatic corrections.";
        await LoadAsync();
    });

    public AsyncCommand CorrectAllCommand => Command(async () =>
    {
        if (!CanCorrectAll)
        {
            Error = _schemeAttached
                ? "This assignment is not linked to a Google Classroom course."
                : "Upload the mark scheme first: nothing can be corrected without one.";
            return;
        }

        if (!Session.Dialogs.Confirm("Correct the whole assignment",
                $"Correct every eligible submission of \"{_assignmentTitle}\"?\n\n"
                + "Students who have not handed anything in are skipped. Their progress "
                + "appears in Correction monitor."))
        {
            return;
        }

        await Session.Api.CorrectAllAsync(_courseId!, _courseworkId!, refresh: true);
        Status = "Every eligible paper in this assignment was queued.";
        await LoadAsync();
    });

    private void RaiseSettingUp()
    {
        OnPropertyChanged(nameof(SchemeAttached));
        OnPropertyChanged(nameof(SchemeMissing));
        OnPropertyChanged(nameof(SchemeChecking));
        OnPropertyChanged(nameof(SchemeTitle));
        OnPropertyChanged(nameof(UploadSchemeLabel));
        OnPropertyChanged(nameof(Allowlisted));
        OnPropertyChanged(nameof(AllowlistText));
        OnPropertyChanged(nameof(CanAllowlist));
        OnPropertyChanged(nameof(CanCorrectAll));
    }

    /// <summary>Reads this assignment's scheme from the backend. A late answer for
    /// an assignment the teacher has already left is dropped.</summary>
    private async Task LoadSchemeAsync()
    {
        var courseworkId = _courseworkId;
        if (string.IsNullOrWhiteSpace(courseworkId))
        {
            return;
        }

        var request = ++_schemeRequest;
        _schemeChecking = true;
        RaiseSettingUp();
        try
        {
            var payload = await Session.Api.MarkSchemeAsync(courseworkId).ConfigureAwait(true);
            if (request != _schemeRequest)
            {
                return;
            }

            ApplyScheme(payload);
        }
        catch (ApiException ex)
        {
            if (request == _schemeRequest)
            {
                SchemeTitle = "The mark scheme could not be read";
                SchemeDetail = ex.Display;
            }
        }
        catch (Exception ex)
        {
            if (request == _schemeRequest)
            {
                SchemeTitle = "The mark scheme could not be read";
                SchemeDetail = UserMessage.For(ex);
            }
        }
        finally
        {
            if (request == _schemeRequest)
            {
                _schemeChecking = false;
                RaiseSettingUp();
            }
        }
    }

    private void ApplyScheme(JsonElement payload)
    {
        bool Flag(string name) => payload.TryGetProperty(name, out var value)
            && value.ValueKind == JsonValueKind.True;

        string? Text(string name) => payload.TryGetProperty(name, out var value)
            && value.ValueKind == JsonValueKind.String
                ? value.GetString()
                : null;

        int? Number(string name) => payload.TryGetProperty(name, out var value)
            && value.ValueKind == JsonValueKind.Number
            && value.TryGetDouble(out var number)
                ? (int)Math.Round(number)
                : null;

        if (!Flag("has_mark_scheme"))
        {
            _schemeAttached = false;
            SchemeTitle = "Not attached";
            SchemeDetail = "Corrections for this assignment cannot start until a scheme is uploaded.";
            return;
        }

        _schemeAttached = true;
        var parts = new List<string>();
        if (Text("file_name") is { Length: > 0 } fileName)
        {
            parts.Add(fileName);
        }

        if (Number("pages") is { } pages && pages > 0)
        {
            parts.Add(pages == 1 ? "1 page" : $"{pages} pages");
        }

        if (Number("total_marks") is { } marks && marks > 0)
        {
            parts.Add($"{marks} marks");
        }

        if (Number("subquestion_count") is { } points && points > 0)
        {
            parts.Add($"{points} marking points");
        }

        var processed = Flag("processed");
        var detail = string.Join(" · ", parts);
        if (!processed)
        {
            detail = (detail.Length > 0 ? detail + " — " : string.Empty)
                     + "it is analysed the first time work is corrected.";
        }

        SchemeTitle = processed ? "Attached · verified" : "Attached · not analysed yet";
        if (processed && Text("verification_state") is { Length: > 0 } state)
        {
            SchemeTitle = "Attached · " + PayloadFacts.StatusLabel(state).ToLowerInvariant();
        }

        SchemeDetail = detail;
    }

    public string ShownText
    {
        get
        {
            var completed = Items.Count(r => r.Completion == "COMPLETED");
            var text = Items.Count == 1 ? "1 student shown" : $"{Items.Count} students shown";
            return Items.Count == 0 ? text : $"{text} · {completed} completed";
        }
    }

    /// <summary>Opens one assignment. The identity travels beside the page key
    /// (the same mechanism the marking desk uses), so the page always loads the
    /// assignment that was clicked rather than the last one it happened to hold.</summary>
    public static void Open(AppSession session, AssignmentDto assignment)
    {
        session.OpenAssignment(assignment.Id);
    }

    protected override async Task LoadCoreAsync()
    {
        var pending = Session.TakePendingAssignmentId();
        if (!string.IsNullOrWhiteSpace(pending))
        {
            _courseworkId = pending;
        }

        if (string.IsNullOrWhiteSpace(_courseworkId))
        {
            Reset();
            Status = "Choose an assignment in Assignments to see its students.";
            return;
        }

        try
        {
            var data = await Session.Api.AssignmentStudentsAsync(_courseworkId).ConfigureAwait(true);
            Apply(data);
            await LoadSchemeAsync().ConfigureAwait(true);
        }
        catch (ApiException ex) when (ex.Status == 404)
        {
            // A deleted or unknown assignment must not leave the previous one's
            // students on screen pretending they belong to this identifier.
            Reset();
            Error = ex.Display;
        }
    }

    /// <summary>Applies one backend payload. Public so tests (and the page's own
    /// reload) exercise the same path the HTTP call uses.</summary>
    public void Apply(AssignmentStudentsDto data)
    {
        _summary = data.Summary ?? new AssignmentSummaryDto();

        var assignment = data.Assignment ?? new AssignmentDto();
        Heading = string.IsNullOrWhiteSpace(assignment.DisplayTitle)
            ? "Assignment details"
            : assignment.DisplayTitle;

        // The setup controls act on this assignment: its own course, its own
        // allow-list flag, its own scheme. Nothing is carried over from a
        // previously opened assignment.
        _courseId = assignment.CourseId;
        _assignmentTitle = Heading;
        _allowlisted = assignment.Allowlisted != 0;
        RaiseSettingUp();

        var kind = assignment.Kind;
        HeaderDescription = string.Join(" · ", new[]
        {
            assignment.CourseLabel,
            kind,
            string.IsNullOrWhiteSpace(assignment.DueDate)
                ? "No due date"
                : "Due " + assignment.DueDate,
        });

        Facts.Clear();
        Facts.Add(new FactRow("Assignment ID", assignment.Id));
        Facts.Add(new FactRow("Class", assignment.CourseLabel));
        Facts.Add(new FactRow("Kind", kind));
        Facts.Add(new FactRow("Due", assignment.DueDate ?? "Not available"));
        Facts.Add(new FactRow("Maximum points",
            assignment.MaxPoints is { } points ? $"{points:0.##}" : "Not available"));
        Facts.Add(new FactRow("Mark scheme", assignment.HasMarkScheme ? "Attached" : "Not attached"));
        if (!string.IsNullOrWhiteSpace(assignment.Description))
        {
            Facts.Add(new FactRow("Description", assignment.Description!));
        }

        if (assignment.DeletedInClassroom)
        {
            Facts.Add(new FactRow("Classroom state", "Deleted in Classroom"));
        }

        FactsTitle = "ASSIGNMENT";
        OnPropertyChanged(nameof(HasFacts));

        _all.Clear();
        foreach (var student in data.Students ?? new List<AssignmentStudentDto>())
        {
            _all.Add(new AssignmentStudentRow(student));
        }

        Warnings.Clear();
        foreach (var warning in data.Warnings ?? new List<string>())
        {
            if (!string.IsNullOrWhiteSpace(warning))
            {
                Warnings.Add(warning);
            }
        }

        HasWarnings = Warnings.Count > 0;

        var sync = data.Sync ?? new AssignmentSyncDto();
        SyncText = sync.LastSync is > 0
            ? "Roster synced " + EpochTime.Format(sync.LastSync)
            : "Roster never synced";

        ApplyFilter();
        RaiseSummary();
        Status = _summary.Expected == 0
            ? "No students are recorded for this assignment yet."
            : $"{_summary.Expected} expected · {_summary.Completed} completed · "
              + $"{_summary.NotCompleted} not completed";
    }

    private void RaiseSummary()
    {
        OnPropertyChanged(nameof(ExpectedText));
        OnPropertyChanged(nameof(CompletedText));
        OnPropertyChanged(nameof(NotCompletedText));
        OnPropertyChanged(nameof(MissingText));
        OnPropertyChanged(nameof(UnknownText));
        OnPropertyChanged(nameof(CompletionText));
        OnPropertyChanged(nameof(SubmittedText));
        OnPropertyChanged(nameof(CorrectedText));
        OnPropertyChanged(nameof(CorrectionDetailText));
        OnPropertyChanged(nameof(ShownText));
    }

    private void ApplyFilter()
    {
        var needle = Filter.Trim();
        var rows = _all.Where(row =>
        {
            if (StatusFilter != AnyStatus
                && !string.Equals(row.CompletionLabel, StatusFilter, StringComparison.Ordinal))
            {
                return false;
            }

            if (CorrectionFilter != AnyCorrection
                && !string.Equals(row.CorrectionLabel, CorrectionFilter, StringComparison.Ordinal))
            {
                return false;
            }

            if (needle.Length == 0)
            {
                return true;
            }

            return row.Name.Contains(needle, StringComparison.OrdinalIgnoreCase)
                   || row.StudentIdText.Contains(needle, StringComparison.OrdinalIgnoreCase)
                   || row.EmailText.Contains(needle, StringComparison.OrdinalIgnoreCase);
        });

        rows = SortBy switch
        {
            "Status" => rows.OrderBy(r => r.CompletionRank).ThenBy(r => r.Name, StringComparer.OrdinalIgnoreCase),
            // Newest hand-in first; rows with no submission sort last instead of
            // pretending to be the oldest submission.
            "Submitted" => rows.OrderBy(r => r.HasSubmissionAt ? 0 : 1)
                .ThenByDescending(r => r.SubmittedSortKey)
                .ThenBy(r => r.Name, StringComparer.OrdinalIgnoreCase),
            _ => rows.OrderBy(r => r.Name, StringComparer.OrdinalIgnoreCase),
        };

        Items.Clear();
        foreach (var row in rows)
        {
            Items.Add(row);
        }

        OnPropertyChanged(nameof(ShownText));
    }

    private void Reset()
    {
        _courseworkId = null;
        _courseId = null;
        _allowlisted = false;
        _schemeAttached = false;
        SchemeTitle = "Mark scheme";
        SchemeDetail = string.Empty;
        RaiseSettingUp();
        _all.Clear();
        Items.Clear();
        Facts.Clear();
        Warnings.Clear();
        _summary = new AssignmentSummaryDto();
        HasWarnings = false;
        Heading = "Assignment details";
        HeaderDescription = "No assignment is open.";
        SyncText = "Roster never synced";
        FactsTitle = "ASSIGNMENT";
        OnPropertyChanged(nameof(HasFacts));
        RaiseSummary();
    }
}

/// <summary>
/// One student row, formatted for the page. Everything is computed from the
/// backend's record: nothing here guesses a name, a mark or a submission state.
/// </summary>
public sealed class AssignmentStudentRow
{
    public AssignmentStudentRow(AssignmentStudentDto dto)
    {
        Dto = dto;
    }

    public AssignmentStudentDto Dto { get; }

    public string Name => string.IsNullOrWhiteSpace(Dto.Name) ? "Not available" : Dto.Name!;

    public string StudentIdText => string.IsNullOrWhiteSpace(Dto.StudentId)
        ? "Not available"
        : Dto.StudentId!;

    public string EmailText => string.IsNullOrWhiteSpace(Dto.Email) ? "No email on record" : Dto.Email!;

    /// <summary>COMPLETED | NOT_COMPLETED | MISSING | UNKNOWN.</summary>
    public string Completion => string.IsNullOrWhiteSpace(Dto.Completion) ? "UNKNOWN" : Dto.Completion!;

    public string CompletionLabel => string.IsNullOrWhiteSpace(Dto.CompletionLabel)
        ? "Unknown"
        : Dto.CompletionLabel!;

    public string CompletionText => string.IsNullOrWhiteSpace(Dto.CompletionReason)
        ? CompletionLabel
        : $"{CompletionLabel} — {Dto.CompletionReason}";

    /// <summary>The Classroom-level state (Submitted, Draft, Reclaimed, …).</summary>
    public string StatusLabel => string.IsNullOrWhiteSpace(Dto.StatusLabel)
        ? (string.IsNullOrWhiteSpace(Dto.Status) ? "Unknown" : Dto.Status!)
        : Dto.StatusLabel!;

    /// <summary>Only a real hand-in has a hand-in time. The backend also stores
    /// when the submission resource was created (which happens for a draft
    /// too), so that timestamp must never be shown as a submission date.</summary>
    public bool HasSubmissionAt => Dto.Submitted && !string.IsNullOrWhiteSpace(Dto.SubmittedAt);

    public bool Late => Dto.Late;

    public string SubmittedText
    {
        get
        {
            if (!Dto.Submitted)
            {
                return "No submission";
            }

            if (string.IsNullOrWhiteSpace(Dto.SubmittedAt))
            {
                return "Handed in · no time recorded";
            }

            return DateTimeOffset.TryParse(Dto.SubmittedAt, CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal, out var when)
                ? when.ToLocalTime().ToString("yyyy-MM-dd HH:mm") + (Dto.Late ? " · late" : string.Empty)
                : Dto.SubmittedAt! + (Dto.Late ? " · late" : string.Empty);
        }
    }

    public string SubmittedSortKey => Dto.Submitted ? Dto.SubmittedAt ?? string.Empty : string.Empty;

    /// <summary>Lower sorts first; the order the teacher acts on.</summary>
    public int CompletionRank => Completion switch
    {
        "MISSING" => 0,
        "NOT_COMPLETED" => 1,
        "UNKNOWN" => 2,
        _ => 3,
    };

    public string CorrectionState => Dto.Correction?.State ?? "NOT_CORRECTED";

    /// <summary>Completion labels and correction labels are distinct sets, so a
    /// page filter can never show a failed correction as a missing submission.</summary>
    public string CorrectionLabel => CorrectionState switch
    {
        "CORRECTED" => "Corrected",
        "PROCESSING" => "Being corrected",
        "FAILED" => "Correction failed",
        _ => "Not corrected",
    };

    public string CorrectionText
    {
        get
        {
            var correction = Dto.Correction;
            if (correction is null)
            {
                return "Not corrected";
            }

            return correction.State switch
            {
                "CORRECTED" when correction.Partial => "Corrected (partial coverage)",
                "CORRECTED" => $"Corrected · {MarksText} ({PercentageText})",
                "PROCESSING" => "Being corrected",
                "FAILED" => "Correction failed",
                _ => "Not corrected",
            };
        }
    }

    public string MarksText
    {
        get
        {
            var correction = Dto.Correction;
            if (correction?.Earned is null || correction.Available is null)
            {
                return "Not marked";
            }

            return $"{Trim(correction.Earned.Value)}/{Trim(correction.Available.Value)}";
        }
    }

    public string PercentageText => Dto.Correction?.Percentage is { } percentage
        ? $"{percentage:0.#}%"
        : "—";

    /// <summary>Classroom's own assigned grade, kept separate from V5's marks.</summary>
    public string GradeText => Dto.AssignedGrade is { } grade
        ? Dto.MaxPoints is { } max ? $"{Trim(grade)}/{Trim(max)}" : Trim(grade)
        : "Not graded";

    public string AttachmentText => Dto.Attachments.Count == 0
        ? "No attachment"
        : string.Join(" · ", Dto.Attachments.Select(a =>
            string.IsNullOrWhiteSpace(a.Title) ? "Attachment" : a.Title!));

    public string? AttachmentLink => Dto.Attachments
        .Select(a => a.Link)
        .FirstOrDefault(link => !string.IsNullOrWhiteSpace(link));

    public bool HasAttachmentLink => !string.IsNullOrWhiteSpace(AttachmentLink);

    /// <summary>The single line that explains why this row needs attention, or
    /// null when it does not.</summary>
    public string? Warning
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(Dto.SyncError))
            {
                return "Sync error: " + Dto.SyncError;
            }

            if (string.Equals(Dto.Quality, "ERROR", StringComparison.OrdinalIgnoreCase))
            {
                return "The backend recorded an error for this record.";
            }

            if (!string.IsNullOrWhiteSpace(Dto.Correction?.Error))
            {
                return Dto.Correction!.Error;
            }

            if (Dto.Stale)
            {
                return "Last synchronized more than 30 minutes ago.";
            }

            return null;
        }
    }

    public bool HasWarning => Warning is not null;

    private static string Trim(double value) =>
        value == Math.Floor(value) ? value.ToString("0", CultureInfo.InvariantCulture)
            : value.ToString("0.##", CultureInfo.InvariantCulture);
}
