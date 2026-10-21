using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
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
    private string _markSchemeText = "Select an assignment to read its mark scheme.";
    private string _summary = string.Empty;
    private readonly List<AssignmentDto> _all = new();

    public AssignmentsViewModel(AppSession session)
        : base(session, "assignments", "Assignments")
    {
    }

    public ObservableCollection<AssignmentDto> Items { get; } = new();
    public ObservableCollection<string> Kinds { get; } = new() { "all", "managed", "external", "local" };

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
            if (SetProperty(ref _selected, value) && value is not null)
            {
                _ = LoadMarkSchemeAsync(value.Id);
            }
        }
    }

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

    public string MarkSchemeText
    {
        get => _markSchemeText;
        private set => SetProperty(ref _markSchemeText, value);
    }

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

    public AsyncCommand RecheckMarkSchemeCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        var payload = await Session.Api.RecheckMarkSchemeAsync(Selected.Id);
        MarkSchemeText = Json.Pretty(payload);
        await LoadAsync();
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
        try
        {
            var payload = await Session.Api.MarkSchemeAsync(courseworkId);
            if (payload.ValueKind == JsonValueKind.Object
                && payload.TryGetProperty("text", out var text)
                && text.ValueKind == JsonValueKind.String)
            {
                MarkSchemeText = text.GetString() ?? "(empty mark scheme)";
            }
            else
            {
                MarkSchemeText = Json.Pretty(payload);
            }
        }
        catch (ApiException ex)
        {
            MarkSchemeText = ex.Display;
        }
        catch (Exception ex)
        {
            MarkSchemeText = ex.Message;
        }
    }
}
