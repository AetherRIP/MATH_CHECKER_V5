using System.Diagnostics;
using System.Text.Json;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Mvvm;

namespace MathChecker.Desktop.Core.Sync;

/// <summary>Where one synchronization run stands. ``Partial`` is a real
/// outcome, not a synonym for success: required work failed.</summary>
public enum SyncOutcome
{
    Idle,
    Running,
    Completed,
    Partial,
    Failed,
}

/// <summary>One part the backend could not synchronize, with the ids that let
/// a retry re-run JUST that part.</summary>
public sealed record SyncProblem(string Part, string Detail, string? CourseId, string? CourseworkId)
{
    /// <summary>True when the backend named an assignment, so the part can be
    /// re-synced on its own instead of repeating the whole class sync.</summary>
    public bool Retryable => !string.IsNullOrEmpty(CourseId) && !string.IsNullOrEmpty(CourseworkId);

    public string Text => string.IsNullOrWhiteSpace(Detail) ? Part : $"{Part}: {Detail}";

    public override string ToString() => Text;
}

/// <summary>The verified result of one run, built from the backend's own final
/// snapshot — never from a client-side assumption that "it looks finished".</summary>
public sealed record SyncResult(
    SyncOutcome Outcome,
    string Headline,
    string Counts,
    string? Error,
    IReadOnlyList<SyncProblem> Problems,
    double ElapsedSeconds,
    bool RetryRun,
    JsonElement Snapshot)
{
    public bool IsCompleted => Outcome == SyncOutcome.Completed;
    public bool IsPartial => Outcome == SyncOutcome.Partial;
    public bool IsFailed => Outcome == SyncOutcome.Failed;

    /// <summary>One line for a toast: what actually happened.</summary>
    public string Summary => (Headline + (Counts.Length > 0 ? " · " + Counts : string.Empty)).Trim();
}

/// <summary>
/// ONE synchronization path for the whole desktop app.
///
/// Before this type every page had its own idea of "sync": the People page
/// started the backend job, waited 1.5 s, read the progress ONCE and reloaded —
/// so the UI always showed "Connecting… 5 %" while the sync was still running,
/// and the new courses/students/submissions arrived minutes later, or never
/// (nobody refreshed them). It read exactly like "the sync stops halfway".
///
/// This service instead:
///   * starts the backend's full sync (courses → assignments → students →
///     submissions → database) or joins the run already in flight, so a second
///     click can never queue a duplicate job;
///   * polls ``/api/classroom/sync/progress`` until the backend reports the run
///     finished, surfacing its real stage, message and percentage;
///   * ends ONLY on the backend's own final state: ``error`` → failed, a
///     ``summary`` with ``partial`` → partial, otherwise completed;
///   * re-runs just the named failed assignments on request;
///   * raises <see cref="Completed"/> so the visible page reloads its rows.
/// </summary>
public sealed class ClassroomSyncService : ObservableObject
{
    /// <summary>How often the pollable snapshot is read while a run is alive.
    /// A full class sync takes tens of seconds, so this is cheap and prompt.</summary>
    public TimeSpan PollInterval { get; set; } = TimeSpan.FromSeconds(1);

    /// <summary>A hard stop so a wedged backend can never spin the UI forever
    /// (the backend itself has its own request timeouts; this only bounds the
    /// client's patience).</summary>
    public TimeSpan MaxWait { get; set; } = TimeSpan.FromMinutes(30);

    /// <summary>Consecutive failed progress polls tolerated before the client
    /// stops claiming it knows what the backend is doing.</summary>
    public int MaxPollFailures { get; set; } = 3;

    private readonly AppSession _session;
    private readonly object _lock = new();
    private Task<SyncResult?>? _inFlight;
    private SyncResult? _last;
    private bool _isRunning;
    private bool _hasPercent;
    private double _percent;
    private string _message = string.Empty;
    private string _stage = string.Empty;

