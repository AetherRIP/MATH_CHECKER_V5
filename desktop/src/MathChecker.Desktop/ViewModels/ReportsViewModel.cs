using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One row of a rendered report, mapped from the backend's own fields.</summary>
public sealed class ReportRow
{
    public string Name { get; init; } = string.Empty;

    /// <summary>The teacher-assigned class (10A, 10B, …), not a course name.</summary>
    public string ClassText { get; init; } = string.Empty;

    public string Score { get; init; } = string.Empty;
    public string Percentage { get; init; } = string.Empty;

    /// <summary>Google Classroom's own grade for the same assignment set.</summary>
    public string Classroom { get; init; } = string.Empty;

    /// <summary>Each assignment's own marks, so a combined total never hides one.</summary>
    public string Breakdown { get; init; } = string.Empty;

    public string Note { get; init; } = string.Empty;
}

/// <summary>One assignment included in the report, with the marks it carried.</summary>
public sealed class ReportAssignment
{
    public string Title { get; init; } = string.Empty;
    public string Meta { get; init; } = string.Empty;
    public string Stats { get; init; } = string.Empty;

    /// <summary>Course, marks and the assignment's own results on one line.</summary>
    public string Feed => string.Join(" · ", new[] { Meta, Stats }
        .Where(part => !string.IsNullOrWhiteSpace(part)));
}

/// <summary>An assignment the teacher can tick for the report.</summary>
public sealed class AssignmentChoice : ObservableObject
{
    private bool _isSelected;

    public AssignmentChoice(AssignmentDto assignment, string courseLabel)
    {
        Assignment = assignment;
        CourseLabel = courseLabel;
    }

    public AssignmentDto Assignment { get; }
    public string Id => Assignment.Id;
    public string CourseId => Assignment.CourseId ?? string.Empty;
    public string DisplayTitle => Assignment.DisplayTitle;
    public string CourseLabel { get; }

    public string MetaText
    {
        get
        {
            var parts = new List<string> { CourseLabel };
            if (Assignment.MaxPoints is { } points)
            {
                parts.Add($"{points:0.#} marks");
            }

            return string.Join(" · ", parts);
        }
    }

    public bool IsSelected
    {
        get => _isSelected;
        set
        {
            if (SetProperty(ref _isSelected, value))
            {
                SelectionChanged?.Invoke(this, EventArgs.Empty);
            }
        }
    }

    public event EventHandler? SelectionChanged;
}

public sealed class ReportsViewModel : PageViewModel
{
    private readonly List<AssignmentChoice> _allAssignments = new();
    private readonly List<string> _studentClasses = new();
    private const string AllClasses = "All classes";

    private ClassDto? _selectedClass;
    private string _studentClass = AllClasses;
    private string _studentQuery = string.Empty;
    private string _headline = string.Empty;
    private string _statsLine = string.Empty;
    private string _rawText = string.Empty;
    private bool _isScopeExpanded = true;

    public ReportsViewModel(AppSession session)
        : base(session, "reports", "Reports")
    {
        Rows.CollectionChanged += (_, _) => OnPropertyChanged(nameof(RowCountText));
        Assignments.CollectionChanged += (_, _) => OnPropertyChanged(nameof(AssignmentCountText));

        SelectAllCommand = new RelayCommand(SelectAll, () => VisibleAssignments.Any(a => !a.IsSelected));
        ClearSelectionCommand = new RelayCommand(ClearSelection, () => SelectedIds.Count > 0);
        ToggleScopeCommand = new RelayCommand(() => IsScopeExpanded = !IsScopeExpanded);
    }

    /// <summary>
    /// The scope band (filters plus the assignment picker) is tall. Once a
    /// report is built the teacher needs the rows, so the band folds down to one
    /// summary line — and it is never hidden without a way to bring it back.
    /// </summary>
    public bool IsScopeExpanded
    {
        get => _isScopeExpanded;
        set
        {
            if (SetProperty(ref _isScopeExpanded, value))
            {
                OnPropertyChanged(nameof(IsScopeCollapsed));
                OnPropertyChanged(nameof(ScopeButtonText));
            }
        }
    }

