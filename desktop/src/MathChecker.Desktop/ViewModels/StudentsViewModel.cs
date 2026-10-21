using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class StudentsViewModel : PageViewModel
{
    private const string AllClasses = "All classes";

    private StudentDto? _selected;
    private string _filter = string.Empty;
    private string _classFilter = AllClasses;
    private string _profileText = string.Empty;
    private string _summary = string.Empty;
    private readonly List<StudentDto> _all = new();

    public StudentsViewModel(AppSession session)
        : base(session, "students", "Students")
    {
    }

    public ObservableCollection<StudentDto> Items { get; } = new();
    public ObservableCollection<string> ClassFilters { get; } = new() { AllClasses };

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

    public string ClassFilter
    {
        get => _classFilter;
        set
        {
            // Rebuilding ClassFilters empties the ComboBox, and WPF writes null
            // back through the two-way binding. Treat that as "no class filter"
            // instead of a filter that matches nothing.
            var normalized = string.IsNullOrWhiteSpace(value) ? AllClasses : value;
            if (SetProperty(ref _classFilter, normalized))
            {
                ApplyFilter();
            }
        }
    }

    public StudentDto? Selected
    {
        get => _selected;
        set
        {
            if (SetProperty(ref _selected, value) && value is not null)
            {
                _ = LoadProfileAsync(value.StudentId);
            }
        }
    }

    public string ProfileText
    {
        get => _profileText;
        private set => SetProperty(ref _profileText, value);
    }

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    public AsyncCommand EditDetailsCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        var nickname = Session.Dialogs.AskText("Nickname",
            $"Display nickname for {Selected.Name} (leave empty to clear):", Selected.Nickname ?? string.Empty);
        if (nickname is null)
        {
            return;
        }

        var className = Session.Dialogs.AskText("Class",
            "Class label used on reports (leave empty to clear):", Selected.ClassName ?? string.Empty);
        if (className is null)
        {
            return;
        }

        await Session.Api.SaveStudentDetailsAsync(Selected.StudentId,
            string.IsNullOrWhiteSpace(nickname) ? null : nickname,
            string.IsNullOrWhiteSpace(className) ? null : className);
        await LoadAsync();
    });

    public AsyncCommand OpenReportCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        var file = await Session.Api.StudentReportAsync(Selected.StudentId);
        var saved = DownloadSaver.Save(Session.Settings, Session.Dialogs, file,
            "PDF report (*.pdf)|*.pdf", "Save student report", openAfter: true);
        if (!string.IsNullOrEmpty(saved))
        {
            Session.Notify("Student report saved: " + saved);
        }
    });

    public AsyncCommand OpenResultsCommand => new(() =>
    {
        Session.GoTo("results");
        return Task.CompletedTask;
    });

    protected override async Task LoadCoreAsync()
    {
        var students = await Session.Api.StudentsAsync();
        _all.Clear();
        _all.AddRange(students.Students);

        var previousClassFilter = ClassFilter;
        ClassFilters.Clear();
        ClassFilters.Add(AllClasses);
        foreach (var label in _all.Select(s => s.ClassLabel ?? "Not assigned").Distinct().OrderBy(x => x))
        {
            ClassFilters.Add(label);
        }

        // Restore the selection explicitly: clearing the list left the ComboBox
        // with no selection, which used to hide every student.
        ClassFilter = ClassFilters.Contains(previousClassFilter) ? previousClassFilter : AllClasses;
        ApplyFilter();
        Summary = $"{_all.Count} students known from Classroom, local corrections and results.";
    }

    private void ApplyFilter()
    {
        var needle = Filter.Trim();
        Items.Clear();
        foreach (var student in _all)
        {
            if (!string.Equals(ClassFilter, AllClasses, StringComparison.Ordinal)
                && (student.ClassLabel ?? "Not assigned") != ClassFilter)
            {
                continue;
            }

            if (needle.Length > 0
                && (student.Name.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
                && (student.StudentId.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
                && (student.Email ?? string.Empty).IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
            {
                continue;
            }

            Items.Add(student);
        }
    }

    private async Task LoadProfileAsync(string studentId)
    {
        try
        {
            var profile = await Session.Api.StudentAsync(studentId);
            ProfileText = Json.Pretty(profile);
        }
        catch (Exception ex)
        {
            ProfileText = "Could not load the profile: " + ex.Message;
        }
    }
}
