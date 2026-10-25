using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// One card on the correction dashboard: exactly one correction job, bound to
/// the backend's own fields.
///
/// Nothing here is invented. Every value comes from the job row the backend
/// returned (worker id, status, stage text, measured progress, timestamps), an
/// unknown value reads "Not available", and a job that has not measured any
/// work shows an indeterminate bar instead of a made-up percentage. The card is
/// updated in place, so the grid never rebuilds itself while a teacher watches.
/// </summary>
public sealed class WorkerCardViewModel : ObservableObject
{
    private JobDto _job;

    public WorkerCardViewModel(JobDto job)
    {
        _job = job;
        JobId = job.Id;
        Job = job;
        Refresh();
    }

    /// <summary>The job this card represents; never replaced, only updated.</summary>
    public JobDto Job { get; private set; }

    public string JobId { get; private set; }

    public string StudentName { get; private set; } = "Not available";
    public string PaperText { get; private set; } = "Not available";
    public string PagesText { get; private set; } = "Not available";
    public string WorkerLabel { get; private set; } = "Not assigned";
    public string StatusText { get; private set; } = "—";
    public string PhaseText { get; private set; } = string.Empty;
    public string StageText { get; private set; } = string.Empty;

    /// <summary>True only when the stage says something the phase line does
    /// not: a completed card used to print "Correction completed" twice.</summary>
    public bool HasStageLine => StageText.Length > 0
        && !string.Equals(StageText, PhaseText, StringComparison.OrdinalIgnoreCase);
    public string QuestionText { get; private set; } = string.Empty;
    public bool HasQuestion { get; private set; }
    public string AttemptsText { get; private set; } = "1 of 1";
    public string ErrorText { get; private set; } = string.Empty;
    public bool HasError { get; private set; }
    public string ScoreText { get; private set; } = string.Empty;
    public bool HasScore { get; private set; }
    public string FinishedText { get; private set; } = string.Empty;
    public bool IsFinished { get; private set; }

    public double Progress { get; private set; }
    public bool IsIndeterminate { get; private set; }
    public string ProgressText { get; private set; } = "0%";
    public string ElapsedText { get; private set; } = "—";
    public string EtaText { get; private set; } = string.Empty;
    public bool HasEta { get; private set; }

    public bool IsActive { get; private set; }
    public bool IsQueued { get; private set; }
    public bool IsCompleted { get; private set; }
    public bool IsFailed { get; private set; }
    public bool IsWaitingForApi { get; private set; }

    /// <summary>Visual tone the card style keys off: active | queued | done | error.</summary>
    public string Tone { get; private set; } = "queued";

    public string WorkerGlyph => IsWorkerAssigned ? "\uE9F5" : "\uE823";

    public bool IsWorkerAssigned { get; private set; }

    public string JobIdText => string.IsNullOrEmpty(JobId) ? "—" : JobId[..Math.Min(8, JobId.Length)];

    /// <summary>
    /// Copies a newer backend state into this card. Returns false when the
    /// update is stale (an older row arriving after a newer one), which is how
    /// out-of-order stream events are ignored instead of rewinding the card.
    /// </summary>
    public bool Apply(JobDto job, DateTimeOffset? now = null)
    {
        if (!string.Equals(job.Id, JobId, StringComparison.Ordinal))
        {
            return false;
        }

        if (_job.UpdatedAt is not null && job.UpdatedAt is not null
            && job.UpdatedAt.Value < _job.UpdatedAt.Value && !job.IsTerminal)
        {
            return false;
        }

        // A terminal card is never rewound by a late "still marking" event.
        if (_job.IsTerminal && !job.IsTerminal)
        {
            return false;
        }

        _job = job;
        Job = job;
        Refresh(now);
        return true;
    }

