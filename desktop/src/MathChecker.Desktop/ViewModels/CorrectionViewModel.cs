using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class CorrectionViewModel : PageViewModel
{
    private static readonly string[] AllowedExtensions = { ".pdf", ".png", ".jpg", ".jpeg", ".webp" };
    private const long MaxBytes = 200L * 1024 * 1024;

    private string? _submissionPath;
    private string? _markSchemePath;
    private string _studentName = "Student";
    private double _progress;
    private string _progressText = string.Empty;
    private string _outcome = string.Empty;
    private bool _uploading;

    public CorrectionViewModel(AppSession session)
        : base(session, "correct", "New correction")
    {
    }

    public ObservableCollection<JobDto> RecentJobs { get; } = new();

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

    public string SubmissionLabel => SubmissionPath is null
        ? "No student PDF selected"
        : $"{Path.GetFileName(SubmissionPath)} ({new FileInfo(SubmissionPath).Length / 1024d:0.#} KB)";

    public string MarkSchemeLabel => MarkSchemePath is null
        ? "No mark scheme selected"
        : Path.GetFileName(MarkSchemePath);

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

    public string Outcome
    {
        get => _outcome;
        private set => SetProperty(ref _outcome, value);
    }

    public bool CanSubmit => !_uploading && SubmissionPath is not null && MarkSchemePath is not null;

    public RelayCommand PickSubmissionCommand => Sync(() =>
    {
        var path = Session.Dialogs.PickOpenFile("Select the student's answer file",
            "Documents (*.pdf;*.png;*.jpg;*.jpeg;*.webp)|*.pdf;*.png;*.jpg;*.jpeg;*.webp|All files (*.*)|*.*");
        if (path is not null)
        {
            SetSubmission(path);
        }
    });

    public RelayCommand PickMarkSchemeCommand => Sync(() =>
    {
        var path = Session.Dialogs.PickOpenFile("Select the mark scheme",
            "Documents (*.pdf;*.png;*.jpg;*.jpeg;*.webp)|*.pdf;*.png;*.jpg;*.jpeg;*.webp|All files (*.*)|*.*");
        if (path is not null)
        {
            SetMarkScheme(path);
        }
    });

    public RelayCommand ClearCommand => Sync(() =>
    {
        SubmissionPath = null;
        MarkSchemePath = null;
        Outcome = string.Empty;
        Progress = 0;
        ProgressText = string.Empty;
        RaiseCanSubmit();
    });

    // Cached: the XAML binding holds ONE command instance, so the instance
    // RaiseCanSubmit() notifies must be the same one. Rebuilding the command on
    // every property read left "Start correction" permanently disabled.
    private AsyncCommand? _submitCommand;

    public AsyncCommand SubmitCommand => _submitCommand ??= Command(async () =>
    {
        if (SubmissionPath is null || MarkSchemePath is null)
        {
            Error = "Select both the student's work and the mark scheme.";
            return;
        }

        if (!ValidateFile(SubmissionPath, out var problem) || !ValidateFile(MarkSchemePath, out problem))
        {
            Error = problem;
            return;
        }

        _uploading = true;
        RaiseCanSubmit();
        Progress = 0;
        ProgressText = "Uploading…";
        Outcome = string.Empty;
        Error = null;
        try
        {
            var progress = new Progress<double>(fraction =>
            {
                Progress = fraction;
                ProgressText = $"Uploading… {fraction * 100:0}%";
            });

            var result = await Session.Api.SubmitUnifiedCorrectionAsync(
                SubmissionPath, MarkSchemePath,
                string.IsNullOrWhiteSpace(StudentName) ? "Student" : StudentName.Trim(),
                progress, CancellationToken.None);

            ProgressText = "Accepted by the backend";
            Outcome = Describe(result);
            Session.Notify("Correction queued.");
            await RefreshJobsAsync();
        }
        finally
        {
            _uploading = false;
            RaiseCanSubmit();
        }
    }, canExecute: () => CanSubmit);

    public AsyncCommand OpenJobsCommand => new(() =>
    {
        Session.GoTo("jobs");
        return Task.CompletedTask;
    });

    public void SetSubmission(string path)
    {
        SubmissionPath = path;
        Error = null;
        RaiseCanSubmit();
    }

    public void SetMarkScheme(string path)
    {
        MarkSchemePath = path;
        Error = null;
        RaiseCanSubmit();
    }

    private void RaiseCanSubmit()
    {
        OnPropertyChanged(nameof(CanSubmit));
        if (SubmitCommand is AsyncCommand command)
        {
            command.RaiseCanExecuteChanged();
        }
    }

    private static bool ValidateFile(string path, out string problem)
    {
        problem = string.Empty;
        var extension = Path.GetExtension(path).ToLowerInvariant();
        if (!AllowedExtensions.Contains(extension))
        {
            problem = $"Unsupported file type '{extension}'. Use PDF, PNG, JPG or WEBP — the backend "
                      + "rejects anything else.";
            return false;
        }

        var info = new FileInfo(path);
        if (!info.Exists)
        {
            problem = "The selected file no longer exists.";
            return false;
        }

        if (info.Length > MaxBytes)
        {
            problem = $"{info.Name} is larger than 200 MB, which the backend refuses.";
            return false;
        }

        return true;
    }

    private static string Describe(JsonElement result)
    {
        var pages = result.TryGetProperty("page_count", out var pc) ? pc.GetInt32() : 0;
        var reason = result.TryGetProperty("reason", out var r) ? r.GetString() : string.Empty;
        var jobId = result.TryGetProperty("job_id", out var j) ? j.GetString() : null;

        return $"Standard single-pass pipeline\nPages: {pages}\n"
               + (jobId is null ? string.Empty : $"Correction job: {jobId}\n")
               + $"\n{reason}";
    }

    private async Task RefreshJobsAsync()
    {
        var jobs = await Session.Api.JobsAsync(limit: 5);
        RecentJobs.Clear();
        foreach (var job in jobs.Jobs)
        {
            RecentJobs.Add(job);
        }
    }

    protected override async Task LoadCoreAsync()
    {
        await RefreshJobsAsync();
    }
}
