using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One in-page section of Classroom. The tabs share a single view
/// model, so switching a tab never re-fetches or loses the chosen course.</summary>
public sealed class ClassroomTabVm
{
    public ClassroomTabVm(string key, string title, string detail)
    {
        Key = key;
        Title = title;
        Detail = detail;
    }

    public string Key { get; }
    public string Title { get; }
    public string Detail { get; }

    public override string ToString() => Title;
}

public sealed class ClassroomViewModel : PageViewModel
{
    private ClassroomStatusDto? _status;
    private ClassroomCourseDto? _selectedCourse;
    private ClassroomAssignmentDto? _selectedAssignment;
    private ClassroomSubmissionDto? _selectedSubmission;
    private string _detailNote = string.Empty;
    private string _detailRaw = string.Empty;

    // What is attached to the chosen assignment. The card only offered an
    // upload button, so a teacher could not tell an assignment that already had
    // a scheme from one that would fail with "no mark scheme".
    private string _schemeTitle = "Choose an assignment to see its mark scheme.";
    private string _schemeDetail = string.Empty;
    private bool _schemeAttached;
    private bool _schemeChecking;

    /// <summary>Set while a load restores a selection by id, so assigning the
    /// restored item does not start a second, duplicate fetch.</summary>
    private bool _restoringSelection;

    private ClassroomTabVm _selectedTab;

    public ClassroomViewModel(AppSession session)
        : base(session, "classroom", "Classroom")
    {
        Tabs = new ObservableCollection<ClassroomTabVm>
        {
            new("connection", "Connection", "Account, sync and the backend's last answer"),
            new("assignments", "Assignments", "Allow-list, mark scheme and bulk correction"),
            new("submissions", "Submissions", "Every paper in the chosen assignment"),
            new("grades", "Grades", "Write draft marks back to Google Classroom"),
        };
        _selectedTab = Tabs[0];
    }

    public ObservableCollection<ClassroomTabVm> Tabs { get; }

    public ClassroomTabVm SelectedTab
    {
        get => _selectedTab;
        set
        {
            if (value is null || !SetProperty(ref _selectedTab, value))
            {
                return;
            }

            OnPropertyChanged(nameof(ShowConnection));
            OnPropertyChanged(nameof(ShowAssignments));
            OnPropertyChanged(nameof(ShowSubmissions));
            OnPropertyChanged(nameof(ShowGrades));
        }
    }

    public bool ShowConnection => _selectedTab.Key == "connection";
    public bool ShowAssignments => _selectedTab.Key == "assignments";
    public bool ShowSubmissions => _selectedTab.Key == "submissions";
    public bool ShowGrades => _selectedTab.Key == "grades";

    public ObservableCollection<ClassroomCourseDto> Courses { get; } = new();
    public ObservableCollection<ClassroomAssignmentDto> Assignments { get; } = new();
    public ObservableCollection<ClassroomSubmissionDto> Submissions { get; } = new();

    public string StatusText => _status?.StateText ?? "—";
    public string ProblemText => _status?.Problem ?? string.Empty;
    public bool IsConnected => _status?.Connected ?? false;

    public bool IsDisconnected => !IsConnected;

    /// <summary>Every Classroom action below needs an assignment; correcting a
    /// single paper additionally needs a submission. Without these the buttons
    /// stay disabled rather than running into a "select something" error.</summary>
    public bool HasCourse => SelectedCourse is not null;

    public bool HasAssignmentTarget => SelectedCourse is not null && SelectedAssignment is not null;

    public bool HasSubmissionTarget => HasAssignmentTarget && SelectedSubmission is not null;
    public string LastSyncText => _status is null ? "—" : EpochTime.Format(_status.LastSync, "never");

    /// <summary>Short state for the connection panel: connected, or the honest
    /// reason the account is not (no client, or never signed in).</summary>
    public string StateTitle => _status is null ? "Checking…"
        : _status.Connected ? "Connected"
        : _status.ClientConfigured ? "Not connected" : "OAuth client missing";

