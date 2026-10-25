using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// The New correction page: choose a student's paper and the mark scheme, then
/// hand both to the backend.
///
/// Watching the correction happen belongs to the Correction monitor
/// (<see cref="WorkersViewModel"/>), which owns the worker cards; this page
/// keeps its original job — attaching the two files and queueing one
/// correction — and sends the teacher there once the backend accepts it.
/// </summary>
public sealed class CorrectionViewModel : PageViewModel
{
    private static readonly string[] AllowedExtensions = { ".pdf", ".png", ".jpg", ".jpeg", ".webp" };
    private const long MaxBytes = 200L * 1024 * 1024;

    private string? _submissionPath;
    private string? _markSchemePath;
    private string _studentName = "Student";
    private double _uploadProgress;
    private string _progressText = string.Empty;
    private string _outcomeNote = string.Empty;
    private bool _uploading;
    private bool _uploadIndeterminate;

    public CorrectionViewModel(AppSession session)
        : base(session, "correct", "New correction")
    {
        Group = "Correct";
    }

    // ---------------------------------------------------------------- files

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

    /// <summary>Upload transfer progress only — the correction's own progress
    /// is reported per worker card on the Correction monitor.</summary>
    public double Progress
    {
        get => _uploadProgress;
        private set => SetProperty(ref _uploadProgress, value);
    }

    public string ProgressText
    {
        get => _progressText;
        private set => SetProperty(ref _progressText, value);
    }

    public bool IsProgressIndeterminate
    {
        get => _uploadIndeterminate;
        private set => SetProperty(ref _uploadIndeterminate, value);
    }

    public ObservableCollection<FactRow> OutcomeFacts { get; } = new();

    public string OutcomeNote
    {
        get => _outcomeNote;
        private set
        {
            if (SetProperty(ref _outcomeNote, value))
            {
                OnPropertyChanged(nameof(HasOutcome));
            }
        }
    }

    public bool HasOutcome => OutcomeFacts.Count > 0 || !string.IsNullOrWhiteSpace(OutcomeNote);

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
        OutcomeFacts.Clear();
        OutcomeNote = string.Empty;
        OnPropertyChanged(nameof(HasOutcome));
        Progress = 0;
        IsProgressIndeterminate = false;
        ProgressText = string.Empty;
        RaiseCanSubmit();
    });

    /// <summary>Opens the page that shows every correction the backend runs.</summary>
    public AsyncCommand OpenMonitorCommand => new(() =>
    {
        Session.GoTo("workers");
        return Task.CompletedTask;
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
        IsProgressIndeterminate = true;
        ProgressText = "Uploading the files…";
        OutcomeFacts.Clear();
        OutcomeNote = string.Empty;
        OnPropertyChanged(nameof(HasOutcome));
        Error = null;
        try
        {
            var progress = new Progress<double>(fraction =>
            {
                Progress = fraction;
                ProgressText = $"Uploading the files… {fraction * 100:0}%";
            });

            var result = await Session.Api.SubmitUnifiedCorrectionAsync(
                SubmissionPath, MarkSchemePath,
                string.IsNullOrWhiteSpace(StudentName) ? "Student" : StudentName.Trim(),
                progress, CancellationToken.None);

            FillOutcome(result);
            Status = "Correction queued — it is running on the Correction monitor.";
            Session.Notify("Correction queued. Follow it on the Correction monitor.");

            // The upload ending is not the correction finishing: the monitor
            // shows the job the backend actually queued, as its own card.
            var jobId = result.TryGetProperty("job_id", out var id) && id.ValueKind == JsonValueKind.String
                ? id.GetString()
                : null;
            if (!string.IsNullOrWhiteSpace(jobId))
            {
                Session.GoTo("workers");
            }
        }
        finally
        {
            _uploading = false;
            RaiseCanSubmit();
            IsProgressIndeterminate = false;
            ProgressText = Progress >= 0.999 ? string.Empty : "Upload did not finish.";
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

    private void FillOutcome(JsonElement result)
    {
        OutcomeFacts.Clear();
        foreach (var fact in PayloadFacts.Of(result))
        {
            OutcomeFacts.Add(fact);
        }

        var reason = result.TryGetProperty("reason", out var r) && r.ValueKind == JsonValueKind.String
            ? r.GetString()
            : null;
        OutcomeNote = string.IsNullOrWhiteSpace(reason)
            ? "The backend accepted the upload and queued the correction."
            : reason!;
        OnPropertyChanged(nameof(HasOutcome));
    }

    protected override Task LoadCoreAsync()
    {
        // A form has nothing to fetch: every figure on this page comes from the
        // files the teacher just chose, and the queued correction is followed
        // on the Correction monitor.
        return Task.CompletedTask;
    }
}
