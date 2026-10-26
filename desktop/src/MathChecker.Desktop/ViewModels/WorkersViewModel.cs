using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// The Correction monitor: a full page of worker cards, one per real
/// correction job the backend is running.
///
/// Every value on a card comes from the backend's job row. Progress, stage
/// text, worker assignment and timestamps are never simulated, a queued job is
/// shown as queued rather than pretending to work, and a job with no measured
/// progress says "Working" instead of an invented percentage. Live updates
/// arrive from the backend's SSE stream, with a slow poll as the reconciliation
/// path when the stream drops.
/// </summary>
public sealed class WorkersViewModel : PageViewModel
{
    private readonly DispatcherTimer _pollTimer;
    private readonly DispatcherTimer _clockTimer;
    private readonly Dictionary<string, WorkerCardViewModel> _cards = new();
    private readonly List<ChoiceOption> _filters = new()
    {
        new("All", "all"),
        new("Processing", "processing"),
        new("Queued", "queued"),
        new("Completed", "completed"),
        new("Failed", "failed"),
    };

    private bool _polling;
    private string _filter = "all";
    private string _searchText = string.Empty;
    private string _liveMode = "live updates";
    private string _workersText = "—";
    private WorkerCardViewModel? _selected;
    private CancellationTokenSource? _streamCts;
    private CancellationTokenSource? _detailCts;
    private int _requestedWorkers = 2;
    private int _poolSize;
    private bool _poolKnown;
    private string _workerNote = string.Empty;