    public bool IsScopeCollapsed => !IsScopeExpanded;

    public string ScopeButtonText => IsScopeExpanded ? "Hide" : "Change selection";

    /// <summary>One line that says exactly what the folded band was set to.</summary>
    public string ScopeSummary
    {
        get
        {
            var chosen = SelectedIds.Count;
            var assignments = chosen == 0
                ? "No assignments"
                : chosen == 1 ? "1 assignment" : $"{chosen} assignments";
            var course = SelectedClass is null || string.IsNullOrWhiteSpace(SelectedClass.Id)
                ? "All classes"
                : SelectedClass.Title;
            var scope = $"{assignments} · {course} · {StudentClass}";
            return string.IsNullOrWhiteSpace(StudentQuery)
                ? scope
                : $"{scope} · name contains \"{StudentQuery.Trim()}\"";
        }
    }

    public string RowCountText => Rows.Count == 1 ? "1 student" : $"{Rows.Count} students";

    public ObservableCollection<ClassDto> Classes { get; } = new();
    public ObservableCollection<AssignmentChoice> Assignments { get; } = new();
    public ObservableCollection<AssignmentChoice> VisibleAssignments { get; } = new();
    public ObservableCollection<string> StudentClasses { get; } = new();
    public ObservableCollection<ReportRow> Rows { get; } = new();
    public ObservableCollection<ReportAssignment> ReportAssignments { get; } = new();

    public RelayCommand SelectAllCommand { get; }
    public RelayCommand ClearSelectionCommand { get; }
    public RelayCommand ToggleScopeCommand { get; }

    /// <summary>The chosen Classroom course, or the "All classes" option.</summary>
    public ClassDto? SelectedClass
    {
        get => _selectedClass;
        set
        {
            if (!SetProperty(ref _selectedClass, value))
            {
                return;
            }

            OnPropertyChanged(nameof(ScopeSummary));
            RefreshVisibleAssignments();
        }
    }

    /// <summary>The teacher-assigned class (grade group) to report on.</summary>
    public string StudentClass
    {
        get => _studentClass;
        set
        {
            var normalized = string.IsNullOrWhiteSpace(value) ? AllClasses : value;
            if (SetProperty(ref _studentClass, normalized))
            {
                OnPropertyChanged(nameof(ScopeSummary));
            }
        }
    }

    public string StudentQuery
    {
        get => _studentQuery;
        set
        {
            if (SetProperty(ref _studentQuery, value ?? string.Empty))
            {
                OnPropertyChanged(nameof(ScopeSummary));
            }
        }
    }

    public string Headline
    {
        get => _headline;
        private set => SetProperty(ref _headline, value);
    }

    /// <summary>Combined figures for the report the backend last produced.</summary>
    public string StatsLine
    {
        get => _statsLine;
        private set => SetProperty(ref _statsLine, value);
    }

    /// <summary>Raw report data, reachable only through technical details.</summary>
    public string RawText
    {
        get => _rawText;
        private set
        {
            if (SetProperty(ref _rawText, value))
            {
                OnPropertyChanged(nameof(HasRawText));
            }
        }
    }

    public bool HasRawText => !string.IsNullOrWhiteSpace(RawText);

    public string AssignmentCountText => Assignments.Count == 0
        ? "No assignments synced"
        : $"{Assignments.Count} assignments";

    public IReadOnlyList<string> SelectedIds => Assignments
        .Where(a => a.IsSelected)
        .Select(a => a.Id)
        .ToList();

    public string SelectionSummary
    {
        get
        {
            var chosen = SelectedIds.Count;
            var visible = VisibleAssignments.Count(a => a.IsSelected);
            var hidden = chosen - visible;
            var text = chosen == 0
                ? "No assignments selected"
                : chosen == 1 ? "1 assignment selected" : $"{chosen} assignments selected";
            // Selections from a class the filter now hides are still reported;
            // saying so prevents a "why is that assignment in my report?".
            return hidden > 0 ? $"{text} ({hidden} outside this class)" : text;
        }
    }