    public string StateDetail => _status is null ? string.Empty
        : _status.Connected ? _status.Email ?? "Google account"
        : _status.ClientConfigured
            ? "Sign in with Google to read courses and hand-outs."
            : "Add a Google OAuth client in Config → Settings first.";

    public string ConnectedSinceText => _status?.Connected == true
        ? "Connected " + EpochTime.Format(_status.ConnectedAt, "just now")
        : "No account linked yet";

    public string SyncDetail => _status?.Connected == true
        ? "Last sync " + EpochTime.Format(_status.LastSync, "never")
        : "Sync runs once an account is connected";

    public string WriteBackText => _status?.WriteDraftGrades == true
        ? "Draft write-back is on: corrected marks can be pushed back to Classroom."
        : "Draft write-back is off. Turn it on in Config → Settings before pushing grades.";

    public bool WriteBackEnabled => _status?.WriteDraftGrades ?? false;

    public string WriteBackLabel => WriteBackEnabled ? "On" : "Off";

    /// <summary>"Open in Classroom" needs a course that actually carries a
    /// Google link; the command explains itself when the course has none.</summary>
    public bool CanOpenClassroom => !string.IsNullOrWhiteSpace(SelectedCourse?.AlternateLink);

    /// <summary>Pushing grades needs a target AND the teacher's explicit
    /// write-back setting; otherwise the button is a request the backend will
    /// refuse.</summary>
    public bool CanPushGrades => HasAssignmentTarget && WriteBackEnabled;

    public string CourseCountText => Courses.Count == 1 ? "1 course" : $"{Courses.Count} courses";

    /// <summary>An unknown count is reported as unknown: a course nobody has
    /// opened yet has no fetched assignment list to count.</summary>
    public string AssignmentCountText => SelectedCourse is null
        ? "—"
        : Assignments.Count == 1 ? "1 assignment" : $"{Assignments.Count} assignments";

    public string SelectedCourseText => SelectedCourse?.Title ?? "No course chosen";

    public string SelectedAssignmentText => SelectedAssignment?.TitleText ?? "No assignment chosen";

    public string AssignmentMetaText => SelectedAssignment is null
        ? "Choose an assignment to unlock allow-listing, mark schemes and correction."
        : $"{SelectedAssignment.PointsText} points"
          + (string.IsNullOrWhiteSpace(SelectedAssignment.DueDate) ? string.Empty
              : $" · due {SelectedAssignment.DueDate}")
          + (string.IsNullOrWhiteSpace(SelectedAssignment.State) ? string.Empty
              : $" · {PayloadFacts.StatusLabel(SelectedAssignment.State)}");

    /// <summary>True when this assignment already has a scheme on disk.</summary>
    public bool SchemeAttached => _schemeAttached;

    /// <summary>True only when a scheme is definitely absent: a read still in
    /// flight or one that failed is not evidence of "not attached".</summary>
    public bool SchemeMissing => HasAssignmentTarget && !_schemeAttached && !_schemeChecking;

    public bool SchemeChecking => _schemeChecking;

    /// <summary>The headline: "Attached · verified", "Not attached", …</summary>
    public string SchemeTitle => _schemeTitle;

    /// <summary>File, page count, marks and structure — what is actually on
    /// disk, read from the backend, never inferred from the upload button.</summary>
    public string SchemeDetail => _schemeDetail;

    public bool HasSchemeDetail => !string.IsNullOrWhiteSpace(_schemeDetail);

    public string UploadSchemeLabel => _schemeAttached
        ? "Replace mark scheme…"
        : "Upload mark scheme…";

    public string SubmissionsSummary
    {
        get
        {
            if (Submissions.Count == 0)
            {
                return "No submissions loaded yet";
            }

            var turnedIn = Submissions.Count(s => string.Equals(s.State, "TURNED_IN",
                StringComparison.OrdinalIgnoreCase));
            return $"{Submissions.Count} submissions · {turnedIn} turned in";
        }
    }

