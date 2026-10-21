using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One row of a rendered report, mapped from the backend's own fields.</summary>
public sealed class ReportRow
{
    public string Name { get; init; } = string.Empty;
    public string Status { get; init; } = string.Empty;
    public string Score { get; init; } = string.Empty;
    public string Percentage { get; init; } = string.Empty;
    public string Note { get; init; } = string.Empty;
}

public sealed class ReportsViewModel : PageViewModel
{
    private ClassDto? _selectedClass;
    private AssignmentDto? _selectedAssignment;
    private string _mode = "Class report";
    private string _headline = string.Empty;
    private string _rawText = string.Empty;

    public ReportsViewModel(AppSession session)
        : base(session, "reports", "Reports")
    {
    }

    public ObservableCollection<ClassDto> Classes { get; } = new();
    public ObservableCollection<AssignmentDto> Assignments { get; } = new();
    public ObservableCollection<ReportRow> Rows { get; } = new();
    public ObservableCollection<string> Modes { get; } = new() { "Class report", "Assignment report" };

    public string Mode
    {
        get => _mode;
        set => SetProperty(ref _mode, value);
    }

    public ClassDto? SelectedClass
    {
        get => _selectedClass;
        set => SetProperty(ref _selectedClass, value);
    }

    public AssignmentDto? SelectedAssignment
    {
        get => _selectedAssignment;
        set => SetProperty(ref _selectedAssignment, value);
    }

    public string Headline
    {
        get => _headline;
        private set => SetProperty(ref _headline, value);
    }

    public string RawText
    {
        get => _rawText;
        private set => SetProperty(ref _rawText, value);
    }

    public AsyncCommand BuildCommand => Command(async () =>
    {
        Rows.Clear();
        RawText = string.Empty;
        Headline = string.Empty;

        if (Mode == "Class report")
        {
            if (SelectedClass is null)
            {
                Error = "Choose a class.";
                return;
            }

            var report = await Session.Api.ClassReportAsync(SelectedClass.Id);
            Headline = HeadlineOf(report, "Class report");
            FillFrom(report, "students");
            RawText = Json.Pretty(report);
        }
        else
        {
            if (SelectedAssignment is null)
            {
                Error = "Choose an assignment.";
                return;
            }

            var report = await Session.Api.AssignmentReportAsync(SelectedAssignment.Id);
            Headline = HeadlineOf(report, "Assignment report");
            FillFrom(report, "rows");
            RawText = Json.Pretty(report);
        }
    });

    public AsyncCommand ExportPdfCommand => Command(async () =>
    {
        var file = Mode == "Class report"
            ? SelectedClass is null ? null : await Session.Api.ExportClassReportAsync(SelectedClass.Id, "pdf")
            : SelectedAssignment is null ? null : await Session.Api.ExportAssignmentReportAsync(SelectedAssignment.Id, "pdf");
        if (file is null)
        {
            Error = "Choose the class or assignment first.";
            return;
        }

        var saved = DownloadSaver.Save(Session.Settings, Session.Dialogs, file,
            "PDF report (*.pdf)|*.pdf", "Save report", openAfter: true);
        if (!string.IsNullOrEmpty(saved))
        {
            Session.Notify("Report saved: " + saved);
        }
    });

    public AsyncCommand ExportCsvCommand => Command(async () =>
    {
        var file = Mode == "Class report"
            ? SelectedClass is null ? null : await Session.Api.ExportClassReportAsync(SelectedClass.Id, "csv")
            : SelectedAssignment is null ? null : await Session.Api.ExportAssignmentReportAsync(SelectedAssignment.Id, "csv");
        if (file is null)
        {
            Error = "Choose the class or assignment first.";
            return;
        }

        var saved = DownloadSaver.Save(Session.Settings, Session.Dialogs, file,
            "CSV (*.csv)|*.csv", "Save report", openAfter: false);
        if (!string.IsNullOrEmpty(saved))
        {
            Session.Notify("CSV saved: " + saved);
        }
    });

    protected override async Task LoadCoreAsync()
    {
        var classes = await Session.Api.ClassesAsync();
        Classes.Clear();
        foreach (var item in classes.Classes)
        {
            Classes.Add(item);
        }

        var assignments = await Session.Api.AssignmentsAsync();
        Assignments.Clear();
        foreach (var item in assignments.Assignments)
        {
            Assignments.Add(item);
        }

        SelectedClass ??= Classes.FirstOrDefault();
        SelectedAssignment ??= Assignments.FirstOrDefault();
    }

    private static string HeadlineOf(JsonElement report, string fallback)
    {
        var title = fallback;
        if (report.TryGetProperty("class", out var cls) && cls.ValueKind == JsonValueKind.Object)
        {
            var name = cls.TryGetProperty("name", out var n) ? n.GetString() : null;
            var section = cls.TryGetProperty("section", out var s) ? s.GetString() : null;
            title = string.Join(" — ", new[] { name, section }.Where(x => !string.IsNullOrWhiteSpace(x)));
        }

        if (report.TryGetProperty("assignment", out var assignment) && assignment.ValueKind == JsonValueKind.Object
            && assignment.TryGetProperty("title", out var assignmentTitle))
        {
            title = $"{assignmentTitle.GetString()} · {title}";
        }

        if (report.TryGetProperty("stats", out var stats) && stats.ValueKind == JsonValueKind.Object)
        {
            var total = stats.TryGetProperty("total_students", out var t) ? t.ToString() : "?";
            return $"{fallback}: {title} · {total} students";
        }

        return $"{fallback}: {title}";
    }

    private void FillFrom(JsonElement report, string arrayName)
    {
        if (!report.TryGetProperty(arrayName, out var rows) || rows.ValueKind != JsonValueKind.Array)
        {
            return;
        }

        foreach (var row in rows.EnumerateArray())
        {
            var name = Text(row, "display_name") ?? Text(row, "student_name") ?? "—";
            var status = Text(row, "status_label") ?? Text(row, "status") ?? string.Empty;

            string score;
            string percentage;
            if (row.TryGetProperty("mc_earned", out _) || row.TryGetProperty("mc_available", out _))
            {
                score = $"{Text(row, "mc_earned") ?? "—"} / {Text(row, "mc_available") ?? "—"}";
                percentage = FormatPercent(row, "mc_percentage");
            }
            else
            {
                score = $"{Text(row, "mc_total_earned") ?? "—"} / {Text(row, "mc_total_available") ?? "—"}";
                percentage = FormatPercent(row, "mc_average");
                status = $"{Text(row, "corrected") ?? "0"} corrected of {Text(row, "assignments") ?? "0"}";
            }

            var note = Text(row, "job_error") ?? Text(row, "class_label")
                       ?? Text(row, "data_quality") ?? string.Empty;

            Rows.Add(new ReportRow
            {
                Name = name,
                Status = status,
                Score = score,
                Percentage = percentage,
                Note = note,
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

    private static string FormatPercent(JsonElement element, string property) =>
        element.TryGetProperty(property, out var value) && value.ValueKind == JsonValueKind.Number
            ? $"{value.GetDouble():0.#}%"
            : "—";
}