    public AsyncCommand BuildCommand => Command(async () =>
    {
        var ids = SelectedIds;
        Rows.Clear();
        ReportAssignments.Clear();
        RawText = string.Empty;
        Headline = string.Empty;
        StatsLine = string.Empty;

        if (ids.Count == 0)
        {
            Error = "Tick at least one assignment. Use Select all to include the whole class.";
            return;
        }

        var report = await Session.Api.MultiAssignmentReportAsync(
            ids, ClassLabelOrNull(), CourseOrNull(), QueryOrNull());
        Headline = HeadlineOf(report, ids.Count);
        StatsLine = StatsOf(report);
        FillAssignments(report);
        FillStudents(report);
        FillChosenAssignments(ids);
        RawText = Json.Pretty(report);
        // The page's own status line is where these figures belong: it is
        // visible without spending any of the report table's height, and it
        // stays one line so the header does not grow.
        Status = Rows.Count == 0
            ? "No student rows matched these filters."
            : StatsLine;

        // Fold the scope band away so the rows get the page.
        IsScopeExpanded = Rows.Count == 0;
    });

    public AsyncCommand ExportPdfCommand => Command(() => ExportAsync("pdf", "PDF report (*.pdf)|*.pdf", true));

    public AsyncCommand ExportCsvCommand => Command(() => ExportAsync("csv", "CSV (*.csv)|*.csv", false));

    private async Task ExportAsync(string format, string filter, bool openAfter)
    {
        var ids = SelectedIds;
        if (ids.Count == 0)
        {
            Error = "Tick at least one assignment before exporting.";
            return;
        }

        var file = await Session.Api.ExportMultiAssignmentReportAsync(
            ids, format, ClassLabelOrNull(), CourseOrNull(), QueryOrNull());
        var saved = DownloadSaver.Save(Session.Settings, Session.Dialogs, file, filter,
            "Save report", openAfter: openAfter);
        if (!string.IsNullOrEmpty(saved))
        {
            Session.Notify((format == "pdf" ? "Report saved: " : "CSV saved: ") + saved);
        }
    }

    protected override async Task LoadCoreAsync()
    {
        var classes = await Session.Api.ClassesAsync();
        var previousClass = SelectedClass?.Id;
        Classes.Clear();
        // A real Classroom course list, plus an explicit "everything" choice:
        // the report needs to span classes when a teacher picks papers from two
        // of them, so "All classes" must be selectable rather than implied.
        Classes.Add(new ClassDto { Id = string.Empty, Name = AllClasses });
        foreach (var item in classes.Classes)
        {
            Classes.Add(item);
        }

        var assignments = await Session.Api.AssignmentsAsync();
        _allAssignments.Clear();
        Assignments.Clear();
        foreach (var item in assignments.Assignments)
        {
            var course = classes.Classes.FirstOrDefault(c => c.Id == item.CourseId);
            var choice = new AssignmentChoice(item, course?.Title ?? item.CourseLabel);
            choice.SelectionChanged += OnChoiceChanged;
            _allAssignments.Add(choice);
            Assignments.Add(choice);
        }

        await LoadStudentClassesAsync();

        _selectedClass = Classes.FirstOrDefault(c => c.Id == previousClass) ?? Classes.FirstOrDefault();
        OnPropertyChanged(nameof(SelectedClass));
        RefreshVisibleAssignments();
    }