    public ClassroomCourseDto? SelectedCourse
    {
        get => _selectedCourse;
        set
        {
            if (!SetProperty(ref _selectedCourse, value))
            {
                return;
            }

            // Changing the course invalidates every assignment and submission
            // chosen under the previous one: a stale id must never be the
            // target of "correct" or "push grades" in another classroom.
            Assignments.Clear();
            Submissions.Clear();
            SelectedAssignment = null;
            SelectedSubmission = null;

            OnPropertyChanged(nameof(HasCourse));
            OnPropertyChanged(nameof(HasAssignmentTarget));
            OnPropertyChanged(nameof(HasSubmissionTarget));
        OnPropertyChanged(nameof(CanPushGrades));
        OnPropertyChanged(nameof(CanOpenClassroom));
        OnPropertyChanged(nameof(AssignmentCountText));
        OnPropertyChanged(nameof(SelectedCourseText));
            OnPropertyChanged(nameof(SelectedAssignmentText));
            OnPropertyChanged(nameof(AssignmentMetaText));
            if (value is not null && !_restoringSelection)
            {
                _ = LoadAssignmentsAsync(value.Id);
            }
        }
    }

    public ClassroomAssignmentDto? SelectedAssignment
    {
        get => _selectedAssignment;
        set
        {
            if (!SetProperty(ref _selectedAssignment, value))
            {
                return;
            }

            Submissions.Clear();
            SelectedSubmission = null;
            OnPropertyChanged(nameof(HasAssignmentTarget));
            OnPropertyChanged(nameof(HasSubmissionTarget));
            OnPropertyChanged(nameof(CanPushGrades));
            OnPropertyChanged(nameof(SelectedAssignmentText));
            OnPropertyChanged(nameof(AssignmentMetaText));
            OnPropertyChanged(nameof(SubmissionsSummary));
            if (value is null)
            {
                SetScheme("Choose an assignment to see its mark scheme.", string.Empty, false,
                    checking: false);
            }
            else if (SelectedCourse is not null && !_restoringSelection)
            {
                _ = LoadSubmissionsAsync(SelectedCourse.Id, value.Id);
                _ = LoadMarkSchemeAsync(value.Id);
            }
        }
    }

    public ClassroomSubmissionDto? SelectedSubmission
    {
        get => _selectedSubmission;
        set
        {
            if (SetProperty(ref _selectedSubmission, value))
            {
                OnPropertyChanged(nameof(HasSubmissionTarget));
            }
        }
    }

    /// <summary>What the backend last answered, said in words and labelled
    /// facts. The raw JSON stays inside technical details.</summary>
    public ObservableCollection<FactRow> DetailFacts { get; } = new();

    public string DetailNote
    {
        get => _detailNote;
        private set
        {
            if (SetProperty(ref _detailNote, value))
            {
                OnPropertyChanged(nameof(HasDetail));
            }
        }
    }

    public string DetailRaw
    {
        get => _detailRaw;
        private set
        {
            if (SetProperty(ref _detailRaw, value))
            {
                OnPropertyChanged(nameof(HasDetailRaw));
            }
        }
    }

    public bool HasDetail => DetailFacts.Count > 0 || !string.IsNullOrWhiteSpace(DetailNote);

    public bool HasDetailRaw => !string.IsNullOrWhiteSpace(DetailRaw);

    private void SetScheme(string title, string detail, bool attached, bool checking)
    {
        _schemeTitle = title;
        _schemeDetail = detail;
        _schemeAttached = attached;
        _schemeChecking = checking;
        OnPropertyChanged(nameof(SchemeAttached));
        OnPropertyChanged(nameof(SchemeMissing));
        OnPropertyChanged(nameof(SchemeChecking));
        OnPropertyChanged(nameof(SchemeTitle));
        OnPropertyChanged(nameof(SchemeDetail));
        OnPropertyChanged(nameof(HasSchemeDetail));
        OnPropertyChanged(nameof(UploadSchemeLabel));
    }

    /// <summary>Reads the scheme actually attached to this assignment.
    ///
    /// A late answer for an assignment the teacher has already left is dropped:
    /// the card must describe the assignment in front of them, not the one they
    /// clicked before.</summary>
    private Task LoadMarkSchemeAsync(string courseId, string courseworkId) =>
        SelectedCourse?.Id == courseId ? LoadMarkSchemeAsync(courseworkId) : Task.CompletedTask;

