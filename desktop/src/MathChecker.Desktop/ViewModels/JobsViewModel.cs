using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class JobsViewModel : PageViewModel
{
    private readonly bool _showWorkers;
    private readonly DispatcherTimer _timer;
    private readonly List<ChoiceOption> _statusFilters = new()
    {
        new("All jobs", "all"),
        new("Queued", "QUEUED"),
        new("Marking", "MARKING"),
        new("Completed", "COMPLETED"),
        new("Failed", "FAILED"),
        new("Cancelled", "CANCELLED"),
    };

    private JobDto? _selected;
    private string _statusFilter = "all";
    private WorkerStateDto? _workers;
    private string _workerCount = "2";
    private string _liveMode = "polling";
    private CancellationTokenSource? _streamCts;
    private bool _polling;

    public JobsViewModel(AppSession session, string key, string title, bool showWorkers)
        : base(session, key, title)
    {
        _showWorkers = showWorkers;
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(10) };
        _timer.Tick += async (_, _) => await PollAsync();
    }

    public ObservableCollection<JobDto> Items { get; } = new();
    public ObservableCollection<ChoiceOption> StatusFilters => new(_statusFilters);

    public bool ShowWorkers => _showWorkers;

    public JobDto? Selected
    {
        get => _selected;
        set
        {
            if (SetProperty(ref _selected, value))
            {
                OnPropertyChanged(nameof(HasSelection));
                RefreshSelectedFacts();
            }
        }
    }

    public bool HasSelection => Selected is not null;

    /// <summary>The selected job as labelled facts. It used to be one
    /// concatenated string in a monospace box, which read like debug output.</summary>
    public ObservableCollection<FactRow> SelectedFacts { get; } = new();

    private void RefreshSelectedFacts()
    {
        SelectedFacts.Clear();
        var job = Selected;
        if (job is null)
        {
            return;
        }

        SelectedFacts.Add(new FactRow("Status", PayloadFacts.StatusLabel(job.Status)));
        SelectedFacts.Add(new FactRow("Stage", string.IsNullOrWhiteSpace(job.StageText)
            ? "Waiting for the worker"
            : job.StageText));
        SelectedFacts.Add(new FactRow("Student", job.StudentName ?? job.StudentId ?? "—"));
        SelectedFacts.Add(new FactRow("Attempt", $"{job.Attempts} of {job.MaxAttempts}"));
        SelectedFacts.Add(new FactRow("Progress", $"{job.Progress * 100:0}%"));
        SelectedFacts.Add(new FactRow("Created", job.CreatedText));
        SelectedFacts.Add(new FactRow("Finished", EpochTime.Format(job.FinishedAt)));
        if (!string.IsNullOrWhiteSpace(job.Error))
        {
            SelectedFacts.Add(new FactRow("Error", job.Error!));
        }

        // What the correction cost in provider requests, counted where they
        // are sent. Empty for jobs recorded before the ledger existed, so the
        // panel never claims a paper cost 0.
        if (!string.IsNullOrWhiteSpace(job.AiRequestsText))
        {
            SelectedFacts.Add(new FactRow("AI requests", job.AiRequestsText));
        }

        if (!string.IsNullOrWhiteSpace(job.ResultId))
        {
            SelectedFacts.Add(new FactRow("Result ID", job.ResultId!));
        }
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

    public string WorkerCount
    {
        get => _workerCount;
        set => SetProperty(ref _workerCount, value);
    }

    public string WorkersText => _workers is null ? "—" : _workers.Summary;

    public string LiveMode
    {
        get => _liveMode;
        private set => SetProperty(ref _liveMode, value);
    }

    public AsyncCommand CancelJobCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        var who = string.IsNullOrWhiteSpace(Selected.StudentName) ? "this paper" : Selected.StudentName;
        if (!Session.Dialogs.Confirm("Cancel job", $"Cancel the correction of {who}?"))
        {
            return;
        }

        await Session.Api.CancelJobAsync(Selected.Id);
        await LoadAsync();
    });

    public AsyncCommand RetryJobCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        await Session.Api.RetryJobAsync(Selected.Id);
        await LoadAsync();
    });

    public AsyncCommand ResizeWorkersCommand => Command(async () =>
    {
        if (!int.TryParse(WorkerCount, out var count) || count < 1 || count > 8)
        {
            Error = "The worker count must be between 1 and 8.";
            return;
        }

        _workers = await Session.Api.ResizeWorkersAsync(count);
        OnPropertyChanged(nameof(WorkersText));
        Status = "Worker pool resized.";
    });

    public RelayCommand OpenResultCommand => Sync(() =>
    {
        if (Selected?.ResultId is not null)
        {
            Session.GoTo("results");
        }
    });

    protected override async Task LoadCoreAsync()
    {
        var jobs = await Session.Api.JobsAsync(
            StatusFilter == "all" ? null : StatusFilter, 300);
        var previousId = Selected?.Id;
        Items.Clear();
        foreach (var job in jobs.Jobs)
        {
            Items.Add(job);
        }

        Selected = previousId is null ? null : Items.FirstOrDefault(j => j.Id == previousId);
        RefreshSelectedFacts();
        OnPropertyChanged(nameof(IsEmpty));

        if (_showWorkers)
        {
            _workers = await Session.Api.WorkersAsync();
            // "size" is the pool; "running" only exists in the resize reply.
            WorkerCount = _workers.PoolSize.ToString();
            OnPropertyChanged(nameof(WorkersText));
        }
    }

    private async Task PollAsync()
    {
        if (_polling || IsBusy)
        {
            return;
        }

        _polling = true;
        try
        {
            var jobs = await Session.Api.JobsAsync(StatusFilter == "all" ? null : StatusFilter, 300);
            Merge(jobs.Jobs);
        }
        catch (Exception ex)
        {
            Session.Log.Warn("job poll failed: " + ex.Message);
        }
        finally
        {
            _polling = false;
        }
    }

    /// <summary>Applies a job update from the SSE stream or a poll into the
    /// existing collection without losing the selection.</summary>
    private void Merge(IEnumerable<JobDto> jobs)
    {
        var selectedId = Selected?.Id;
        var incoming = jobs.ToList();

        foreach (var job in incoming)
        {
            var existing = Items.FirstOrDefault(i => i.Id == job.Id);
            if (existing is null)
            {
                Items.Insert(0, job);
            }
            else
            {
                var index = Items.IndexOf(existing);
                Items[index] = job;
            }
        }

        if (selectedId is not null)
        {
            Selected = Items.FirstOrDefault(i => i.Id == selectedId);
            RefreshSelectedFacts();
        }

        OnPropertyChanged(nameof(IsEmpty));
    }

    public bool IsEmpty => Items.Count == 0;

    private void StartStream()
    {
        if (_streamCts is not null)
        {
            return;
        }

        _streamCts = new CancellationTokenSource();
        var token = _streamCts.Token;
        var dispatcher = Application.Current?.Dispatcher;
        _ = Task.Run(async () =>
        {
            try
            {
                await Session.Api.StreamJobsAsync(async job =>
                {
                    if (dispatcher is null)
                    {
                        return;
                    }

                    await dispatcher.InvokeAsync(() =>
                    {
                        LiveMode = "live stream";
                        Merge(new[] { job });
                    });
                }, token);
            }
            catch (OperationCanceledException)
            {
                // Leaving the page.
            }
            catch (Exception ex)
            {
                // The SSE route can be unavailable behind a proxy; polling is
                // the fallback and keeps the page correct.
                Session.Log.Warn("job stream unavailable, using polling: " + ex.Message);
                if (dispatcher is not null)
                {
                    await dispatcher.InvokeAsync(() => LiveMode = "polling (stream unavailable)");
                }
            }
        }, token);
    }

    private void StopStream()
    {
        _streamCts?.Cancel();
        _streamCts?.Dispose();
        _streamCts = null;
    }

    public override void OnActivated()
    {
        _timer.Start();
        StartStream();
    }

    public override void OnDeactivated()
    {
        _timer.Stop();
        StopStream();
    }
}