    private async Task LoadStudentClassesAsync()
    {
        try
        {
            var students = await Session.Api.StudentsAsync();
            _studentClasses.Clear();
            foreach (var label in students.Students
                         .Select(s => s.ClassText)
                         .Where(text => !string.IsNullOrWhiteSpace(text))
                         .Distinct()
                         .OrderBy(text => text, StringComparer.OrdinalIgnoreCase))
            {
                _studentClasses.Add(label);
            }
        }
        catch (Exception ex)
        {
            // A missing roster must not stop the report page: the class filter
            // simply offers the "all" option.
            Session.Log.Warn("student class filter unavailable: " + ex.Message);
        }

        // Reconciled in place for the same reason as the class list: a bound
        // ComboBox that is emptied loses the selection it is then told to show.
        SyncStrings(StudentClasses, new[] { AllClasses }.Concat(_studentClasses));

        StudentClass = StudentClasses.Contains(StudentClass) ? StudentClass : AllClasses;
        OnPropertyChanged(nameof(StudentClass));
    }

    /// <summary>Makes <paramref name="target"/> hold exactly <paramref name="wanted"/>
    /// without clearing it, so a ComboBox keeps its selected item.</summary>
    private static void SyncStrings(ObservableCollection<string> target, IEnumerable<string> wanted)
    {
        var list = wanted.ToList();
        for (var i = target.Count - 1; i >= 0; i--)
        {
            if (!list.Contains(target[i]))
            {
                target.RemoveAt(i);
            }
        }

        for (var i = 0; i < list.Count; i++)
        {
            if (i >= target.Count)
            {
                target.Add(list[i]);
            }
            else if (!string.Equals(target[i], list[i], StringComparison.Ordinal))
            {
                target.Insert(i, list[i]);
            }
        }
    }

    private void OnChoiceChanged(object? sender, EventArgs e)
    {
        OnPropertyChanged(nameof(SelectionSummary));
        OnPropertyChanged(nameof(ScopeSummary));
        SelectAllCommand.RaiseCanExecuteChanged();
        ClearSelectionCommand.RaiseCanExecuteChanged();
    }

    private void RefreshVisibleAssignments()
    {
        var courseId = CourseOrNull();
        VisibleAssignments.Clear();
        foreach (var choice in _allAssignments)
        {
            if (courseId is null || choice.CourseId == courseId)
            {
                VisibleAssignments.Add(choice);
            }
        }

        OnPropertyChanged(nameof(SelectionSummary));
        OnPropertyChanged(nameof(ScopeSummary));
        OnPropertyChanged(nameof(AssignmentCountText));
        SelectAllCommand.RaiseCanExecuteChanged();
    }

    private void SelectAll()
    {
        foreach (var choice in VisibleAssignments)
        {
            choice.IsSelected = true;
        }
    }

    private void ClearSelection()
    {
        foreach (var choice in Assignments)
        {
            choice.IsSelected = false;
        }
    }

    private string? CourseOrNull() =>
        SelectedClass is null || string.IsNullOrWhiteSpace(SelectedClass.Id) ? null : SelectedClass.Id;

    private string? ClassLabelOrNull() =>
        string.Equals(StudentClass, AllClasses, StringComparison.Ordinal) ? null : StudentClass;

    private string? QueryOrNull() =>
        string.IsNullOrWhiteSpace(StudentQuery) ? null : StudentQuery.Trim();

    private static string HeadlineOf(JsonElement report, int requested)
    {
        var title = "Combined report";
        if (report.TryGetProperty("class", out var cls) && cls.ValueKind == JsonValueKind.Object
            && cls.TryGetProperty("name", out var name))
        {
            var section = cls.TryGetProperty("section", out var s) && s.ValueKind == JsonValueKind.String
                ? s.GetString()
                : null;
            title = string.Join(" — ", new[] { name.GetString(), section }
                .Where(x => !string.IsNullOrWhiteSpace(x)));
        }
        else if (requested > 0)
        {
            title = $"{requested} assignments";
        }

        return title;
    }