    private async Task LoadMarkSchemeAsync(string courseworkId)
    {
        SetScheme("Checking this assignment's mark scheme…", string.Empty, false, checking: true);
        try
        {
            var payload = await Session.Api.MarkSchemeAsync(courseworkId);
            if (SelectedAssignment?.Id != courseworkId)
            {
                return;
            }

            ApplyScheme(payload);
        }
        catch (ApiException ex)
        {
            if (SelectedAssignment?.Id == courseworkId)
            {
                SetScheme("The mark scheme could not be read", ex.Display, false, checking: false);
            }
        }
        catch (Exception ex)
        {
            if (SelectedAssignment?.Id == courseworkId)
            {
                SetScheme("The mark scheme could not be read", UserMessage.For(ex), false,
                    checking: false);
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
            SetScheme("Not attached",
                "Corrections for this assignment cannot start until a scheme is uploaded.",
                attached: false, checking: false);
            return;
        }

        var parts = new List<string>();
        var fileName = Text("file_name");
        if (!string.IsNullOrWhiteSpace(fileName))
        {
            parts.Add(fileName!);
        }

        if (Number("pages") is { } pages && pages > 0)
        {
            parts.Add(pages == 1 ? "1 page" : $"{pages} pages");
        }

        if (Number("total_marks") is { } marks && marks > 0)
        {
            parts.Add($"{marks} marks");
        }

        if (Number("subquestion_count") is { } partsCount && partsCount > 0)
        {
            parts.Add($"{partsCount} marking points");
        }

        var processed = Flag("processed");
        var state = processed ? SchemeStateText(Text("verification_state")) : null;
        var title = processed
            ? (string.IsNullOrEmpty(state) ? "Attached" : $"Attached · {state}")
            : "Attached · not analysed yet";

        var detail = string.Join(" · ", parts);
        if (!processed)
        {
            detail = (detail.Length > 0 ? detail + " — " : string.Empty)
                     + "it is analysed the first time work is corrected.";
        }

        SetScheme(title, detail, attached: true, checking: false);
    }

    private static string? SchemeStateText(string? state)
    {
        var value = (state ?? string.Empty).Trim().ToUpperInvariant();
        return value switch
        {
            "VERIFIED" => "verified",
            "NEEDS_MANUAL_REVIEW" => "needs review",
            "NEEDS_REPAIR" => "needs repair",
            "NOT_VERIFIED" => "not verified yet",
            "VERIFYING" => "being checked",
            "REPAIRING" or "REVERIFYING" => "being re-read",
            _ => string.IsNullOrWhiteSpace(value) ? null : PayloadFacts.StatusLabel(state),
        };
    }

    private void ShowDetail(JsonElement payload, string fallbackNote)
    {
        DetailFacts.Clear();
        foreach (var fact in PayloadFacts.Of(payload))
        {
            // Identifiers belong in technical details; the panel above says
            // what happened, not which uuid it happened to.
            if (fact.Label.EndsWith(" ID", StringComparison.Ordinal)
                || fact.Label is "Kind" or "Path" or "Route")
            {
                continue;
            }

            DetailFacts.Add(fact);
        }

        DetailRaw = PayloadFacts.Raw(payload);
        var summary = PayloadFacts.Summarize(payload);
        DetailNote = string.IsNullOrWhiteSpace(summary) ? fallbackNote : summary;
        OnPropertyChanged(nameof(HasDetail));
    }

    public AsyncCommand ConnectCommand => Command(async () =>
    {
        var payload = await Session.Api.ConnectClassroomAsync();
        ShowDetail(payload, "Google sign-in started. Finish it in the browser window that opened.");
        await LoadAsync();
        Status = "The backend started the Google consent flow; complete it in the browser it opened.";
    });

    public AsyncCommand DisconnectCommand => Command(async () =>
    {
        if (!Session.Dialogs.Confirm("Disconnect Classroom",
                "Disconnect Google Classroom? Local data is kept; only the Google connection is removed."))
        {
            return;
        }

        await Session.Api.DisconnectClassroomAsync();
        await LoadAsync();
    });

    /// <summary>Classroom owns the synchronized data, so the page frame shows
    /// the shared Sync control here.</summary>
    public override bool SyncsFromClassroom => true;

    /// <summary>Runs the shared full sync and WAITS for the backend's terminal
    /// state before reloading.
    ///
    /// The previous version started the job, slept 1.5 s, read ONE progress
    /// snapshot and reloaded — so the page always showed "Connecting… 5 %"
    /// while courses, students and submissions were still arriving, and the
    /// rows it had just fetched were the ones from BEFORE the sync. That is
    /// exactly why the sync looked like it stopped halfway.</summary>
    public AsyncCommand SyncCommand => Command(async () =>
    {
        var result = await Session.Sync.SyncAsync();
        try
        {
            ShowDetail(await Session.Api.ClassroomSyncProgressAsync(),
                result?.Summary ?? "Sync finished.");
        }
        catch (ApiException ex)
        {
            DetailNote = ex.Display;
            DetailRaw = ex.Technical;
            OnPropertyChanged(nameof(HasDetail));
            OnPropertyChanged(nameof(HasDetailRaw));
        }

        await LoadAsync();
        if (result is not null)
        {
            Status = result.Summary;
        }
    });

    public AsyncCommand CorrectSelectedCommand => Command(async () =>
    {
        if (SelectedCourse is null || SelectedAssignment is null || SelectedSubmission is null)
        {
            Error = "Select a course, an assignment and a submission.";
            return;
        }

        if (!Session.Dialogs.Confirm("Correct submission",
                $"Correct the submission of {SelectedSubmission.TitleText}?\n\n"
                + $"Course: {SelectedCourse.Title}\nAssignment: {SelectedAssignment.TitleText}\n\n"
                + "The AI marker will run once and only this paper is marked."))
        {
            return;
        }

        var payload = JsonSerializer.SerializeToElement(
            new { submission_ids = new[] { SelectedSubmission.Id } });
        ShowDetail(await Session.Api.CorrectSubmissionAsync(
            SelectedCourse.Id, SelectedAssignment.Id, payload),
            $"Correction queued for {SelectedSubmission.TitleText}.");
        Status = "Correction queued.";
    });

    public AsyncCommand CorrectAllCommand => Command(async () =>
    {
        if (SelectedCourse is null || SelectedAssignment is null)
        {
            Error = "Select a course and an assignment.";
            return;
        }

        if (!Session.Dialogs.Confirm("Correct the whole assignment",
                $"Correct every eligible submission of \"{SelectedAssignment.TitleText}\" "
                + $"in \"{SelectedCourse.Title}\"?\n\n"
                + "Students who have not handed anything in are skipped, and the assignment must be "
                + "allow-listed with a mark scheme."))
        {
            return;
        }

        ShowDetail(await Session.Api.CorrectAllAsync(
            SelectedCourse.Id, SelectedAssignment.Id, refresh: true),
            $"Every eligible paper in “{SelectedAssignment.TitleText}” was queued.");
        await LoadAsync();
    });

    public AsyncCommand ToggleAllowlistCommand => Command(async () =>
    {
        if (SelectedCourse is null || SelectedAssignment is null)
        {
            return;
        }

        await Session.Api.SetAllowlistAsync(SelectedCourse.Id, SelectedAssignment.Id, true);
        DetailFacts.Clear();
        DetailRaw = string.Empty;
        DetailNote = $"“{SelectedAssignment.TitleText}” is allow-listed for automatic corrections.";
        OnPropertyChanged(nameof(HasDetail));
        OnPropertyChanged(nameof(HasDetailRaw));
    });

    public AsyncCommand UploadMarkSchemeCommand => Command(async () =>
    {
        if (SelectedCourse is null || SelectedAssignment is null)
        {
            return;
        }

        var path = Session.Dialogs.PickOpenFile("Select the mark scheme PDF", "PDF (*.pdf)|*.pdf");
        if (path is null)
        {
            return;
        }

        var courseworkId = SelectedAssignment.Id;
        ShowDetail(await Session.Api.UploadMarkSchemeAsync(
            SelectedCourse.Id, courseworkId, path),
            $"The mark scheme for “{SelectedAssignment.TitleText}” was uploaded.");
        Status = "Mark scheme uploaded.";
        // The card must show the file that is now attached, not the state it
        // was in before the upload.
        await LoadMarkSchemeAsync(courseworkId);
    });

    public AsyncCommand PushGradesCommand => Command(async () =>
    {
        if (SelectedCourse is null || SelectedAssignment is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Push draft grades",
                $"Write the corrected marks for \"{SelectedAssignment.TitleText}\" "
                + $"in \"{SelectedCourse.Title}\" back to Google Classroom as draft grades?\n\n"
                + "This changes grades in Google Classroom; it needs the grade write-back setting enabled."))
        {
            return;
        }

        ShowDetail(await Session.Api.PushGradesAsync(SelectedCourse.Id, SelectedAssignment.Id),
            "Draft grades were written back to Google Classroom.");
    });

