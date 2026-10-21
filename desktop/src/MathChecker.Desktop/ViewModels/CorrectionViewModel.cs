using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class PipelineOption
{
    public string Value { get; init; } = "auto";
    public string Label { get; init; } = string.Empty;
    public string Detail { get; init; } = string.Empty;
}

public sealed class CorrectionViewModel : PageViewModel
{
    private static readonly string[] AllowedExtensions = { ".pdf", ".png", ".jpg", ".jpeg", ".webp" };
    private const long MaxBytes = 200L * 1024 * 1024;

    private string? _submissionPath;
    private string? _markSchemePath;
    private string _studentName = "Student";
    private PipelineOption? _selectedPipeline;
    private double _progress;
    private string _progressText = string.Empty;
    private string _outcome = string.Empty;
    private string _rulesText = string.Empty;
    private bool _uploading;

    public CorrectionViewModel(AppSession session)
        : base(session, "correct", "New correction")
    {
    }

    public ObservableCollection<PipelineOption> Pipelines { get; } = new();
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

    public PipelineOption? SelectedPipeline
    {
        get => _selectedPipeline;
        set => SetProperty(ref _selectedPipeline, value);
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

    public string RulesText
    {
        get => _rulesText;
        private set => SetProperty(ref _rulesText, value);
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

    public AsyncCommand SubmitCommand => Command(async () =>
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
                SelectedPipeline?.Value ?? "auto", progress, CancellationToken.None);

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
        var pipeline = result.TryGetProperty("applied_pipeline", out var p) ? p.GetString() : "?";
        var pages = result.TryGetProperty("page_count", out var pc) ? pc.GetInt32() : 0;
        var limit = result.TryGetProperty("page_limit", out var pl) ? pl.GetInt32() : 0;
        var reason = result.TryGetProperty("reason", out var r) ? r.GetString() : string.Empty;
        var jobId = result.TryGetProperty("job_id", out var j) ? j.GetString() : null;
        var largeId = result.TryGetProperty("large_pdf_job_id", out var lj) ? lj.GetString() : null;

        return $"Pipeline: {pipeline}\nPages: {pages} (standard threshold {limit})\n"
               + (jobId is null ? string.Empty : $"Correction job: {jobId}\n")
               + (largeId is null ? string.Empty : $"Batched job: {largeId}\n")
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
        var payload = await Session.Api.CorrectionPipelinesAsync();
        Pipelines.Clear();
        if (payload.TryGetProperty("options", out var options) && options.ValueKind == JsonValueKind.Array)
        {
            foreach (var option in options.EnumerateArray())
            {
                Pipelines.Add(new PipelineOption
                {
                    Value = option.TryGetProperty("value", out var v) ? v.GetString() ?? "auto" : "auto",
                    Label = option.TryGetProperty("label", out var l) ? l.GetString() ?? string.Empty : string.Empty,
                    Detail = option.TryGetProperty("detail", out var d) ? d.GetString() ?? string.Empty : string.Empty,
                });
            }
        }

        SelectedPipeline = Pipelines.FirstOrDefault(p => p.Value == "auto") ?? Pipelines.FirstOrDefault();

        var threshold = payload.TryGetProperty("threshold_pages", out var t) ? t.GetInt32() : 0;
        var enabled = payload.TryGetProperty("large_pdf_enabled", out var e) && e.GetBoolean();
        RulesText =
            $"Documents up to {threshold} pages use the single-pass pipeline; longer documents are split into "
            + $"balanced batches and merged. The batched pipeline is currently {(enabled ? "enabled" : "DISABLED in Settings")}. "
            + "The backend decides the route from the student PDF's real page count.";

        await RefreshJobsAsync();
    }
}
