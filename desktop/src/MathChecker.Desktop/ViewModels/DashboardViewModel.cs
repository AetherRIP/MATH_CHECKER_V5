using System.Collections.ObjectModel;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class DashboardViewModel : PageViewModel
{
    private readonly DispatcherTimer _timer;
    private DashboardDto? _dashboard;
    private JobDto? _selectedJob;
    private string _lastUpdated = "—";

    public DashboardViewModel(AppSession session)
        : base(session, "dashboard", "Today")
    {
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(15) };
        _timer.Tick += async (_, _) =>
        {
            if (!IsBusy)
            {
                await LoadAsync();
            }
        };
    }

    public ObservableCollection<AttentionItemDto> AttentionItems { get; } = new();
    public ObservableCollection<JobDto> RecentJobs { get; } = new();

    public JobDto? SelectedJob
    {
        get => _selectedJob;
        set => SetProperty(ref _selectedJob, value);
    }

    public string LastUpdated
    {
        get => _lastUpdated;
        private set => SetProperty(ref _lastUpdated, value);
    }

    public string TodayText => _dashboard?.CorrectionsToday.ToString() ?? "—";
    public string PendingText => _dashboard?.Pending.ToString() ?? "—";
    public string ProcessingText => _dashboard?.Processing.ToString() ?? "—";
    public string CompletedText => _dashboard?.Completed.ToString() ?? "—";
    public string FailedText => _dashboard?.Failed.ToString() ?? "—";

    public string ApiHealthText
    {
        get
        {
            var health = _dashboard?.ApiHealth;
            if (health is null)
            {
                return "—";
            }

            return health.HasKeys
                ? $"{health.Healthy} healthy of {health.Total} configured"
                : "No Gemini API keys configured";
        }
    }

    public bool HasKeys => _dashboard?.ApiHealth?.HasKeys ?? false;

    public string ClassroomText
    {
        get
        {
            var classroom = _dashboard?.Classroom;
            if (classroom is null)
            {
                return "—";
            }

            if (!classroom.Configured)
            {
                return "Not configured (no OAuth client)";
            }

            return classroom.Connected
                ? $"Connected as {classroom.Email} · {classroom.Courses} courses · {classroom.Allowlisted} assignments enabled"
                : "Configured but not connected";
        }
    }

    public string VersionText => _dashboard is null
        ? "—"
        : $"v{_dashboard.Version} (build {_dashboard.BuildNumber}) · {(_dashboard.DemoMode ? "DEMO data" : "live data")}"
          + (_dashboard.MockMode ? " · mock marking" : string.Empty);

    public string AttentionSummary => AttentionItems.Count == 0
        ? "Nothing needs attention."
        : $"{AttentionItems.Count} items need attention";

    public string? RoleText => _dashboard?.Role;

    public bool HasAttention => AttentionItems.Count > 0;

    public AsyncCommand OpenCorrectionCommand => new(() =>
    {
        Session.GoTo("correct");
        return Task.CompletedTask;
    });

    public AsyncCommand OpenReviewCommand => new(() =>
    {
        Session.GoTo("review");
        return Task.CompletedTask;
    });

    public AsyncCommand OpenResultsCommand => new(() =>
    {
        Session.GoTo("results");
        return Task.CompletedTask;
    });

    public AsyncCommand OpenJobsCommand => new(() =>
    {
        Session.GoTo("jobs");
        return Task.CompletedTask;
    });

    public AsyncCommand OpenAiKeysCommand => new(() =>
    {
        Session.GoTo("ai");
        return Task.CompletedTask;
    });

    public AsyncCommand CancelSelectedJobCommand => Command(async () =>
    {
        if (SelectedJob is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Cancel job",
                $"Cancel the correction for {SelectedJob.StudentName ?? SelectedJob.Id}?"))
        {
            return;
        }

        await Session.Api.CancelJobAsync(SelectedJob.Id);
        await LoadAsync();
    });

    public AsyncCommand RetrySelectedJobCommand => Command(async () =>
    {
        if (SelectedJob is null)
        {
            return;
        }

        await Session.Api.RetryJobAsync(SelectedJob.Id);
        await LoadAsync();
    });

    protected override async Task LoadCoreAsync()
    {
        var dashboard = await Session.Api.DashboardAsync();
        var attention = await Session.Api.AttentionAsync();
        var jobs = await Session.Api.JobsAsync(limit: 8);

        _dashboard = dashboard;
        AttentionItems.Clear();
        foreach (var item in attention.Items)
        {
            AttentionItems.Add(item);
        }

        RecentJobs.Clear();
        foreach (var job in jobs.Jobs)
        {
            RecentJobs.Add(job);
        }

        LastUpdated = DateTime.Now.ToString("HH:mm:ss");
        OnPropertyChanged(nameof(TodayText));
        OnPropertyChanged(nameof(PendingText));
        OnPropertyChanged(nameof(ProcessingText));
        OnPropertyChanged(nameof(CompletedText));
        OnPropertyChanged(nameof(FailedText));
        OnPropertyChanged(nameof(ApiHealthText));
        OnPropertyChanged(nameof(HasKeys));
        OnPropertyChanged(nameof(ClassroomText));
        OnPropertyChanged(nameof(VersionText));
        OnPropertyChanged(nameof(AttentionSummary));
        OnPropertyChanged(nameof(HasAttention));
        OnPropertyChanged(nameof(RoleText));
    }

    public override void OnActivated() => _timer.Start();

    public override void OnDeactivated() => _timer.Stop();
}