    /// <summary>The report's own totals, written as a mark fraction.</summary>
    private static string StatsOf(JsonElement report)
    {
        if (!report.TryGetProperty("stats", out var stats) || stats.ValueKind != JsonValueKind.Object)
        {
            return string.Empty;
        }

        var parts = new List<string>();
        var students = Int(stats, "students");
        var corrected = Int(stats, "corrected");
        var missing = Int(stats, "missing");
        if (students is not null)
        {
            parts.Add(students == 1 ? "1 student" : $"{students} students");
        }

        if (corrected is not null)
        {
            parts.Add($"{corrected} corrected");
        }

        if (missing is > 0)
        {
            parts.Add($"{missing} not submitted");
        }

        var earned = Number(stats, "earned");
        var available = Number(stats, "available");
        var percentage = Number(stats, "percentage");
        if (earned is not null && available is > 0)
        {
            parts.Add($"Math Checker total {earned:0.#}/{available:0.#}"
                      + (percentage is null ? string.Empty : $" ({percentage:0.#}%)"));
        }

        var classroomPct = Number(stats, "classroom_percentage");
        if (classroomPct is not null)
        {
            parts.Add($"Classroom {classroomPct:0.#}%");
        }

        return string.Join(" · ", parts);
    }

    private void FillAssignments(JsonElement report)
    {
        if (!report.TryGetProperty("assignments", out var items) || items.ValueKind != JsonValueKind.Array)
        {
            return;
        }

        foreach (var item in items.EnumerateArray())
        {
            var meta = new List<string>();
            var course = Text(item, "course_name");
            if (!string.IsNullOrWhiteSpace(course))
            {
                var section = Text(item, "course_section");
                meta.Add(string.IsNullOrWhiteSpace(section) ? course! : $"{course} — {section}");
            }

            var points = Number(item, "max_points");
            if (points is not null)
            {
                meta.Add($"{points:0.#} marks");
            }

            ReportAssignments.Add(new ReportAssignment
            {
                Title = Text(item, "title") ?? "Untitled assignment",
                Meta = string.Join(" · ", meta),
                Stats = item.TryGetProperty("stats", out var stats)
                    && stats.ValueKind == JsonValueKind.Object
                        ? StatsOfAssignment(stats)
                        : string.Empty,
            });
        }
    }

    /// <summary>One assignment's own single-assignment statistics, which the
    /// backend nests under "math_checker" / "submission".</summary>
    private static string StatsOfAssignment(JsonElement stats)
    {
        var parts = new List<string>();
        if (stats.TryGetProperty("math_checker", out var mc) && mc.ValueKind == JsonValueKind.Object)
        {
            var corrected = Int(mc, "corrected");
            if (corrected is not null)
            {
                parts.Add($"{corrected} corrected");
            }

            var average = Number(mc, "average");
            if (average is not null)
            {
                parts.Add($"average {average:0.#}%");
            }
        }

        if (stats.TryGetProperty("submission", out var submission)
            && submission.ValueKind == JsonValueKind.Object)
        {
            var missing = Int(submission, "not_submitted");
            if (missing is > 0)
            {
                parts.Add($"{missing} not submitted");
            }
        }

        return parts.Count == 0 ? "no rows recorded" : string.Join(" · ", parts);
    }

    private void FillStudents(JsonElement report)
    {
        if (!report.TryGetProperty("students", out var rows) || rows.ValueKind != JsonValueKind.Array)
        {
            return;
        }

        foreach (var row in rows.EnumerateArray())
        {
            var earned = Number(row, "earned");
            var available = Number(row, "available");
            var percentage = Number(row, "percentage");

            var status = new List<string>();
            var corrected = Int(row, "corrected");
            var missingCount = Int(row, "missing");
            var incomplete = Int(row, "incomplete");
            var failed = Int(row, "failed");
            var zero = Int(row, "zero_marks");
            if (corrected is > 0)
            {
                status.Add($"{corrected} corrected");
            }

            if (missingCount is > 0)
            {
                status.Add($"{missingCount} not submitted");
            }

            if (incomplete is > 0)
            {
                status.Add($"{incomplete} incomplete");
            }

            if (failed is > 0)
            {
                status.Add($"{failed} failed");
            }

            if (zero is > 0)
            {
                status.Add($"{zero} scored 0");
            }

            Rows.Add(new ReportRow
            {
                Name = Text(row, "display_name") ?? Text(row, "student_name") ?? "—",
                ClassText = Text(row, "class_label") ?? "Not assigned",
                // A student with nothing corrected has no mark fraction to show;
                // zero marks and no marks must never look the same.
                Score = available is > 0 ? $"{earned:0.#} / {available:0.#}" : "—",
                Percentage = percentage is null ? "—" : $"{percentage:0.#}%",
                Classroom = ClassroomText(row),
                Breakdown = BreakdownOf(row),
                Note = string.Join(" · ", status),
            });
        }
    }