    public RelayCommand OpenExternalCommand => Sync(() =>
    {
        var link = SelectedCourse?.AlternateLink;
        if (string.IsNullOrWhiteSpace(link))
        {
            Error = "This course has no Google Classroom link.";
            return;
        }

        Session.Dialogs.OpenExternal(link!);
    });

    protected override async Task LoadCoreAsync()
    {
        _status = await Session.Api.ClassroomStatusAsync();
        OnPropertyChanged(nameof(StatusText));
        OnPropertyChanged(nameof(ProblemText));
        OnPropertyChanged(nameof(IsConnected));
        OnPropertyChanged(nameof(IsDisconnected));
        OnPropertyChanged(nameof(LastSyncText));
        OnPropertyChanged(nameof(StateTitle));
        OnPropertyChanged(nameof(StateDetail));
        OnPropertyChanged(nameof(ConnectedSinceText));
        OnPropertyChanged(nameof(SyncDetail));
        OnPropertyChanged(nameof(WriteBackText));
        OnPropertyChanged(nameof(WriteBackEnabled));
        OnPropertyChanged(nameof(WriteBackLabel));
        OnPropertyChanged(nameof(CanOpenClassroom));
        OnPropertyChanged(nameof(CanPushGrades));
        OnPropertyChanged(nameof(CourseCountText));
        OnPropertyChanged(nameof(AssignmentCountText));
        OnPropertyChanged(nameof(SelectedCourseText));
        OnPropertyChanged(nameof(SelectedAssignmentText));
        OnPropertyChanged(nameof(AssignmentMetaText));
        OnPropertyChanged(nameof(SubmissionsSummary));
        OnPropertyChanged(nameof(SchemeAttached));
        OnPropertyChanged(nameof(SchemeMissing));
        OnPropertyChanged(nameof(SchemeChecking));
        OnPropertyChanged(nameof(SchemeTitle));
        OnPropertyChanged(nameof(SchemeDetail));
        OnPropertyChanged(nameof(HasSchemeDetail));
        OnPropertyChanged(nameof(UploadSchemeLabel));

        // /classroom/sync streams server-sent events, so it can never be read as
        // JSON. The pollable snapshot is /classroom/sync/progress; a failure here
        // is cosmetic and must not stop the course list from loading.
        try
        {
            var progress = await Session.Api.ClassroomSyncProgressAsync();
            ShowDetail(progress, "Sync progress read from the backend.");
        }
        catch (ApiException ex)
        {
            DetailNote = ex.Display;
            DetailRaw = ex.Technical;
            OnPropertyChanged(nameof(HasDetail));
            OnPropertyChanged(nameof(HasDetailRaw));
        }

        if (!_status.Connected)
        {
            // A stale selection from an earlier connection must not leave the
            // course/assignment actions looking available.
            Courses.Clear();
            Assignments.Clear();
            Submissions.Clear();
            SelectedCourse = null;
            SelectedAssignment = null;
            SelectedSubmission = null;
            return;
        }

        var keepCourseId = SelectedCourse?.Id;
        var keepAssignmentId = SelectedAssignment?.Id;
        var keepSubmissionId = SelectedSubmission?.Id;

        var courses = await Session.Api.ClassroomCoursesAsync();
        Courses.Clear();
        foreach (var course in courses.Courses)
        {
            Courses.Add(course);
        }

        OnPropertyChanged(nameof(CourseCountText));

        // Keep the teacher's own choice across a refresh, but never pick one for
        // them: auto-selecting the first course and its first assignment armed
        // "correct" and "push grades" against a classroom nobody chose. The
        // destructive actions must follow an explicit choice, not list order.
        var keep = keepCourseId is null
            ? null
            : Courses.FirstOrDefault(c => c.Id == keepCourseId);
        _restoringSelection = true;
        try
        {
            SelectedCourse = keep;
        }
        finally
        {
            _restoringSelection = false;
        }

        if (keep is not null)
        {
            await LoadAssignmentsAsync(keep.Id, keepAssignmentId, keepSubmissionId);
        }
    }

