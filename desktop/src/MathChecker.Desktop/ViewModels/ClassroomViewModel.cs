using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class ClassroomViewModel : PageViewModel
{
    private ClassroomStatusDto? _status;
    private ClassroomCourseDto? _selectedCourse;
    private ClassroomAssignmentDto? _selectedAssignment;
    private ClassroomSubmissionDto? _selectedSubmission;
    private string _detailText = string.Empty;
    private string _syncText = string.Empty;

    public ClassroomViewModel(AppSession session)
        : base(session, "classroom", "Classroom")
    {
    }

    public ObservableCollection<ClassroomCourseDto> Courses { get; } = new();
    public ObservableCollection<ClassroomAssignmentDto> Assignments { get; } = new();
    public ObservableCollection<ClassroomSubmissionDto> Submissions { get; } = new();

    public string StatusText => _status?.StateText ?? "—";
    public string ProblemText => _status?.Problem ?? string.Empty;
    public bool IsConnected => _status?.Connected ?? false;
    public string LastSyncText => _status is null ? "—" : EpochTime.Format(_status.LastSync, "never");

    public ClassroomCourseDto? SelectedCourse
    {
        get => _selectedCourse;
        set
        {
            if (SetProperty(ref _selectedCourse, value) && value is not null)
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
            if (SetProperty(ref _selectedAssignment, value) && value is not null
                && SelectedCourse is not null)
            {
                _ = LoadSubmissionsAsync(SelectedCourse.Id, value.Id);
            }
        }
    }

    public ClassroomSubmissionDto? SelectedSubmission
    {
        get => _selectedSubmission;
        set => SetProperty(ref _selectedSubmission, value);
    }

    public string DetailText
    {
        get => _detailText;
        private set => SetProperty(ref _detailText, value);
    }

    public string SyncText
    {
        get => _syncText;
        private set => SetProperty(ref _syncText, value);
    }

    public AsyncCommand ConnectCommand => Command(async () =>
    {
        var payload = await Session.Api.ConnectClassroomAsync();
        DetailText = Json.Pretty(payload);
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

    public AsyncCommand SyncCommand => Command(async () =>
    {
        var started = await Session.Api.StartClassroomSyncAsync();
        DetailText = Json.Pretty(started);
        await Task.Delay(1500);
        var progress = await Session.Api.ClassroomSyncProgressAsync();
        SyncText = Json.Pretty(progress);
        await LoadAsync();
    });

    public AsyncCommand CorrectSelectedCommand => Command(async () =>
    {
        if (SelectedCourse is null || SelectedAssignment is null || SelectedSubmission is null)
        {
            Error = "Select a course, an assignment and a submission.";
            return;
        }

        if (!Session.Dialogs.Confirm("Correct submission",
                $"Correct the submission of {SelectedSubmission.TitleText}? The AI marker will run once."))
        {
            return;
        }

        var payload = JsonSerializer.SerializeToElement(
            new { submission_ids = new[] { SelectedSubmission.Id } });
        DetailText = Json.Pretty(await Session.Api.CorrectSubmissionAsync(
            SelectedCourse.Id, SelectedAssignment.Id, payload));
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
                $"Correct every eligible submission of \"{SelectedAssignment.TitleText}\"?\n\n"
                + "Students who have not handed anything in are skipped, and the assignment must be "
                + "allow-listed with a mark scheme."))
        {
            return;
        }

        DetailText = Json.Pretty(await Session.Api.CorrectAllAsync(
            SelectedCourse.Id, SelectedAssignment.Id, refresh: true));
        await LoadAsync();
    });

    public AsyncCommand ToggleAllowlistCommand => Command(async () =>
    {
        if (SelectedCourse is null || SelectedAssignment is null)
        {
            return;
        }

        await Session.Api.SetAllowlistAsync(SelectedCourse.Id, SelectedAssignment.Id, true);
        DetailText = "Assignment allow-listed for automatic corrections.";
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

        DetailText = Json.Pretty(await Session.Api.UploadMarkSchemeAsync(
            SelectedCourse.Id, SelectedAssignment.Id, path));
        Status = "Mark scheme uploaded.";
    });

    public AsyncCommand PushGradesCommand => Command(async () =>
    {
        if (SelectedCourse is null || SelectedAssignment is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Push draft grades",
                "Write the corrected marks back to Google Classroom as draft grades? This needs the "
                + "grade write-back setting enabled."))
        {
            return;
        }

        DetailText = Json.Pretty(await Session.Api.PushGradesAsync(SelectedCourse.Id, SelectedAssignment.Id));
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
        OnPropertyChanged(nameof(LastSyncText));

        SyncText = Json.Pretty(await Session.Api.ClassroomSyncStatusAsync());

        if (!_status.Connected)
        {
            Courses.Clear();
            return;
        }

        var courses = await Session.Api.ClassroomCoursesAsync();
        Courses.Clear();
        foreach (var course in courses.Courses)
        {
            Courses.Add(course);
        }

        if (SelectedCourse is null && Courses.Count > 0)
        {
            SelectedCourse = Courses[0];
        }
    }

    private async Task LoadAssignmentsAsync(string courseId)
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

            SelectedAssignment = Assignments.FirstOrDefault();
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
    }

    private async Task LoadSubmissionsAsync(string courseId, string courseworkId)
    {
        try
        {
            var payload = await Session.Api.ClassroomSubmissionsAsync(courseId, courseworkId);
            Submissions.Clear();
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
                        Grade = item.TryGetProperty("assigned_grade", out var grade) ? grade.ToString() : null,
                        CourseworkId = courseworkId,
                    });
                }
            }

            SelectedSubmission = Submissions.FirstOrDefault();
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
    }
}