    public WorkersViewModel(AppSession session)
        : base(session, "workers", "Correction monitor")
    {
        Group = "Correct";
        ApplyWorkersCommand = Command(ApplyWorkersAsync);
        _pollTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(3) };
        _pollTimer.Tick += async (_, _) => await PollAsync();
        _clockTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _clockTimer.Tick += (_, _) => TickClocks();
    }

    // ------------------------------------------------------------- the grid

    /// <summary>Cards currently visible (filtered + searched), newest first.
    /// Card instances are reused, so the grid never flickers or loses focus.</summary>
    public ObservableCollection<WorkerCardViewModel> Cards { get; } = new();

    public ObservableCollection<ChoiceOption> Filters => new(_filters);

    public string Filter
    {
        get => _filter;
        set
        {
            if (SetProperty(ref _filter, value))
            {
                Rebuild();
            }
        }
    }

    public string SearchText
    {
        get => _searchText;
        set
        {
            if (SetProperty(ref _searchText, value))
            {
                Rebuild();
            }
        }
    }

    public string LiveMode
    {
        get => _liveMode;
        private set => SetProperty(ref _liveMode, value);
    }

    public string WorkersText
    {
        get => _workersText;
        private set => SetProperty(ref _workersText, value);
    }

    public string ProviderText { get; private set; } = string.Empty;

    public bool HasProviderPause => ProviderText.Length > 0;

    public int TotalCount => _cards.Count;
    public int ProcessingCount => _cards.Values.Count(c => c.IsActive && !c.IsQueued);
    public int QueuedCount => _cards.Values.Count(c => c.IsQueued);
    public int CompletedCount => _cards.Values.Count(c => c.IsCompleted);
    public int FailedCount => _cards.Values.Count(c => c.IsFailed);
    public bool IsEmpty => Cards.Count == 0;

    public string EmptyTitle => _cards.Count == 0
        ? "No corrections yet"
        : "Nothing matches this filter";

    public string EmptyText => _cards.Count == 0
        ? "Start a correction from the New correction page; each one appears here as its own worker card."
        : "Try another tab or clear the search.";

    // --------------------------------------------------------------- details

    public WorkerCardViewModel? Selected
    {
        get => _selected;
        private set
        {
            if (SetProperty(ref _selected, value))
            {
                OnPropertyChanged(nameof(HasSelection));
                OnPropertyChanged(nameof(CanRetry));
                OnPropertyChanged(nameof(CanCancel));
                OnPropertyChanged(nameof(CanOpenResult));
            }
        }
    }

    /// <summary>Retry only makes sense for a job the backend marked failed: it
    /// creates a new attempt from the stored attempt count.</summary>
    public bool CanRetry => Selected is { IsFailed: true };

    /// <summary>The backend refuses cancellation for a job that already
    /// finished; a completed correction must stay a completed correction.</summary>
    public bool CanCancel => Selected is { } card && !card.IsFinished;

    public bool CanOpenResult => Selected?.Job.ResultId is { Length: > 0 };

    public bool HasSelection => Selected is not null;

    /// <summary>Labelled facts about the selected job (all from the backend).</summary>
    public ObservableCollection<FactRow> DetailFacts { get; } = new();

    /// <summary>The stages the backend recorded for the selected job.</summary>
    public ObservableCollection<StageEventDto> DetailStages { get; } = new();

    public bool HasStages => DetailStages.Count > 0;

    public string DetailTitle => Selected is null
        ? "Select a worker card"
        : $"{Selected.StudentName} · {Selected.WorkerLabel}";

    public AsyncCommand OpenCardCommand => Command(async parameter =>
    {
        if (parameter is WorkerCardViewModel card)
        {
            await SelectAsync(card);
        }
    });

    public RelayCommand CloseDetailsCommand => Sync(() =>
    {
        Selected = null;
        DetailFacts.Clear();
        DetailStages.Clear();
        OnPropertyChanged(nameof(HasStages));
        OnPropertyChanged(nameof(DetailTitle));
    });

    public AsyncCommand RefreshNowCommand => Command(async () => await RefreshDashboardAsync());

    // ------------------------------------------------- real actions on a job

    public AsyncCommand RetrySelectedCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        await Session.Api.RetryJobAsync(Selected.JobId);
        Status = "Retry queued for " + Selected.StudentName + ".";
        await RefreshDashboardAsync();
    });

    public AsyncCommand CancelSelectedCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Cancel this correction?",
                $"Stop correcting {Selected.StudentName}'s paper?\n\n"
                + "A queued job is removed; a running one stops at the next safe stage. "
                + "The submission itself is never deleted."))
        {
            return;
        }

        await Session.Api.CancelJobAsync(Selected.JobId);
        Status = "Cancellation requested for " + Selected.StudentName + ".";
        await RefreshDashboardAsync();
    });

    /// <summary>Opens the stored result in the marking desk, where the marks,
    /// the pages and the teacher's corrections live.</summary>
    public RelayCommand OpenResultCommand => Sync(() =>
    {
        if (Selected?.Job.ResultId is { Length: > 0 } resultId)
        {
            Session.OpenResult(resultId);
        }
    });

    // ------------------------------------------------- parallel corrections

    /// <summary>How many corrections may run at once. The pool caps this by the
    /// healthy API keys, so the number here is a request, not a promise —
    /// <see cref="WorkerNote"/> says what the backend actually did.</summary>
    public ObservableCollection<int> WorkerOptions { get; } = new() { 1, 2, 3, 4, 5, 6, 7, 8 };

    public int RequestedWorkers
    {
        get => _requestedWorkers;
        set
        {
            // 0 means the combo lost its selection, not a worker count.
            if (value > 0)
            {
                SetProperty(ref _requestedWorkers, value);
            }
        }
    }

    /// <summary>The pool size the backend holds, as a plain figure. It is shown
    /// on its own rather than as "0 of 2 running": the second number used to be
    /// the local request, so the card claimed two papers were being corrected
    /// while both were idle, and every figure on the page disagreed with it.</summary>
    public string PoolText => _poolKnown ? _poolSize.ToString() : "—";

    public string WorkerNote
    {
        get => _workerNote;
        private set => SetProperty(ref _workerNote, value);
    }

    /// <summary>Cached: a fresh command per binding evaluation left the button
    /// unable to disable itself while the request was in flight.</summary>
    public AsyncCommand ApplyWorkersCommand { get; }

    private async Task ApplyWorkersAsync()
    {
        var state = await Session.Api.ResizeWorkersAsync(RequestedWorkers);
        // The reply is the pool the backend actually holds: it caps the request
        // by healthy API keys, so the reply, not the request, is what we show.
        _poolSize = state.PoolSize;
        _poolKnown = state.PoolSize > 0;
        if (state.PoolSize > 0)
        {
            RequestedWorkers = state.PoolSize;
        }

        OnPropertyChanged(nameof(PoolText));
        WorkerNote = DescribePool(state.PoolSize, state.Active, state.Requested);
        Status = state.PoolSize < state.Requested
            ? $"Asked for {state.Requested}; the backend runs {state.PoolSize} because healthy API keys cap it."
            : $"Corrections at once set to {state.PoolSize}.";
        await RefreshDashboardAsync();
    }

    /// <summary>One honest line about the pool: what it holds and what is in
    /// flight. Nothing here claims work is happening when the count is zero.</summary>
    private static string DescribePool(int pool, int active, int requested)
    {
        if (pool <= 0)
        {
            return "The backend has no worker pool running.";
        }

        if (requested > pool)
        {
            return $"{pool} at once: healthy API keys cap the {requested} asked for.";
        }

        return active == 0
            ? $"{pool} at once · nothing being corrected right now"
            : $"{pool} at once · {active} being corrected right now";
    }

    public AsyncCommand OpenJobsCommand => new(() =>
    {
        Session.GoTo("jobs");
        return Task.CompletedTask;
    });

    public AsyncCommand StartCorrectionCommand => new(() =>
    {
        Session.GoTo("correct");
        return Task.CompletedTask;
    });

    /// <summary>Opens one card's details: its real fields plus the stage
    /// history the backend recorded for that job.</summary>
    public async Task SelectAsync(WorkerCardViewModel card)
    {
        Selected = card;
        FillDetailFacts(card, card.Job);
        OnPropertyChanged(nameof(DetailTitle));

        _detailCts?.Cancel();
        _detailCts?.Dispose();
        var cts = new CancellationTokenSource();
        _detailCts = cts;
        try
        {
            // The history and the stored result only come from the detail
            // endpoint; the list row is enough to paint the card itself.
            var detail = await Session.Api.JobAsync(card.JobId, cts.Token);
            if (cts.IsCancellationRequested || Selected != card)
            {
                return;
            }

            card.Apply(detail);
            FillDetailFacts(card, detail);
            DetailStages.Clear();
            foreach (var stage in detail.StageHistory)
            {
                DetailStages.Add(stage);
            }

            OnPropertyChanged(nameof(HasStages));
            OnPropertyChanged(nameof(DetailTitle));
        }
        catch (OperationCanceledException)
        {
            // A different card was opened.
        }
        catch (Exception ex)
        {
            Session.Log.Warn("job detail could not be loaded: " + ex.Message);
        }
    }

    private void FillDetailFacts(WorkerCardViewModel card, JobDto job)
    {
        DetailFacts.Clear();
        DetailFacts.Add(new FactRow("Student", job.StudentLabel));
        DetailFacts.Add(new FactRow("Paper", job.PaperText));
        DetailFacts.Add(new FactRow("Pages", job.PagesText));
        DetailFacts.Add(new FactRow("Status", card.StatusText));
        DetailFacts.Add(new FactRow("Working on", card.PhaseText));
        DetailFacts.Add(new FactRow("Stage detail",
            string.IsNullOrWhiteSpace(job.StageDetail) ? "Not reported yet" : job.StageDetail!));
        DetailFacts.Add(new FactRow("Worker", card.WorkerLabel));
        DetailFacts.Add(new FactRow("Progress", card.IsIndeterminate
            ? "Measured work not started"
            : card.ProgressText));
        DetailFacts.Add(new FactRow("Attempt", card.AttemptsText));
        DetailFacts.Add(new FactRow("Started", EpochTime.Format(job.StartedAt)));
        DetailFacts.Add(new FactRow("Last update", EpochTime.Format(job.UpdatedAt)));
        DetailFacts.Add(new FactRow("Elapsed", card.ElapsedText));
        if (job.IsRemark != 0)
        {
            DetailFacts.Add(new FactRow("Remark", "Yes — this is a re-mark"));
        }

        if (!string.IsNullOrWhiteSpace(job.ResultId))
        {
            DetailFacts.Add(new FactRow("Result", job.ResultId!));
        }

        var marks = job.MarksText;
        if (marks.Length > 0)
        {
            DetailFacts.Add(new FactRow("Marks", marks));
        }

        if (card.HasError)
        {
            DetailFacts.Add(new FactRow("Error", card.ErrorText));
        }

        if (string.IsNullOrWhiteSpace(job.StageDetail) && job.IsQueued)
        {
            DetailFacts.Add(new FactRow("Question progress",
                "Not reported by the backend for this stage"));
        }

        OnPropertyChanged(nameof(HasStages));
    }

    // ---------------------------------------------------------- live updates

    private async Task RefreshDashboardAsync()
    {
        var jobs = await Session.Api.JobsAsync(limit: 200);
        ApplyJobs(jobs.Jobs);
        await RefreshWorkersAsync();
    }

    private async Task RefreshWorkersAsync()
    {
        try
        {
            var state = await Session.Api.WorkersAsync();
            // The listing reports the pool size as "size" and the papers in
            // flight as "active"; it has no "running"/"requested" fields, so
            // reading those showed "0 of 0" however many workers existed.
            var pool = state.PoolSize;
            _poolSize = pool;
            _poolKnown = pool > 0;
            if (pool > 0)
            {
                RequestedWorkers = pool;
            }

            WorkersText = $"Workers {state.Active} of {pool}";
            OnPropertyChanged(nameof(PoolText));
            WorkerNote = DescribePool(pool, state.Active, pool);
            // A provider cooldown is why queued cards wait; say it here rather
            // than leaving the queue looking stuck.
            ProviderText = state.ProviderText;
            OnPropertyChanged(nameof(HasProviderPause));
        }
        catch (Exception ex)
        {
            // The worker pool endpoint is optional for the grid; the queue
            // counts on the cards still come from the job rows themselves.
            Session.Log.Warn("worker pool state unavailable: " + ex.Message);
            WorkersText = "Worker state unavailable";
            ProviderText = string.Empty;
            OnPropertyChanged(nameof(HasProviderPause));
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
            // Reconciling poll: catches states that changed while the stream was
            // down and resolves out-of-order events. It never invents progress.
            var jobs = await Session.Api.JobsAsync(limit: 200);
            ApplyJobs(jobs.Jobs);
            LiveMode = _streamCts is null ? "polling (stream unavailable)" : "live updates";
        }
        catch (Exception ex)
        {
            Session.Log.Warn("job poll failed: " + ex.Message);
            LiveMode = "reconnecting";
        }
        finally
        {
            _polling = false;
        }
    }

    /// <summary>Merges backend rows into the existing cards, replacing each card
    /// object rather than recreating the list.</summary>
    public void ApplyJobs(IEnumerable<JobDto> jobs)
    {
        var incoming = jobs.ToList();
        foreach (var job in incoming)
        {
            if (string.IsNullOrEmpty(job.Id))
            {
                continue;
            }

            if (_cards.TryGetValue(job.Id, out var card))
            {
                card.Apply(job);
            }
            else
            {
                _cards[job.Id] = new WorkerCardViewModel(job);
            }
        }

        // A job's row is never removed from the backend, but a bounded list can
        // scroll past old ones: keep the cards we have rather than dropping
        // them, and only forget a card when a full refresh says it is gone.
        Rebuild();
        RaiseSummary();
    }

    private bool Matches(WorkerCardViewModel card) => Filter switch
    {
        "processing" => card.IsActive && !card.IsQueued,
        "queued" => card.IsQueued,
        "completed" => card.IsCompleted,
        "failed" => card.IsFailed,
        _ => true,
    } && MatchesSearch(card);

    private bool MatchesSearch(WorkerCardViewModel card)
    {
        if (string.IsNullOrWhiteSpace(SearchText))
        {
            return true;
        }

        var needle = SearchText.Trim();
        return card.StudentName.Contains(needle, StringComparison.OrdinalIgnoreCase)
            || card.PaperText.Contains(needle, StringComparison.OrdinalIgnoreCase)
            || card.JobId.StartsWith(needle, StringComparison.OrdinalIgnoreCase)
            || card.StatusText.Contains(needle, StringComparison.OrdinalIgnoreCase);
    }

    private void Rebuild()
    {
        var wanted = _cards.Values
            .Where(Matches)
            .OrderByDescending(card => card.Job.CreatedAt ?? 0)
            .ToList();

        for (var index = Cards.Count - 1; index >= 0; index--)
        {
            if (!wanted.Contains(Cards[index]))
            {
                Cards.RemoveAt(index);
            }
        }

        for (var index = 0; index < wanted.Count; index++)
        {
            var card = wanted[index];
            var at = Cards.IndexOf(card);
            if (at < 0)
            {
                Cards.Insert(index, card);
            }
            else if (at != index)
            {
                Cards.Move(at, index);
            }
        }

        OnPropertyChanged(nameof(IsEmpty));
        OnPropertyChanged(nameof(EmptyTitle));
        OnPropertyChanged(nameof(EmptyText));
    }

    private void RaiseSummary()
    {
        OnPropertyChanged(nameof(TotalCount));
        OnPropertyChanged(nameof(ProcessingCount));
        OnPropertyChanged(nameof(QueuedCount));
        OnPropertyChanged(nameof(CompletedCount));
        OnPropertyChanged(nameof(FailedCount));
        OnPropertyChanged(nameof(IsEmpty));
        OnPropertyChanged(nameof(EmptyTitle));
        OnPropertyChanged(nameof(EmptyText));
    }

    private void TickClocks()
    {
        var now = DateTimeOffset.Now;
        foreach (var card in Cards)
        {
            card.Tick(now);
        }

        if (Selected is { } selected)
        {
            var elapsed = DetailFacts.FirstOrDefault(f => f.Label == "Elapsed");
            if (elapsed is not null)
            {
                var index = DetailFacts.IndexOf(elapsed);
                if (index >= 0 && elapsed.Value != selected.ElapsedText)
                {
                    DetailFacts[index] = new FactRow("Elapsed", selected.ElapsedText);
                }
            }
        }
    }

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
                        LiveMode = "live updates";
                        ApplyJobs(new[] { job });
                    });
                }, token);
            }
            catch (OperationCanceledException)
            {
                // Leaving the page.
            }
            catch (Exception ex)
            {
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
        _pollTimer.Start();
        _clockTimer.Start();
        StartStream();
    }

    public override void OnDeactivated()
    {
        _pollTimer.Stop();
        _clockTimer.Stop();
        StopStream();
        _detailCts?.Cancel();
    }

    protected override async Task LoadCoreAsync()
    {
        // Entering the page (again) rebuilds the cards from the backend, so a
        // run started elsewhere is never lost or duplicated.
        await RefreshDashboardAsync();
    }
}