    public ClassroomSyncService(AppSession session)
    {
        _session = session;
        SyncCommand = new AsyncCommand(() => SyncAsync(), onError: ex =>
            _session.Log.Error("classroom sync command failed", ex));
        RetryFailedCommand = new AsyncCommand(RetryFailedAsync,
            canExecute: () => CanRetry && !IsRunning,
            onError: ex => _session.Log.Error("classroom sync retry failed", ex));
    }

    /// <summary>Runs (or joins) the full synchronization. Every page's Sync
    /// button calls this same command, so repeated or simultaneous clicks can
    /// never start a second job.</summary>
    public AsyncCommand SyncCommand { get; }

    /// <summary>Re-syncs only the parts the last run reported as failed.</summary>
    public AsyncCommand RetryFailedCommand { get; }

    /// <summary>Starts the one shared synchronization from a non-button caller
    /// (for example right after Google Classroom was connected). It joins a
    /// run already in progress rather than starting a second one.</summary>
    public void RequestSync() => SyncCommand.Execute(null);

    /// <summary>Raised on the caller's thread once a run reached a final state
    /// (completed, partial or failed). The shell reloads the visible page.</summary>
    public event Action<SyncResult>? Completed;

    public SyncResult? Last => _last;

    public bool IsRunning
    {
        get => _isRunning;
        private set
        {
            if (!SetProperty(ref _isRunning, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasState));
            OnPropertyChanged(nameof(CanRetry));
            OnPropertyChanged(nameof(HasProblems));
            OnPropertyChanged(nameof(ShowProgress));
            RetryFailedCommand.RaiseCanExecuteChanged();
        }
    }