    private async Task LoadAssignmentsAsync(string courseId, string? keepAssignmentId = null,
        string? keepSubmissionId = null)
    {
        try
        {
            var payload = await Session.Api.ClassroomCourseAssignmentsAsync(courseId);
            Assignments.Clear();
            if (payload.TryGetProperty("assignments", out var list) && list.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in list.EnumerateArray())
                {
                    Assignments.Add(new ClassroomAssignmentDto
                    {
                        Id = item.TryGetProperty("id", out var id) ? id.GetString() ?? string.Empty : string.Empty,
                        Title = item.TryGetProperty("title", out var title) ? title.GetString() : null,
                        MaxPoints = item.TryGetProperty("max_points", out var points)
                            && points.ValueKind == JsonValueKind.Number ? points.GetDouble() : null,
                        State = item.TryGetProperty("state", out var state) ? state.GetString() : null,
                        DueDate = item.TryGetProperty("due_date", out var due) ? due.GetString() : null,
                        Raw = item.Clone(),
                    });
                }
            }

            OnPropertyChanged(nameof(AssignmentCountText));

            // The teacher picks the assignment; a reload only keeps a choice
            // that still exists in the fresh list.
            var restored = keepAssignmentId is null
                ? null
                : Assignments.FirstOrDefault(a => a.Id == keepAssignmentId);
            _restoringSelection = true;
            try
            {
                SelectedAssignment = restored;
            }
            finally
            {
                _restoringSelection = false;
            }

            if (restored is not null)
            {
                await LoadSubmissionsAsync(courseId, restored.Id, keepSubmissionId);
                await LoadMarkSchemeAsync(courseId, restored.Id);
            }
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
    }