    private void Refresh(DateTimeOffset? now = null)
    {
        var job = _job;
        StudentName = job.StudentLabel;
        PaperText = job.PaperText;
        PagesText = job.PagesText;
        IsWorkerAssigned = !string.IsNullOrWhiteSpace(job.WorkerId);
        WorkerLabel = IsWorkerAssigned ? FormatWorker(job.WorkerId!) : "Not assigned";
        StatusText = job.StatusText;
        PhaseText = job.PhaseText;
        StageText = job.StageText;
        AttemptsText = $"{Math.Max(1, job.Attempts)} of {Math.Max(1, job.MaxAttempts)}";

        IsActive = job.IsActive;
        IsQueued = job.IsQueued;
        IsCompleted = job.IsCompleted;
        IsFailed = job.IsFailed;
        IsWaitingForApi = job.IsWaitingForApi;
        Tone = job.IsFailed ? "error"
            : job.IsCompleted ? "done"
            : job.IsQueued ? "queued"
            : "active";

        ErrorText = job.Error ?? string.Empty;
        HasError = !string.IsNullOrWhiteSpace(job.Error);

        Progress = Math.Clamp(job.Progress, 0, 1);
        // A QUEUED job is not working, so it says so and shows no moving bar.
        // An active job that has measured nothing yet shows activity instead
        // of a frozen 0%, and never a percentage it has not earned.
        IsIndeterminate = job.IsActive && !job.IsQueued && Progress <= 0.0001;
        ProgressText = job.IsQueued
            ? "Waiting"
            : IsIndeterminate ? "Working" : $"{Progress * 100:0}%";

        QuestionText = ExtractQuestion(job.StageDetail);
        HasQuestion = QuestionText.Length > 0;

        ScoreText = job.ResultId is null ? string.Empty
            : $"Result {job.ResultId[..Math.Min(8, job.ResultId.Length)]}";
        HasScore = job.ResultId is not null;
        FinishedText = job.FinishedAt is null ? string.Empty
            : $"Finished {EpochTime.Clock(job.FinishedAt)}";
        IsFinished = job.FinishedAt is not null;

        Tick(now ?? DateTimeOffset.Now);
        // One notification for the whole card: every property above was just
        // recomputed from the same backend row, so binding them one by one
        // would only produce dozens of redundant refreshes per update.
        OnPropertyChanged(string.Empty);
    }

    /// <summary>Updates only the clocks. Called on a timer so elapsed time
    /// keeps moving between backend updates without inventing progress.</summary>
    public void Tick(DateTimeOffset now)
    {
        var started = _job.StartedAt ?? _job.CreatedAt;
        if (started is null)
        {
            SetClock("Not available", string.Empty, false);
            return;
        }

        var startedAt = DateTimeOffset.FromUnixTimeMilliseconds((long)(started.Value * 1000));
        var endedAt = _job.FinishedAt is null
            ? now
            : DateTimeOffset.FromUnixTimeMilliseconds((long)(_job.FinishedAt.Value * 1000));
        var elapsed = endedAt - startedAt;
        if (elapsed < TimeSpan.Zero)
        {
            elapsed = TimeSpan.Zero;
        }

        // An estimate only from real measured work: a job that has finished
        // none of its stages (or only a sliver) has no honest basis for one.
        var eta = string.Empty;
        if (_job.IsActive && Progress is > 0.03 and < 1 && elapsed.TotalSeconds >= 20)
        {
            var remaining = TimeSpan.FromSeconds(
                elapsed.TotalSeconds * (1 - Progress) / Progress);
            if (remaining > TimeSpan.FromSeconds(1) && remaining < TimeSpan.FromHours(2))
            {
                eta = $"~{Round(remaining)} left";
            }
        }

        SetClock(Clock(elapsed), eta, eta.Length > 0);
    }

    private void SetClock(string elapsed, string eta, bool hasEta)
    {
        ElapsedText = elapsed;
        EtaText = eta;
        HasEta = hasEta;
        OnPropertyChanged(nameof(ElapsedText));
        OnPropertyChanged(nameof(EtaText));
        OnPropertyChanged(nameof(HasEta));
    }

    private static string Clock(TimeSpan span) => span.TotalHours >= 1
        ? $"{(int)span.TotalHours}:{span.Minutes:00}:{span.Seconds:00}"
        : $"{span.Minutes:00}:{span.Seconds:00}";

    private static string Round(TimeSpan span) => span.TotalSeconds < 60
        ? $"{Math.Max(5, (int)Math.Round(span.TotalSeconds / 5) * 5)} s"
        : $"{Math.Max(1, (int)Math.Round(span.TotalMinutes))} min";

    /// <summary>"Worker 03" from the pool's real worker id ("W3"); an id that
    /// is not in that shape is shown as the backend wrote it.</summary>
    public static string FormatWorker(string workerId)
    {
        var text = workerId.Trim();
        if (text.Length > 1 && (text[0] == 'W' || text[0] == 'w')
            && int.TryParse(text[1..], out var number))
        {
            return $"Worker {number:00}";
        }

        return text.Length == 0 ? "Not assigned" : text;
    }

    /// <summary>A question number the backend itself mentioned in the stage
    /// text, or nothing. Question-level progress is not reported today, so this
    /// stays empty rather than guessing which question is being marked.</summary>
    private static string ExtractQuestion(string? stage)
    {
        if (string.IsNullOrWhiteSpace(stage))
        {
            return string.Empty;
        }

        var match = System.Text.RegularExpressions.Regex.Match(
            stage, @"\b(?:question|q)\s*(\d+(?:\([a-zA-Z]\)|[a-zA-Z])?)",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        return match.Success ? $"Question {match.Groups[1].Value}" : string.Empty;
    }
}
