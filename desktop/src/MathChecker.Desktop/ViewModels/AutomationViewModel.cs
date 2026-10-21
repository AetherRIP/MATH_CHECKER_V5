using System.Collections.ObjectModel;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class AutomationViewModel : PageViewModel
{
    private readonly DispatcherTimer _timer;
    private AutomationStatusDto? _status;
    private AutomationJobDto? _selected;
    private string _logText = string.Empty;

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
    public ObservableCollection<string> ReportDetails { get; } = new() { "summary", "standard", "detailed" };

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

    public int IntervalMinutes
    {
        get => _intervalMinutes;
        set => SetProperty(ref _intervalMinutes, value);
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
        : $"pending {_status.PendingJobs} · active {_status.ActiveJobs} · enabled assignments {_status.AssignmentsEnabled}"
          + (_status.Counts.Count == 0
              ? string.Empty
              : " · " + string.Join(" · ", _status.Counts.Select(p => $"{p.Key} {p.Value}")));

    public string LogText
    {
        get => _logText;
        private set => SetProperty(ref _logText, value);
    }

    public AsyncCommand ToggleWorkerCommand => Command(async () =>
    {
        await Session.Api.SetAutomationWorkerAsync(!(_status?.WorkerEnabled ?? false));
        await LoadAsync();
        Status = "Worker state updated by the backend.";
    });

    public AsyncCommand SaveSettingsCommand => Command(async () =>
    {
        var payload = new
        {
            enabled = Enabled,
            interval_minutes = IntervalMinutes,
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
        IntervalMinutes = _status.IntervalMinutes;
        ReviewMode = _status.ReviewMode;
        DraftOnly = _status.DraftOnly;
        AttachReport = _status.AttachReport;
        SubmitGrade = _status.SubmitGrade;
        ReturnWork = _status.ReturnWork;
        ReportDetail = _status.ReportDetail ?? "detailed";

        Intervals.Clear();
        foreach (var interval in _status.Intervals.Count > 0 ? _status.Intervals : new List<int> { 5, 10, 15, 30 })
        {
            Intervals.Add(interval);
        }

        LogText = Json.Pretty(log);
        OnPropertyChanged(nameof(WorkerText));
        OnPropertyChanged(nameof(CountsText));
    }

    public override void OnActivated() => _timer.Start();
    public override void OnDeactivated() => _timer.Stop();
}
