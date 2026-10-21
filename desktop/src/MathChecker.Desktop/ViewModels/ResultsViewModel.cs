using System.Collections.ObjectModel;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class ResultsViewModel : PageViewModel
{
    private readonly List<string> _statusOptions = new() { "all", "needs_review", "corrected", "overridden" };

    private string _query = string.Empty;
    private string _statusFilter = "all";
    private ResultSummaryDto? _selected;
    private ResultDetailDto? _detail;
    private QuestionMarkDto? _selectedQuestion;
    private string _detailNote = "Select a correction to see its marks and feedback.";
    private string _summary = string.Empty;
    private bool _loadingDetail;

    public ResultsViewModel(AppSession session)
        : base(session, "results", "Results")
    {
    }

    public ObservableCollection<ResultSummaryDto> Items { get; } = new();
    public ObservableCollection<QuestionMarkDto> Questions { get; } = new();
    public ObservableCollection<VersionEntryDto> Versions { get; } = new();
    public ObservableCollection<string> StatusOptions => new(_statusOptions);

    public string Query
    {
        get => _query;
        set => SetProperty(ref _query, value);
    }

    public string StatusFilter
    {
        get => _statusFilter;
        set
        {
            if (SetProperty(ref _statusFilter, value))
            {
                _ = LoadAsync();
            }
        }
    }

    public ResultSummaryDto? Selected
    {
        get => _selected;
        set
        {
            if (SetProperty(ref _selected, value) && value is not null)
            {
                _ = LoadDetailAsync(value.Id);
            }
        }
    }

    public ResultDetailDto? Detail
    {
        get => _detail;
        private set
        {
            if (SetProperty(ref _detail, value))
            {
                OnPropertyChanged(nameof(HasDetail));
                OnPropertyChanged(nameof(DetailHeader));
            }
        }
    }

    public bool HasDetail => Detail is not null;

    public string DetailHeader => Detail is null
        ? string.Empty
        : $"{Detail.Name} · {Detail.ScoreText} ({Detail.PercentageText}) · grade {Detail.ProvisionalGrade ?? "—"}\n"
          + $"{Detail.ClassLabel ?? "Not assigned"} · confidence {Detail.Confidence ?? "—"} · "
          + $"{(Detail.ManualOverride == 1 ? "teacher override" : "engine result")} · {Detail.CreatedText}";

    public QuestionMarkDto? SelectedQuestion
    {
        get => _selectedQuestion;
        set => SetProperty(ref _selectedQuestion, value);
    }

    public string DetailNote
    {
        get => _detailNote;
        private set => SetProperty(ref _detailNote, value);
    }

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    public AsyncCommand SearchCommand => Command(LoadAsync);

    /// <summary>Opens the selected correction in the paper viewer. The mark
    /// editing controls on this page go away once the desk has its panels.</summary>
    public RelayCommand OpenDeskCommand => new(() =>
    {
        if (Detail is not null)
        {
            Session.OpenResult(Detail.Id);
        }
    });

    public AsyncCommand ExportPdfCommand => Command(() => ExportAsync("pdf"));
    public AsyncCommand ExportCsvCommand => Command(() => ExportAsync("csv"));
    public AsyncCommand ExportMarkdownCommand => Command(() => ExportAsync("md"));

    public AsyncCommand RemarkCommand => Command(async () =>
    {
        if (Detail is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Re-mark with AI",
                "Re-run the AI marker for this submission? A new attempt is stored; the existing result "
                + "is kept in history and is not overwritten until you decide."))
        {
            return;
        }

        var started = await Session.Api.RemarkAsync(Detail.Id);
        Status = started.Duplicate
            ? $"A re-mark is already running (job {started.JobId})."
            : $"Re-mark started (job {started.JobId}).";
        await LoadDetailAsync(Detail.Id);
    });

    public AsyncCommand AcceptRemarkCommand => Command(async () =>
    {
        if (Detail is null)
        {
            return;
        }

        var marked = await Session.Api.RemarkStatusAsync(Detail.Id);
        if (marked.PendingVersion is null)
        {
            Error = "There is no new attempt waiting for a decision.";
            return;
        }

        await Session.Api.RemarkDecisionAsync(Detail.Id, "USE_NEW", marked.PendingVersion);
        await LoadDetailAsync(Detail.Id);
        Status = "New attempt promoted.";
    });

    public AsyncCommand RestoreLatestVersionCommand => Command(async () =>
    {
        if (Detail is null || Versions.Count == 0)
        {
            return;
        }

        var target = Versions.OrderByDescending(v => v.Version).First();
        if (!Session.Dialogs.Confirm("Restore attempt",
                $"Make attempt {target.Version} the active correction again?"))
        {
            return;
        }

        await Session.Api.RestoreVersionAsync(Detail.Id, target.Version);
        await LoadDetailAsync(Detail.Id);
        Status = $"Attempt {target.Version} restored.";
    });

    public AsyncCommand OverrideCommand => Command(async () =>
    {
        if (Detail is null)
        {
            return;
        }

        var entered = Session.Dialogs.AskText("Override the mark",
            $"Enter the whole-paper mark out of {Detail.Available?.ToString("0") ?? "?"}:",
            Detail.Earned?.ToString("0") ?? string.Empty);
        if (entered is null)
        {
            return;
        }

        // The backend casts the mark to an integer; 12.5 would be a 400.
        if (!int.TryParse(entered, out var mark))
        {
            Error = "The mark must be a whole number.";
            return;
        }

        await Session.Api.OverrideResultAsync(Detail.Id, mark, "Overridden from the desktop result page");
        await LoadDetailAsync(Detail.Id);
        Status = "Mark overridden; the result is now a teacher decision.";
    });

    public AsyncCommand SetQuestionMarkCommand => Command(async () =>
    {
        if (Detail is null || SelectedQuestion is null)
        {
            return;
        }

        var max = SelectedQuestion.MaxValue;
        var entered = Session.Dialogs.AskText("Question mark",
            $"Mark for question {SelectedQuestion.NumberText} (max {max?.ToString() ?? "?"}):",
            SelectedQuestion.AwardedValue?.ToString() ?? string.Empty);
        if (entered is null)
        {
            return;
        }

        if (!int.TryParse(entered, out var mark))
        {
            Error = "The mark must be a whole number.";
            return;
        }

        if (max is { } limit && (mark < 0 || mark > limit))
        {
            Error = $"The mark must be between 0 and {limit}.";
            return;
        }

        await Session.Api.SaveQuestionMarkAsync(Detail.Id, SelectedQuestion.NumberText, mark,
            "Set from the desktop result page");
        await LoadDetailAsync(Detail.Id);
        Status = "Question mark saved.";
    });

    public void SelectById(string resultId)
    {
        var match = Items.FirstOrDefault(r => r.Id == resultId);
        if (match is not null)
        {
            Selected = match;
        }
    }

    private async Task ExportAsync(string format)
    {
        if (Detail is null)
        {
            return;
        }

        var file = await Session.Api.ExportResultAsync(Detail.Id, format);
        var filter = format switch
        {
            "pdf" => "PDF (*.pdf)|*.pdf",
            "csv" => "CSV (*.csv)|*.csv",
            _ => "Markdown (*.md)|*.md",
        };
        var saved = DownloadSaver.Save(Session.Settings, Session.Dialogs, file, filter,
            "Save correction report", openAfter: format == "pdf");
        if (!string.IsNullOrEmpty(saved))
        {
            Session.Notify($"{format.ToUpperInvariant()} report saved: {saved}");
        }
    }

    protected override async Task LoadCoreAsync()
    {
        var results = await Session.Api.ResultsAsync(
            string.IsNullOrWhiteSpace(Query) ? null : Query,
            StatusFilter == "all" ? null : StatusFilter, limit: 300);

        var previous = Selected?.Id;
        Items.Clear();
        foreach (var result in results.Results)
        {
            Items.Add(result);
        }

        Summary = $"{results.Total} corrections stored · showing {Items.Count}"
                  + (results.Total > Items.Count ? " (raise the filter or search to narrow it)" : string.Empty);

        if (previous is not null)
        {
            Selected = Items.FirstOrDefault(r => r.Id == previous) ?? Items.FirstOrDefault();
        }
    }

    private async Task LoadDetailAsync(string resultId)
    {
        if (_loadingDetail)
        {
            return;
        }

        _loadingDetail = true;
        try
        {
            Detail = await Session.Api.ResultAsync(resultId);
            Questions.Clear();
            foreach (var question in Detail.Questions())
            {
                Questions.Add(question);
            }

            if (Questions.Count == 0)
            {
                DetailNote = "This correction has no per-question breakdown stored (the legacy engine stored "
                             + "totals only). Use the PDF report for the full feedback.";
            }
            else
            {
                DetailNote = $"{Questions.Count} questions marked.";
            }

            Versions.Clear();
            var history = await Session.Api.ResultVersionsAsync(resultId);
            foreach (var version in history.Versions)
            {
                Versions.Add(version);
            }
        }
        catch (ApiException ex)
        {
            DetailNote = ex.Display;
        }
        catch (Exception ex)
        {
            DetailNote = ex.Message;
        }
        finally
        {
            _loadingDetail = false;
        }
    }
}
