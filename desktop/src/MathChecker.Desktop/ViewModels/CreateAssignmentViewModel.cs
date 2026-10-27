using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.UI;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// Creating an assignment from the desktop: its name, where it lives, the mark
/// it is out of, and the mark scheme it will be corrected against.
///
/// The assignments list only offered a one-line title box, so an assignment
/// could never arrive with the grade it is marked out of or the scheme the
/// correction engine needs. This page is that missing form; it creates a local
/// assignment or one in Google Classroom, uploads the scheme, then opens the
/// new assignment's own page.
/// </summary>
public sealed class CreateAssignmentViewModel : PageViewModel
{
    /// <summary>The destination value that means "no Google Classroom".</summary>
    public const string LocalDestination = "local";

    private string _assignmentTitle = string.Empty;
    private string _notes = string.Empty;
    private string _dueDate = string.Empty;
    private string _maxPoints = string.Empty;
    private string _destination = LocalDestination;
    private string? _schemePath;
    private string _schemeName = string.Empty;
    private bool _classroomConnected;
    private bool _coursesLoaded;

    public CreateAssignmentViewModel(AppSession session)
        : base(session, "new-assignment", "New assignment")
    {
        PickSchemeCommand = new RelayCommand(PickScheme);
        ClearSchemeCommand = new RelayCommand(ClearScheme, () => HasScheme);
        CancelCommand = new RelayCommand(() => Session.GoTo("assignments"));
        CreateCommand = Command(CreateAsync, () => CanCreate);
    }

    public ObservableCollection<ChoiceOption> Destinations { get; } = new()
    {
        new("Only on this computer (no Google Classroom)", LocalDestination),
    };

    public RelayCommand PickSchemeCommand { get; }
    public RelayCommand ClearSchemeCommand { get; }
    public RelayCommand CancelCommand { get; }
    public AsyncCommand CreateCommand { get; }

    /// <summary>One-line explanation under the page title.</summary>
    public string PageDescription => "Name the assignment, say what it is marked out of, and attach "
        + "the mark scheme the papers will be corrected against.";

    /// <summary>The assignment's name (the page's own title is the base class's).</summary>
    public string AssignmentTitle
    {
        get => _assignmentTitle;
        set
        {
            if (SetProperty(ref _assignmentTitle, value))
            {
                OnPropertyChanged(nameof(CanCreate));
                CreateCommand.RaiseCanExecuteChanged();
            }
        }
    }

    public string Notes
    {
        get => _notes;
        set => SetProperty(ref _notes, value);
    }

    public string DueDate
    {
        get => _dueDate;
        set => SetProperty(ref _dueDate, value);
    }

    /// <summary>The maximum mark (the grade scale) the paper is marked out of.</summary>
    public string MaxPoints
    {
        get => _maxPoints;
        set => SetProperty(ref _maxPoints, value);
    }

    /// <summary>Destination value: <see cref="LocalDestination"/> or a Classroom course id.</summary>
    public string Destination
    {
        get => _destination;
        set
        {
            if (SetProperty(ref _destination, value))
            {
                OnPropertyChanged(nameof(DestinationHint));
                OnPropertyChanged(nameof(CreatesInClassroom));
            }
        }
    }

    public bool CreatesInClassroom => !string.Equals(Destination, LocalDestination, StringComparison.Ordinal);

    public string DestinationHint => !CreatesInClassroom
        ? "Kept on this computer only. Papers can be uploaded and corrected here."
        : ClassroomConnected
            ? "Created in Google Classroom, then stored here so it can be corrected."
            : "Google Classroom is not connected yet. Create it here only, or connect the account on the People page first.";

    public bool HasScheme => !string.IsNullOrWhiteSpace(_schemePath);

    public string SchemeName => string.IsNullOrWhiteSpace(_schemeName)
        ? "No mark scheme attached yet"
        : _schemeName;

    public string SchemeHint => HasScheme
        ? "The correction engine reads this PDF. Replace it on the assignment page if you picked the wrong file."
        : "Optional now, required before correcting: the official mark scheme as a PDF.";

    public bool ClassroomConnected
    {
        get => _classroomConnected;
        private set
        {
            if (SetProperty(ref _classroomConnected, value))
            {
                OnPropertyChanged(nameof(DestinationHint));
            }
        }
    }

    public bool CanCreate => !string.IsNullOrWhiteSpace(AssignmentTitle);

    protected override async Task LoadCoreAsync()
    {
        if (!_coursesLoaded)
        {
            await LoadDestinationsAsync().ConfigureAwait(true);
        }
    }