    private static string ClassroomText(JsonElement row)
    {
        var earned = Number(row, "classroom_earned");
        var available = Number(row, "classroom_available");
        var percentage = Number(row, "classroom_percentage");
        if (available is > 0)
        {
            return $"{earned:0.#} / {available:0.#}"
                   + (percentage is null ? string.Empty : $" ({percentage:0.#}%)");
        }

        return Int(row, "graded_in_classroom") is > 0 ? "Graded, score not recorded" : "—";
    }

    private static string BreakdownOf(JsonElement row)
    {
        if (!row.TryGetProperty("by_assignment", out var cells) || cells.ValueKind != JsonValueKind.Array)
        {
            return string.Empty;
        }

        var parts = new List<string>();
        foreach (var cell in cells.EnumerateArray())
        {
            var earned = Number(cell, "mc_earned");
            var available = Number(cell, "mc_available");
            var mcStatus = Text(cell, "mc_status") ?? string.Empty;
            var status = Text(cell, "status") ?? string.Empty;
            string value;
            if (available is > 0)
            {
                value = $"{earned:0.#}/{available:0.#}";
            }
            else if (mcStatus == "INCOMPLETE")
            {
                value = "incomplete";
            }
            else if (mcStatus == "FAILED")
            {
                value = "failed";
            }
            else if (status is "MISSING" or "NOT_STARTED" or "CREATED" or "RECLAIMED")
            {
                value = "missing";
            }
            else
            {
                // Handed in but not marked yet: say so instead of implying a mark.
                value = "submitted";
            }

            parts.Add(value);
        }

        return string.Join("  ·  ", parts);
    }

    /// <summary>Puts the chosen assignment titles in the report header area even
    /// when the backend returned no per-assignment block (e.g. no rows yet).</summary>
    private void FillChosenAssignments(IReadOnlyList<string> ids)
    {
        if (ReportAssignments.Count > 0)
        {
            return;
        }

        foreach (var id in ids)
        {
            var choice = _allAssignments.FirstOrDefault(a => a.Id == id);
            if (choice is null)
            {
                continue;
            }

            ReportAssignments.Add(new ReportAssignment
            {
                Title = choice.DisplayTitle,
                Meta = choice.MetaText,
                Stats = "no rows recorded",
            });
        }
    }

    private static string? Text(JsonElement element, string property)
    {
        if (!element.TryGetProperty(property, out var value))
        {
            return null;
        }

        return value.ValueKind switch
        {
            JsonValueKind.Null or JsonValueKind.Undefined => null,
            JsonValueKind.String => value.GetString(),
            _ => value.ToString(),
        };
    }

    private static double? Number(JsonElement element, string property)
    {
        if (!element.TryGetProperty(property, out var value))
        {
            return null;
        }

        return value.ValueKind switch
        {
            JsonValueKind.Number when value.TryGetDouble(out var number) => number,
            JsonValueKind.String when double.TryParse(value.GetString(), NumberStyles.Any,
                CultureInfo.InvariantCulture, out var parsed) => parsed,
            _ => null,
        };
    }

    private static int? Int(JsonElement element, string property)
    {
        var value = Number(element, property);
        return value is null ? null : (int)Math.Round(value.Value);
    }
}
