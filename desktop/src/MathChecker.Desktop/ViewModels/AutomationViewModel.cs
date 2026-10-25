using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class AutomationViewModel : PageViewModel
{
    private readonly DispatcherTimer _timer;
    private AutomationStatusDto? _status;
    private AutomationJobDto? _selected;
    private string _logRaw = string.Empty;

    private bool _enabled;
    private int _intervalMinutes = 10;
    private bool _reviewMode = true;
    private bool _draftOnly;
    private bool _attachReport = true;
    private bool _submitGrade = true;
    private bool _returnWork = true;
    private string _reportDetail = "detailed";

    public AutomationViewModel(AppSession session)
        : base(session, "automation", "Automatic")
    {
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(20) };
        _timer.Tick += async (_, _) =>
        {
            if (!IsBusy)
            {
                await LoadAsync();
            }
        };
    }

    public ObservableCollection<AutomationJobDto> Items { get; } = new();
    public ObservableCollection<int> Intervals { get; } = new() { 5, 10, 15, 30 };
    public ObservableCollection<ChoiceOption> ReportDetails { get; } = new()
    {
        new("Summary", "summary"),
        new("Standard", "standard"),
        new("Detailed", "detailed"),
    };

    public AutomationJobDto? Selected
    {
        get => _selected;
        set
        {
            if (SetProperty(ref _selected, value))
            {
                OnPropertyChanged(nameof(HasSelection));
            }
        }
    }

    public bool HasSelection => Selected is not null;

    public bool Enabled
    {
        get => _enabled;
        set => SetProperty(ref _enabled, value);
    }

    /// <summary>
    /// The interval combo binds its SelectedItem here. A ComboBox writes null
    /// back whenever its item list is rebuilt, and a non-nullable int rejects
    /// that write as a conversion error: the control then showed a red border
    /// and an empty box for the rest of the session. Nullable keeps that
    /// transient write legal while the stored value stays a real interval.
    /// </summary>
    public int? IntervalMinutes
    {
        get => _intervalMinutes;
        set
        {
            if (value is null || value <= 0 || value == _intervalMinutes)
            {
                return;
            }

            _intervalMinutes = value.Value;
            OnPropertyChanged(nameof(IntervalMinutes));
        }
    }

    public bool ReviewMode
    {
        get => _reviewMode;
        set => SetProperty(ref _reviewMode, value);
    }

    public bool DraftOnly
    {
        get => _draftOnly;
        set => SetProperty(ref _draftOnly, value);
    }

    public bool AttachReport
    {
        get => _attachReport;
        set => SetProperty(ref _attachReport, value);
    }

    public bool SubmitGrade
    {
        get => _submitGrade;
        set => SetProperty(ref _submitGrade, value);
    }

    public bool ReturnWork
    {
        get => _returnWork;
        set => SetProperty(ref _returnWork, value);
    }

    public string ReportDetail
    {
        get => _reportDetail;
        set => SetProperty(ref _reportDetail, value);
    }

    public string WorkerText => _status is null
        ? "—"
        : $"Automation {(_status.Enabled ? "ON" : "OFF")} · worker {_status.WorkerState ?? "unknown"} · "
          + $"interval {_status.IntervalMinutes} min · last check {_status.LastCheckText} · next {_status.NextCheckText}";

    public string CountsText => _status is null
        ? "—"
        : $"Pending {_status.PendingJobs} · active {_status.ActiveJobs} · "
          + $"assignments enabled {_status.AssignmentsEnabled}"
          + (_status.Counts.Count == 0
              ? string.Empty
              : " · " + string.Join(" · ", _status.Counts
                  .Select(p => $"{PayloadFacts.KeyLabel(p.Key).ToLowerInvariant()} {p.Value}")));

    /// <summary>The activity feed as readable lines (time · level · what
    /// happened). The raw response is kept for technical details only.</summary>
    public ObservableCollection<string> LogLines { get; } = new();

    public string LogRaw
    {
        get => _logRaw;
        private set
        {
            if (SetProperty(ref _logRaw, value))
            {
                OnPropertyChanged(nameof(HasLogRaw));
            }
        }
    }

    public bool HasLogRaw => !string.IsNullOrWhiteSpace(LogRaw);

    public AsyncCommand ToggleWorkerCommand => Command(async () =>
    {
        await Session.Api.SetAutomationWorkerAsync(!(_status?.WorkerEnabled ?? false));
        await LoadAsync();
        Status = "Worker state updated by the backend.";
    });

    public AsyncCommand SaveSettingsCommand => Command(async () =>
    {
        if (IntervalMinutes <= 0)
        {
            Error = "Choose an interval before saving: the backend will not accept 0 minutes.";
            return;
        }

        var payload = new
        {
            enabled = Enabled,
            interval_minutes = IntervalMinutes ?? 10,
            review_mode = ReviewMode,
            draft_only = DraftOnly,
            attach_report = AttachReport,
            submit_grade = SubmitGrade,
            return_work = ReturnWork,
            report_detail = ReportDetail,
        };
        await Session.Api.SetAutomationSettingsAsync(payload);
        await LoadAsync();
        Status = "Automation settings saved.";
    });

    public AsyncCommand RunNowCommand => Command(async () =>
    {
        if (!Session.Dialogs.Confirm("Run automation now",
                "Run one detection cycle and advance pending jobs immediately?"))
        {
            return;
        }

        await Session.Api.RunAutomationNowAsync();
        await LoadAsync();
        Status = "Detections and pending jobs advanced.";
    });

    public AsyncCommand ApproveCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        await Session.Api.ApproveAutomationJobAsync(Selected.Id);
        await LoadAsync();
        Status = "Job approved.";
    });

    public AsyncCommand RetryCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        await Session.Api.RetryAutomationJobAsync(Selected.Id);
        await LoadAsync();
    });

    public AsyncCommand ReconcileCommand => Command(async () =>
    {
        await Session.Api.ReconcileAutomationAsync();
        await LoadAsync();
        Status = "Jobs reconciled against Google Classroom.";
    });

    public AsyncCommand ClearLogCommand => Command(async () =>
    {
        if (!Session.Dialogs.Confirm("Clear automation log", "Clear the displayed automation log?"))
        {
            return;
        }

        await Session.Api.ClearAutomationLogAsync();
        await LoadAsync();
    });

    protected override async Task LoadCoreAsync()
    {
        _status = await Session.Api.AutomationStatusAsync();
        var jobs = await Session.Api.AutomationJobsAsync(limit: 200);
        var log = await Session.Api.AutomationLogAsync();

        Items.Clear();
        foreach (var job in jobs.Jobs)
        {
            Items.Add(job);
        }

        Enabled = _status.Enabled;
        ReviewMode = _status.ReviewMode;
        DraftOnly = _status.DraftOnly;
        AttachReport = _status.AttachReport;
        SubmitGrade = _status.SubmitGrade;
        ReturnWork = _status.ReturnWork;
        ReportDetail = _status.ReportDetail ?? "detailed";

        // The list has to exist BEFORE the value is applied. Clearing it makes
        // the ComboBox write null back through SelectedItem, and re-adding the
        // items afterwards does not restore the choice: the control stayed
        // blank, and a later save would have posted interval 0.
        Intervals.Clear();
        foreach (var interval in _status.Intervals.Count > 0 ? _status.Intervals : new List<int> { 5, 10, 15, 30 })
        {
            Intervals.Add(interval);
        }

        if (_status.IntervalMinutes > 0 && !Intervals.Contains(_status.IntervalMinutes))
        {
            Intervals.Add(_status.IntervalMinutes);
        }

        IntervalMinutes = _status.IntervalMinutes;
        // Re-read even when the value did not change: the combo lost its
        // selection while the list was rebuilt.
        OnPropertyChanged(nameof(IntervalMinutes));

        FillLog(log);
        OnPropertyChanged(nameof(WorkerText));
        OnPropertyChanged(nameof(CountsText));
        OnPropertyChanged(nameof(IsEmpty));
    }

    private void FillLog(JsonElement log)
    {
        LogLines.Clear();
        LogRaw = PayloadFacts.Raw(log);

        if (!log.TryGetProperty("entries", out var entries) || entries.ValueKind != JsonValueKind.Array)
        {
            LogLines.Add("The backend returned no activity entries.");
            return;
        }

        foreach (var entry in entries.EnumerateArray())
        {
            var message = Text(entry, "message") ?? Text(entry, "event") ?? "Activity recorded";
            var level = PayloadFacts.StatusLabel(Text(entry, "level"));
            var stamp = Text(entry, "created_at") ?? Text(entry, "timestamp") ?? Text(entry, "ts");

            var when = string.Empty;
            if (!string.IsNullOrWhiteSpace(stamp))
            {
                when = PayloadFacts.TryFormatTimestamp(stamp!, out var formatted)
                    ? formatted
                    : stamp!;
            }

            LogLines.Add(string.Join("  ·  ", new[] { when, level, message }
                .Where(part => !string.IsNullOrWhiteSpace(part))));
        }

        if (LogLines.Count == 0)
        {
            LogLines.Add("No automation activity yet. Runs appear here once the worker checks for work.");
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
            JsonValueKind.String => value.GetString(),
            JsonValueKind.Number => value.ToString(),
            JsonValueKind.True => "On",
            JsonValueKind.False => "Off",
            _ => null,
        };
    }

    public override void OnActivated() => _timer.Start();
    public override void OnDeactivated() => _timer.Stop();

    public bool IsEmpty => Items.Count == 0;
}