    /// <summary>0–100, straight from the backend's aggregate progress. Zero
    /// means "not measurable yet", and then no number is shown.</summary>
    public double Percent
    {
        get => _percent;
        private set
        {
            if (SetProperty(ref _percent, value))
            {
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    public bool HasPercent
    {
        get => _hasPercent;
        private set
        {
            if (SetProperty(ref _hasPercent, value))
            {
                OnPropertyChanged(nameof(ShowProgress));
            }
        }
    }

    /// <summary>The backend's own stage message ("Fetching submissions — …").</summary>
    public string Message
    {
        get => _message;
        private set
        {
            if (SetProperty(ref _message, value))
            {
                OnPropertyChanged(nameof(StatusText));
            }
        }
    }

    public string Stage
    {
        get => _stage;
        private set => SetProperty(ref _stage, value);
    }

    /// <summary>True while running, or once a run has finished — the header
    /// chip appears only when there is something honest to say.</summary>
    public bool HasState => IsRunning || _last is not null;

    public bool IsPartial => _last?.IsPartial ?? false;

    public bool IsFailed => _last?.IsFailed ?? false;

    public bool HasRun => _last is not null;

    /// <summary>A progress bar is shown only while a run is actually alive.</summary>
    public bool ShowProgress => IsRunning;

    /// <summary>Unknown progress is drawn as an indeterminate bar, never as a
    /// made-up percentage.</summary>
    public bool IsIndeterminate => !HasPercent;

    public string StatusText
    {
        get
        {
            if (IsRunning)
            {
                var message = string.IsNullOrWhiteSpace(Message) ? "Starting…" : Message;
                return HasPercent
                    ? $"{Percent:0}% · {message}"
                    : message;
            }

            if (_last is null)
            {
                return string.Empty;
            }

            return _last.Summary;
        }
    }

    public bool CanRetry => !IsRunning && (_last?.Problems.Any(p => p.Retryable) ?? false);

    /// <summary>True when the detail line actually adds something the one-line
    /// status does not: a failure reason or the failed parts. A clean run must
    /// not print the same sentence twice.</summary>
    public bool HasProblems => IsFailed || (_last?.Problems.Count ?? 0) > 0;

    /// <summary>Long form for a page card: outcome, counts, elapsed, and every
    /// problem the backend reported.</summary>
    public string DetailText
    {
        get
        {
            if (IsRunning)
            {
                return StatusText;
            }

            if (_last is null)
            {
                return "Not synced from this window yet.";
            }

            if (_last.IsFailed)
            {
                return "Sync failed: " + _last.Error;
            }

            var text = _last.Summary;
            if (_last.Problems.Count > 0)
            {
                text += " · " + string.Join("; ", _last.Problems.Select(p => p.Text));
            }

            return text;
        }
    }

    // ------------------------------------------------------------------ running

    /// <summary>Starts the full sync, or joins the one already running.
    /// Returns the single shared result so two callers never disagree.</summary>
    public Task<SyncResult?> SyncAsync()
    {
        lock (_lock)
        {
            if (_inFlight is { IsCompleted: false })
            {
                // A second click (on this page or another) watches the same run.
                return _inFlight;
            }

            _inFlight = RunAsync();
            return _inFlight;
        }
    }

    /// <summary>Re-runs only the assignments the last run failed on.</summary>
    public Task<SyncResult?> RetryFailedAsync()
    {
        lock (_lock)
        {
            if (_inFlight is { IsCompleted: false })
            {
                return _inFlight;
            }

            var parts = _last?.Problems.Where(p => p.Retryable).ToList() ?? new List<SyncProblem>();
            if (parts.Count == 0)
            {
                return Task.FromResult<SyncResult?>(_last);
            }

            _inFlight = RetryRunAsync(parts);
            return _inFlight;
        }
    }

    private async Task<SyncResult?> RunAsync()
    {
        Reset();
        try
        {
            var result = await WatchAsync(
                () => _session.Api.StartClassroomSyncAsync(cancellationToken: default),
                retryRun: false);
            return Publish(result);
        }
        catch (Exception ex)
        {
            return Publish(FailedFrom(ex));
        }
    }

    private async Task<SyncResult?> RetryRunAsync(IReadOnlyList<SyncProblem> parts)
    {
        Reset();
        var repaired = new List<string>();
        var stillFailing = new List<SyncProblem>();
        try
        {
            foreach (var part in parts)
            {
                var partResult = await WatchAsync(
                    () => _session.Api.StartClassroomSyncAsync(part.CourseId, part.CourseworkId),
                    retryRun: true);
                if (partResult.IsCompleted)
                {
                    repaired.Add(part.Part);
                }
                else
                {
                    stillFailing.Add(part);
                }
            }

            var headline = stillFailing.Count == 0
                ? $"Re-synced {repaired.Count} failed part{(repaired.Count == 1 ? string.Empty : "s")}"
                : $"Re-synced {repaired.Count} of {parts.Count} failed parts";
            var outcome = stillFailing.Count == 0 ? SyncOutcome.Completed : SyncOutcome.Partial;
            var result = new SyncResult(outcome, headline, string.Empty,
                stillFailing.Count == 0 ? null : "Some parts still failed",
                stillFailing, 0, true, default);
            return Publish(result);
        }
        catch (Exception ex)
        {
            return Publish(FailedFrom(ex));
        }
    }

    /// <summary>Starts one run through <paramref name="start"/> and follows the
    /// backend's progress registry until that run reports a final state.</summary>
    private async Task<SyncResult> WatchAsync(Func<Task<JsonElement>> start, bool retryRun)
    {
        var clock = Stopwatch.StartNew();
        var failures = 0;

        var started = await start();
        var snapshot = SnapshotOf(started);
        Apply(snapshot);
        var runId = Text(snapshot, "id");

        while (IsRunning)
        {
            if (clock.Elapsed > MaxWait)
            {
                return new SyncResult(SyncOutcome.Failed,
                    "Sync is still running in the backend",
                    string.Empty,
                    $"No final state after {MaxWait.TotalMinutes:0} minutes; reopen the page to read its progress.",
                    Array.Empty<SyncProblem>(), clock.Elapsed.TotalSeconds, retryRun, snapshot);
            }

            await Task.Delay(PollInterval);

            JsonElement progress;
            try
            {
                progress = await _session.Api.ClassroomSyncProgressAsync();
            }
            catch (ApiException)
            {
                // The backend answered and refused: that is a real failure, not
                // a flaky network hop.
                throw;
            }
            catch (Exception ex) when (++failures <= MaxPollFailures)
            {
                _session.Log.Warn("classroom sync progress poll failed: " + ex.Message);
                continue;
            }
            catch (Exception ex)
            {
                return new SyncResult(SyncOutcome.Failed,
                    "Lost contact with the backend",
                    string.Empty,
                    ex.Message,
                    Array.Empty<SyncProblem>(), clock.Elapsed.TotalSeconds, retryRun, snapshot);
            }

            failures = 0;
            var next = SnapshotOf(progress);
            var nextId = Text(next, "id");
            if (runId.Length > 0 && nextId.Length > 0 && nextId != runId)
            {
                // The backend was restarted mid-run: its registry no longer
                // knows our run, so no result for it exists. Say so.
                return new SyncResult(SyncOutcome.Failed,
                    "The backend restarted during the sync",
                    string.Empty,
                    "The run's result is unknown. Sync again to finish.",
                    Array.Empty<SyncProblem>(), clock.Elapsed.TotalSeconds, retryRun, next);
            }

            snapshot = next;
            Apply(snapshot);
        }

        return Build(snapshot, clock.Elapsed, retryRun);
    }

    /// <summary>Final result from the backend's own terminal snapshot.</summary>
    private static SyncResult Build(JsonElement snapshot, TimeSpan elapsed, bool retryRun)
    {
        var error = Text(snapshot, "error");
        var summary = Property(snapshot, "summary");
        var problems = ProblemsOf(summary);

        if (error.Length > 0)
        {
            return new SyncResult(SyncOutcome.Failed, "Sync failed", string.Empty, error,
                problems, elapsed.TotalSeconds, retryRun, snapshot);
        }

        if (summary.ValueKind != JsonValueKind.Object)
        {
            return new SyncResult(SyncOutcome.Failed,
                "Sync ended without a result",
                string.Empty,
                "The backend never reported a summary for this run.",
                problems, elapsed.TotalSeconds, retryRun, snapshot);
        }

        var counts = CountsOf(summary);
        var partial = Flag(summary, "partial");
        var seconds = Number(summary, "elapsed_s");
        if (seconds <= 0)
        {
            seconds = elapsed.TotalSeconds;
        }

        if (partial)
        {
            return new SyncResult(SyncOutcome.Partial,
                $"Completed with {problems.Count} problem{(problems.Count == 1 ? string.Empty : "s")}",
                counts, null, problems, seconds, retryRun, snapshot);
        }

        return new SyncResult(SyncOutcome.Completed,
            $"Sync complete in {seconds:0} s",
            counts, null, problems, seconds, retryRun, snapshot);
    }

    private SyncResult Publish(SyncResult result)
    {
        _last = result;
        IsRunning = false;
        OnPropertyChanged(nameof(Last));
        OnPropertyChanged(nameof(HasRun));
        OnPropertyChanged(nameof(HasState));
        OnPropertyChanged(nameof(IsPartial));
        OnPropertyChanged(nameof(IsFailed));
        OnPropertyChanged(nameof(StatusText));
        OnPropertyChanged(nameof(DetailText));
        OnPropertyChanged(nameof(HasProblems));
        OnPropertyChanged(nameof(CanRetry));
        OnPropertyChanged(nameof(ShowProgress));
        RetryFailedCommand.RaiseCanExecuteChanged();

        _session.Log.Info($"classroom sync {result.Outcome}: {result.Summary}"
                          + (result.Error is null ? string.Empty : $" ({result.Error})"));
        Completed?.Invoke(result);
        return result;
    }

    private SyncResult FailedFrom(Exception ex)
    {
        var message = ex is ApiException api ? api.Display : ex.Message;
        _session.Log.Warn("classroom sync failed: " + message);
        return new SyncResult(SyncOutcome.Failed, "Sync failed", string.Empty, message,
            Array.Empty<SyncProblem>(), 0, false, default);
    }

    private void Reset()
    {
        Percent = 0;
        HasPercent = false;
        Message = string.Empty;
        Stage = string.Empty;
        IsRunning = true;
        OnPropertyChanged(nameof(HasState));
        OnPropertyChanged(nameof(StatusText));
        OnPropertyChanged(nameof(DetailText));
    }

    // ----------------------------------------------------------- reading a frame

    private void Apply(JsonElement snapshot)
    {
        if (snapshot.ValueKind != JsonValueKind.Object)
        {
            return;
        }

        Stage = Text(snapshot, "stage");
        Message = Text(snapshot, "message");
        var progress = Number(snapshot, "progress");
        HasPercent = progress > 0;
        Percent = progress <= 0 ? 0 : Math.Round(progress * 100, 0);
        IsRunning = Flag(snapshot, "running");
    }

    private static JsonElement SnapshotOf(JsonElement payload)
    {
        // /sync/start answers {"started":bool,"snapshot":{…}}; /sync/progress
        // answers the snapshot itself. Accept both so one reader serves both.
        var inner = Property(payload, "snapshot");
        return inner.ValueKind == JsonValueKind.Object ? inner : payload;
    }

    private static JsonElement Property(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value)
            ? value
            : default;

    private static string Text(JsonElement element, string name)
    {
        var value = Property(element, name);
        return value.ValueKind == JsonValueKind.String ? value.GetString() ?? string.Empty : string.Empty;
    }

    private static bool Flag(JsonElement element, string name)
    {
        var value = Property(element, name);
        return value.ValueKind == JsonValueKind.True;
    }

    private static double Number(JsonElement element, string name)
    {
        var value = Property(element, name);
        return value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var number)
            ? number
            : 0;
    }

    private static List<SyncProblem> ProblemsOf(JsonElement summary)
    {
        var list = new List<SyncProblem>();
        var errors = Property(summary, "errors");
        if (errors.ValueKind != JsonValueKind.Array)
        {
            return list;
        }

        foreach (var entry in errors.EnumerateArray())
        {
            if (entry.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            var part = Text(entry, "part");
            var courseId = Text(entry, "course_id");
            var courseworkId = Text(entry, "coursework_id");
            list.Add(new SyncProblem(
                part.Length > 0 ? part : "sync",
                Text(entry, "detail"),
                courseId.Length > 0 ? courseId : null,
                courseworkId.Length > 0 ? courseworkId : null));
        }

        return list;
    }

    /// <summary>"2 courses · 5 assignments · 40 students · 300 submissions",
    /// listing only the counts the backend actually reported.</summary>
    private static string CountsOf(JsonElement summary)
    {
        var parts = new List<string>();
        AddCount("courses", "course");
        AddCount("assignments", "assignment");
        AddCount("students", "student");
        if (!summary.TryGetProperty("students", out _))
        {
            // An assignment-scoped run reports its roster instead of students.
            AddCount("roster", "student");
        }

        AddCount("submissions", "submission");

        var unavailable = Property(summary, "unavailable");
        if (unavailable.ValueKind == JsonValueKind.Array && unavailable.GetArrayLength() > 0)
        {
            var n = unavailable.GetArrayLength();
            parts.Add($"{n} unavailable in Classroom");
        }

        return string.Join(" · ", parts);

        void AddCount(string key, string singular)
        {
            var value = Property(summary, key);
            if (value.ValueKind != JsonValueKind.Number || !value.TryGetDouble(out var number))
            {
                return;
            }

            var count = (long)number;
            parts.Add(count == 1 ? $"1 {singular}" : $"{count} {singular}s");
        }
    }
}
