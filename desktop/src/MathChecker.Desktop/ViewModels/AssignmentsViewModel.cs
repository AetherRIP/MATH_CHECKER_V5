using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class AssignmentsViewModel : PageViewModel
{
    private AssignmentDto? _selected;
    private string _filter = string.Empty;
    private string _kind = "all";
    private string _newTitle = string.Empty;
    private string _newPoints = string.Empty;
    private string _newDueDate = string.Empty;
    private string _markSchemeNote = string.Empty;
    private string _markSchemeContent = string.Empty;
    private string _markSchemeRaw = string.Empty;
    private string _summary = string.Empty;
    private readonly List<AssignmentDto> _all = new();

    public AssignmentsViewModel(AppSession session)
        : base(session, "assignments", "Assignments")
    {
        // An assignment is a link, not just a row: the name column is a button
        // and the toolbar action opens the selected one. Both go through the
        // same method, so there is one way into the details page.
        OpenAssignmentCommand = new RelayCommand(parameter =>
            OpenDetails(parameter as AssignmentDto ?? Selected));
        OpenSelectedCommand = new RelayCommand(() => OpenDetails(Selected),
            () => HasSelection);
    }

    /// <summary>Opens the clicked row's details page.</summary>
    public RelayCommand OpenAssignmentCommand { get; }

    /// <summary>Opens the selected row's details page.</summary>
    public RelayCommand OpenSelectedCommand { get; }

    /// <summary>Classroom coursework is mirrored here, so this page syncs it.</summary>
    public override bool SyncsFromClassroom => true;

    public ObservableCollection<AssignmentDto> Items { get; } = new();
    public ObservableCollection<ChoiceOption> Kinds { get; } = new()
    {
        new("All kinds", "all"),
        new("Managed", "managed"),
        new("External", "external"),
        new("Local", "local"),
    };

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

    public string Kind
    {
        get => _kind;
        set
        {
            if (SetProperty(ref _kind, value))
            {
                ApplyFilter();
            }
        }
    }

    public AssignmentDto? Selected
    {
        get => _selected;
        set
        {
            if (!SetProperty(ref _selected, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasSelection));
            OpenSelectedCommand.RaiseCanExecuteChanged();
            if (value is not null)
            {
                _ = LoadMarkSchemeAsync(value.Id);
                return;
            }

            MarkSchemeFacts.Clear();
            MarkSchemeContent = string.Empty;
            MarkSchemeRaw = string.Empty;
            MarkSchemeNote = string.Empty;
            OnPropertyChanged(nameof(HasMarkSchemeFacts));
        }
    }

    /// <summary>Recheck and the page-image action both act on the selected
    /// assignment only; without one they would silently do nothing.</summary>
    public bool HasSelection => _selected is not null;

    public string NewTitle
    {
        get => _newTitle;
        set => SetProperty(ref _newTitle, value);
    }

    public string NewPoints
    {
        get => _newPoints;
        set => SetProperty(ref _newPoints, value);
    }

    public string NewDueDate
    {
        get => _newDueDate;
        set => SetProperty(ref _newDueDate, value);
    }

    /// <summary>The mark scheme read as a sentence, labelled facts and (when
    /// the backend has one) the scheme text itself. The raw payload is kept for
    /// the technical-details expander only.</summary>
    public ObservableCollection<FactRow> MarkSchemeFacts { get; } = new();

    public string MarkSchemeNote
    {
        get => _markSchemeNote;
        private set => SetProperty(ref _markSchemeNote, value);
    }

    public string MarkSchemeContent
    {
        get => _markSchemeContent;
        private set
        {
            if (SetProperty(ref _markSchemeContent, value))
            {
                OnPropertyChanged(nameof(HasMarkSchemeContent));
            }
        }
    }

    public string MarkSchemeRaw
    {
        get => _markSchemeRaw;
        private set
        {
            if (SetProperty(ref _markSchemeRaw, value))
            {
                OnPropertyChanged(nameof(HasMarkSchemeRaw));
            }
        }
    }

    public bool HasMarkSchemeFacts => MarkSchemeFacts.Count > 0;

    public bool HasMarkSchemeContent => !string.IsNullOrWhiteSpace(MarkSchemeContent);

    public bool HasMarkSchemeRaw => !string.IsNullOrWhiteSpace(MarkSchemeRaw);

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    public AsyncCommand CreateLocalCommand => Command(async () =>
    {
        if (string.IsNullOrWhiteSpace(NewTitle))
        {
            Error = "A title is required for a local assignment.";
            return;
        }

        double? points = null;
        if (!string.IsNullOrWhiteSpace(NewPoints))
        {
            if (!double.TryParse(NewPoints, out var parsed) || parsed <= 0)
            {
                Error = "Maximum points must be a positive number.";
                return;
            }

            points = parsed;
        }

        await Session.Api.CreateLocalAssignmentAsync(NewTitle.Trim(), points,
            string.IsNullOrWhiteSpace(NewDueDate) ? null : NewDueDate.Trim(), null);
        NewTitle = string.Empty;
        NewPoints = string.Empty;
        NewDueDate = string.Empty;
        await LoadAsync();
        Status = "Local assignment created.";
    }, success: null);

    /// <summary>Loads the details page for one assignment. The page reads the
    /// identity it was opened with, so this cannot show another assignment.
    /// </summary>
    public void OpenDetails(AssignmentDto? assignment)
    {
        if (assignment is null || string.IsNullOrWhiteSpace(assignment.Id))
        {
            return;
        }

        Session.OpenAssignment(assignment.Id);
    }

    public AsyncCommand RecheckMarkSchemeCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        await Session.Api.RecheckMarkSchemeAsync(Selected.Id);
        await LoadMarkSchemeAsync(Selected.Id);
        await LoadAsync();
        Status = "Mark scheme rechecked by the backend.";
    });

    public AsyncCommand OpenMarkSchemePageCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        var pages = await Session.Api.MarkSchemePagesAsync(Selected.Id);
        var first = 1;
        if (pages.TryGetProperty("pages", out var list) && list.ValueKind == JsonValueKind.Array
            && list.GetArrayLength() > 0 && list[0].TryGetProperty("page", out var pageNumber))
        {
            first = pageNumber.GetInt32();
        }

        var file = await Session.Api.MarkSchemePageImageAsync(Selected.Id, first);
        var saved = DownloadSaver.Save(Session.Settings, Session.Dialogs, file,
            "PNG image (*.png)|*.png", "Save mark-scheme page", openAfter: true);
        if (!string.IsNullOrEmpty(saved))
        {
            Session.Notify("Mark-scheme page saved: " + saved);
        }
    });

    protected override async Task LoadCoreAsync()
    {
        var assignments = await Session.Api.AssignmentsAsync();
        _all.Clear();
        _all.AddRange(assignments.Assignments);
        ApplyFilter();
        Summary = $"{assignments.Total} assignments · "
                  + $"{_all.Count(a => a.HasMarkScheme)} with a mark scheme";
    }

    private void ApplyFilter()
    {
        var needle = Filter.Trim();
        Items.Clear();
        foreach (var assignment in _all)
        {
            if (Kind != "all")
            {
                var matchesKind = Kind switch
                {
                    "managed" => assignment.Managed,
                    "local" => assignment.Local,
                    "external" => !assignment.Managed && !assignment.Local,
                    _ => true,
                };
                if (!matchesKind)
                {
                    continue;
                }
            }

            if (needle.Length > 0
                && assignment.DisplayTitle.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0
                && assignment.CourseLabel.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
            {
                continue;
            }

            Items.Add(assignment);
        }
    }

    private async Task LoadMarkSchemeAsync(string courseworkId)
    {
        MarkSchemeFacts.Clear();
        MarkSchemeContent = string.Empty;
        MarkSchemeRaw = string.Empty;
        MarkSchemeNote = "Reading the mark scheme…";
        OnPropertyChanged(nameof(HasMarkSchemeFacts));
        try
        {
            var payload = await Session.Api.MarkSchemeAsync(courseworkId);
            MarkSchemeRaw = PayloadFacts.Raw(payload);
            if (payload.ValueKind == JsonValueKind.Object
                && payload.TryGetProperty("text", out var text)
                && text.ValueKind == JsonValueKind.String)
            {
                MarkSchemeContent = text.GetString() ?? string.Empty;
            }

            // Only the facts a teacher acts on. Validation internals (numbering,
            // gaps, duplicates, anomalies) stay in technical details instead of
            // burying the page under two dozen raw field names.
            var hasScheme = ReadBool(payload, "has_mark_scheme");
            var questions = ReadInt(payload, "validation", "question_count");
            var marks = ReadNumber(payload, "validation", "total_marks");
            var subquestions = ReadInt(payload, "validation", "subquestion_count");
            var warnings = ReadString(payload, "warnings");

            if (hasScheme || !string.IsNullOrWhiteSpace(MarkSchemeContent))
            {
                MarkSchemeFacts.Add(new FactRow("Questions", questions.ToString()));
                if (marks is not null)
                {
                    MarkSchemeFacts.Add(new FactRow("Marks available", $"{marks:0.##}"));
                }

                if (subquestions > 0)
                {
                    MarkSchemeFacts.Add(new FactRow("Subquestions", subquestions.ToString()));
                }

                if (!string.IsNullOrWhiteSpace(warnings))
                {
                    MarkSchemeFacts.Add(new FactRow("Warnings", warnings!));
                }

                if (ReadBool(payload, "manual_override"))
                {
                    MarkSchemeFacts.Add(new FactRow("Manual override", "On"));
                }
            }

            MarkSchemeNote = DescribeMarkScheme(payload, MarkSchemeContent);
            OnPropertyChanged(nameof(HasMarkSchemeFacts));
        }
        catch (ApiException ex)
        {
            MarkSchemeNote = ex.Display;
            MarkSchemeRaw = ex.Technical;
        }
        catch (Exception ex)
        {
            MarkSchemeNote = "The mark scheme could not be read: " + ex.Message;
        }
    }

    /// <summary>One sentence that says what the state means rather than
    /// repeating the raw boolean flags.</summary>
    private static string DescribeMarkScheme(JsonElement payload, string content)
    {
        var hasScheme = ReadBool(payload, "has_mark_scheme");
        var processed = ReadBool(payload, "processed");
        var questions = ReadInt(payload, "validation", "question_count");

        if (!hasScheme && string.IsNullOrWhiteSpace(content))
        {
            return "No mark scheme is attached to this assignment yet. "
                   + "Upload one from Classroom → Assignments, then recheck it here.";
        }

        if (!processed && questions == 0)
        {
            return "A mark scheme is attached but has not been processed yet. Run “Recheck mark scheme”.";
        }

        if (questions > 0)
        {
            return "The mark scheme is processed and ready for correction.";
        }

        return string.IsNullOrWhiteSpace(content)
            ? "The mark scheme is attached to this assignment."
            : "The mark scheme text attached to this assignment is shown below.";
    }

    private static bool ReadBool(JsonElement payload, string property) =>
        payload.ValueKind == JsonValueKind.Object
        && payload.TryGetProperty(property, out var value)
        && value.ValueKind == JsonValueKind.True;

    private static string? ReadString(JsonElement payload, string property) =>
        payload.ValueKind == JsonValueKind.Object
        && payload.TryGetProperty(property, out var value)
        && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;

    private static int ReadInt(JsonElement payload, string parent, string property) =>
        payload.ValueKind == JsonValueKind.Object
        && payload.TryGetProperty(parent, out var section)
        && section.ValueKind == JsonValueKind.Object
        && section.TryGetProperty(property, out var value)
        && value.TryGetInt32(out var number)
            ? number
            : 0;

    private static double? ReadNumber(JsonElement payload, string parent, string property) =>
        payload.ValueKind == JsonValueKind.Object
        && payload.TryGetProperty(parent, out var section)
        && section.ValueKind == JsonValueKind.Object
        && section.TryGetProperty(property, out var value)
        && value.TryGetDouble(out var number)
            ? number
            : null;
}
