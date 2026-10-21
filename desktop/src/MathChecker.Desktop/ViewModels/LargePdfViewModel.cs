using System.Collections.ObjectModel;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class LargePdfViewModel : PageViewModel
{
    private readonly DispatcherTimer _timer;
    private LargePdfJobDto? _selected;
    private LargePdfJobDetailDto? _detail;
    private LargePdfConfigDto? _config;
    private string? _submissionPath;
    private string? _markSchemePath;
    private string _studentName = "Student";
    private string _progressText = string.Empty;
    private string _summary = string.Empty;
    private double _progress;

    public LargePdfViewModel(AppSession session)
        : base(session, "largepdf", "Batched jobs")
    {
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(8) };
        _timer.Tick += async (_, _) =>
        {
            if (!IsBusy)
            {
                await LoadAsync();
            }
        };
    }

    public ObservableCollection<LargePdfJobDto> Items { get; } = new();
    public ObservableCollection<BatchDto> Batches { get; } = new();

    public string ConfigText => _config is null
        ? "—"
        : $"Batched pipeline {( _config.Enabled ? "enabled" : "DISABLED")} · threshold {_config.ThresholdPages} pages · "
          + $"batches {_config.TargetMin}–{_config.TargetMax} pages · concurrency {_config.Concurrency}";

    public LargePdfJobDto? Selected
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

    public string? SubmissionPath
    {
        get => _submissionPath;
        private set
        {
            if (SetProperty(ref _submissionPath, value))
            {
                OnPropertyChanged(nameof(SubmissionLabel));
            }
        }
    }

    public string? MarkSchemePath
    {
        get => _markSchemePath;
        private set
        {
            if (SetProperty(ref _markSchemePath, value))
            {
                OnPropertyChanged(nameof(MarkSchemeLabel));
            }
        }
    }

    public string SubmissionLabel => SubmissionPath is null ? "No student PDF" : Path.GetFileName(SubmissionPath);
    public string MarkSchemeLabel => MarkSchemePath is null ? "No mark scheme" : Path.GetFileName(MarkSchemePath);

    public string StudentName
    {
        get => _studentName;
        set => SetProperty(ref _studentName, value);
    }

    public double Progress
    {
        get => _progress;
        private set => SetProperty(ref _progress, value);
    }

    public string ProgressText
    {
        get => _progressText;
        private set => SetProperty(ref _progressText, value);
    }

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    public RelayCommand PickSubmissionCommand => Sync(() =>
    {
        var path = Session.Dialogs.PickOpenFile("Select the student's answer PDF",
            "PDF (*.pdf)|*.pdf|All files (*.*)|*.*");
        if (path is not null)
        {
            SubmissionPath = path;
        }
    });

    public RelayCommand PickMarkSchemeCommand => Sync(() =>
    {
        var path = Session.Dialogs.PickOpenFile("Select the mark scheme PDF",
            "PDF (*.pdf)|*.pdf|All files (*.*)|*.*");
        if (path is not null)
        {
            MarkSchemePath = path;
        }
    });

    public AsyncCommand CreateCommand => Command(async () =>
    {
        if (SubmissionPath is null || MarkSchemePath is null)
        {
            Error = "Select both files first.";
            return;
        }

        var progress = new Progress<double>(f => Progress = f);
        var payload = await Session.Api.CreateLargePdfJobAsync(SubmissionPath, MarkSchemePath,
            string.IsNullOrWhiteSpace(StudentName) ? "Student" : StudentName, progress, CancellationToken.None);
        ProgressText = "Uploaded and planned";
        Summary = Models.Json.Pretty(payload);
        SubmissionPath = null;
        MarkSchemePath = null;
        await LoadAsync();
        Status = "Batched job created.";
    });

    public AsyncCommand ProcessCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        await Session.Api.ProcessLargePdfJobAsync(Selected.Id);
        await LoadAsync();
    });

    public AsyncCommand RetryCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        await Session.Api.RetryLargePdfJobAsync(Selected.Id);
        await LoadAsync();
    });

    public AsyncCommand RetryBatchCommand => Command(async () =>
    {
        if (Selected is null || Batches.Count == 0)
        {
            return;
        }

        var batch = Batches.FirstOrDefault(b => b.Status is "FAILED" or "PARTIAL") ?? Batches[0];
        await Session.Api.RetryLargePdfBatchAsync(Selected.Id, batch.BatchIndex);
        await LoadAsync();
    });

    public AsyncCommand CancelCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Cancel batched job", $"Cancel the batched job for {Selected.Name}?"))
        {
            return;
        }

        await Session.Api.CancelLargePdfJobAsync(Selected.Id);
        await LoadAsync();
    });

    protected override async Task LoadCoreAsync()
    {
        var jobs = await Session.Api.LargePdfJobsAsync();
        _config = jobs.Config;
        var previous = Selected?.Id;
        Items.Clear();
        foreach (var job in jobs.Jobs)
        {
            Items.Add(job);
        }

        if (previous is not null)
        {
            Selected = Items.FirstOrDefault(j => j.Id == previous);
        }

        OnPropertyChanged(nameof(ConfigText));
        if (Items.Count == 0)
        {
            Summary = "No batched jobs yet. Long documents submitted from New correction appear here "
                      + "when the page count exceeds the standard threshold.";
        }
        else
        {
            var counts = jobs.Counts;
            Summary = counts is null
                ? $"{Items.Count} jobs"
                : $"{Items.Count} jobs · {counts.Queued} queued · {counts.Processing} processing · "
                  + $"{counts.Completed} completed · {counts.Failed} failed";
        }
    }

    private async Task LoadDetailAsync(string jobId)
    {
        try
        {
            _detail = await Session.Api.LargePdfJobAsync(jobId);
            Batches.Clear();
            foreach (var batch in _detail.Batches)
            {
                Batches.Add(batch);
            }

            if (string.IsNullOrWhiteSpace(Summary) || true)
            {
                Summary = $"{_detail.PageCount} pages in {_detail.BatchCount} batches · {_detail.StatusLabel}";
            }

            if (_detail.Summary is { } summary)
            {
                ProgressText = Models.Json.Pretty(summary);
            }
        }
        catch (Exception ex)
        {
            Error = "Could not load the batch detail: " + ex.Message;
        }
    }

    public override void OnActivated() => _timer.Start();
    public override void OnDeactivated() => _timer.Stop();

    public bool HasSelection => Selected is not null;
}