    private async Task LoadDestinationsAsync()
    {
        try
        {
            ClassroomConnected = (await Session.Api.ClassroomStatusAsync().ConfigureAwait(true)).Connected;
        }
        catch (Exception ex)
        {
            Session.Log.Warn("classroom status unavailable on new assignment: " + ex.Message);
        }

        var choices = new List<ChoiceOption>
        {
            new("Only on this computer (no Google Classroom)", LocalDestination),
        };

        if (ClassroomConnected)
        {
            try
            {
                var courses = await Session.Api.ClassroomCoursesAsync().ConfigureAwait(true);
                foreach (var course in courses.Courses)
                {
                    if (!string.IsNullOrWhiteSpace(course.Id))
                    {
                        choices.Add(new ChoiceOption(course.Title, course.Id));
                    }
                }

                _coursesLoaded = true;
            }
            catch (Exception ex)
            {
                // The local destination still works, so a Classroom failure is a
                // note on the page, not a dead end.
                Session.Log.Warn("classroom courses unavailable: " + ex.Message);
                Status = "Google Classroom classes could not be listed: " + ex.Message;
            }
        }

        var selected = Destination;
        Destinations.Clear();
        foreach (var choice in choices)
        {
            Destinations.Add(choice);
        }

        Destination = Destinations.Any(c => c.Value == selected) ? selected : LocalDestination;
    }

    private void PickScheme()
    {
        var path = Session.Dialogs.PickOpenFile("Select the mark scheme PDF", "PDF (*.pdf)|*.pdf");
        if (path is null)
        {
            return;
        }

        if (!path.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase))
        {
            Error = "The mark scheme must be a PDF.";
            return;
        }

        _schemePath = path;
        _schemeName = Path.GetFileName(path);
        Error = null;
        RaiseScheme();
    }

    private void ClearScheme()
    {
        _schemePath = null;
        _schemeName = string.Empty;
        RaiseScheme();
    }

    private void RaiseScheme()
    {
        OnPropertyChanged(nameof(HasScheme));
        OnPropertyChanged(nameof(SchemeName));
        OnPropertyChanged(nameof(SchemeHint));
        ClearSchemeCommand.RaiseCanExecuteChanged();
    }

    private async Task CreateAsync()
    {
        var title = AssignmentTitle.Trim();
        if (title.Length == 0)
        {
            Error = "Give the assignment a name.";
            return;
        }

        double? points = null;
        if (!string.IsNullOrWhiteSpace(MaxPoints))
        {
            if (!double.TryParse(MaxPoints, out var parsed) || parsed <= 0)
            {
                Error = "The total marks must be a positive number.";
                return;
            }

            points = parsed;
        }

        string? due = null;
        if (!string.IsNullOrWhiteSpace(DueDate))
        {
            if (!DateTime.TryParse(DueDate, out var when))
            {
                Error = "Enter the due date as YYYY-MM-DD, for example 2026-11-14.";
                return;
            }

            due = when.ToString("yyyy-MM-dd");
        }

        var notes = string.IsNullOrWhiteSpace(Notes) ? null : Notes.Trim();
        var destination = Destination;
        var courseId = destination;
        JsonElement created;
        if (string.Equals(destination, LocalDestination, StringComparison.Ordinal))
        {
            created = await Session.Api.CreateLocalAssignmentAsync(title, points, due, notes)
                .ConfigureAwait(true);
            courseId = ReadString(created, "course_id") ?? LocalDestination;
        }
        else
        {
            created = await Session.Api.CreateClassroomAssignmentAsync(destination, title, notes,
                points, due).ConfigureAwait(true);
        }

        var id = CreatedId(created);
        if (string.IsNullOrWhiteSpace(id))
        {
            Error = "The assignment was created, but the backend did not return its id, so it was not opened. "
                + "Refresh the assignments list to find it.";
            return;
        }

        string? schemeProblem = null;
        if (HasScheme)
        {
            try
            {
                await Session.Api.UploadMarkSchemeAsync(courseId, id, _schemePath!).ConfigureAwait(true);
            }
            catch (ApiException ex)
            {
                schemeProblem = ex.Display;
            }
            catch (Exception ex)
            {
                schemeProblem = ex.Message;
            }
        }

        var attachedScheme = HasScheme && schemeProblem is null;
        ResetForm();

        Session.Notify(schemeProblem is null
            ? attachedScheme
                ? "Assignment created with its mark scheme."
                : "Assignment created. Attach a mark scheme before correcting papers."
            : "Assignment created, but the mark scheme upload failed: " + schemeProblem);
        Session.OpenAssignment(id);
    }

    /// <summary>Clears the form: the page stays in the navigation list, so it
    /// must not reopen with a half-finished assignment in it.</summary>
    private void ResetForm()
    {
        AssignmentTitle = string.Empty;
        Notes = string.Empty;
        DueDate = string.Empty;
        MaxPoints = string.Empty;
        Destination = LocalDestination;
        ClearScheme();
    }

    /// <summary>The created id, whether the backend put it at the root or under
    /// <c>coursework</c> (the Classroom shape).</summary>
    internal static string? CreatedId(JsonElement payload)
    {
        var direct = ReadString(payload, "id");
        if (direct is not null)
        {
            return direct;
        }

        return payload.ValueKind == JsonValueKind.Object
               && payload.TryGetProperty("coursework", out var coursework)
            ? ReadString(coursework, "id")
            : null;
    }

    private static string? ReadString(JsonElement payload, string name)
    {
        if (payload.ValueKind != JsonValueKind.Object
            || !payload.TryGetProperty(name, out var value)
            || value.ValueKind != JsonValueKind.String)
        {
            return null;
        }

        var text = value.GetString();
        return string.IsNullOrWhiteSpace(text) ? null : text;
    }
}