    private async Task LoadSubmissionsAsync(string courseId, string courseworkId,
        string? keepSubmissionId = null)
    {
        try
        {
            var payload = await Session.Api.ClassroomSubmissionsAsync(courseId, courseworkId);
            Submissions.Clear();
            OnPropertyChanged(nameof(SubmissionsSummary));
            if (payload.TryGetProperty("submissions", out var list) && list.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in list.EnumerateArray())
                {
                    Submissions.Add(new ClassroomSubmissionDto
                    {
                        Id = item.TryGetProperty("id", out var id) ? id.GetString() ?? string.Empty : string.Empty,
                        UserId = item.TryGetProperty("user_id", out var user) ? user.GetString() : null,
                        StudentName = item.TryGetProperty("student_name", out var name) ? name.GetString() : null,
                        State = item.TryGetProperty("state", out var state) ? state.GetString() : null,
                        Grade = item.TryGetProperty("assigned_grade", out var grade) && grade.ValueKind != JsonValueKind.Null
                            ? grade.ToString()
                            : null,
                        CourseworkId = courseworkId,
                    });
                }
            }

            OnPropertyChanged(nameof(SubmissionsSummary));

            var restored = keepSubmissionId is null
                ? null
                : Submissions.FirstOrDefault(s => s.Id == keepSubmissionId);
            _restoringSelection = true;
            try
            {
                SelectedSubmission = restored;
            }
            finally
            {
                _restoringSelection = false;
            }
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
    }
}
