using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.Json;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// Auto-correct: the guided way to correct a whole assignment.
///
/// Three steps, in order: choose an assignment, read exactly what the backend
/// would correct (its own preview, including the blockers), then start it. The
/// numbers are never computed here — the preview endpoint classifies verified
/// Classroom state, and the start call enqueues each student independently, so
/// what the page promises is what runs.
///
/// The background scheduler (interval scans, review mode, posting grades back
/// to Classroom) lives in Advanced settings: it is a different feature from
/// correcting an assignment now, and mixing the two was what made this page
/// confusing.
/// </summary>
public sealed class AutomationViewModel : PageViewModel
{
    private readonly DispatcherTimer _timer;
    private readonly List<AutoCorrectAssignment> _all = new();
    private AutomationStatusDto? _status;
    private AutomationJobDto? _automationJob;
    private string _logRaw = string.Empty;
    private string _search = string.Empty;
    private string _classFilter = AllClasses;
    private AutoCorrectAssignment? _selected;
    private CorrectAllPreviewDto? _preview;
    private int _previewRequest;
    private string _queueSummary = "No jobs yet";

    private const string AllClasses = "All classes";

    // Scheduler settings (Advanced).
    private bool _enabled;
    private int _intervalMinutes = 10;
    private bool _reviewMode = true;
    private bool _draftOnly;
    private bool _attachReport = true;
    private bool _submitGrade = true;
    private bool _returnWork = true;
    private string _reportDetail = "detailed";

    public AutomationViewModel(AppSession session)
        : base(session, "automation", "Auto-correct")
    {
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(20) };
        _timer.Tick += async (_, _) =>
        {
            if (!IsBusy)
            {
                await LoadAsync();
            }
        };

        OpenAssignmentCommand = new RelayCommand(parameter =>
        {
            if ((parameter as AutoCorrectAssignment ?? Selected) is { } assignment)
            {
                Session.OpenAssignment(assignment.Id);
            }
        });
        OpenMonitorCommand = new RelayCommand(() => Session.GoTo("workers"));
        OpenAssignmentsCommand = new RelayCommand(() => Session.GoTo("assignments"));
        // The card's own button. Selecting loads the backend's preview, which
        // is what the review panel and the start button read.
        SelectCommand = new RelayCommand(parameter =>
        {
            if (parameter is AutoCorrectAssignment assignment)
            {
                Selected = assignment;
            }
        });
        // One command instance: RaiseCanExecuteChanged must reach the button.
        StartCommand = Command(StartAsync, () => CanStart);
    }

    // ------------------------------------------------------- choose an assignment

    /// <summary>Real Classroom assignments with the counts the backend holds.
    /// Local-only assignments are excluded: they have no Classroom roster, so a
    /// whole-assignment correction cannot apply to them.</summary>
    public ObservableCollection<AutoCorrectAssignment> Items { get; } = new();

    public ObservableCollection<string> ClassFilters { get; } = new() { AllClasses };

    public RelayCommand OpenAssignmentCommand { get; }
    public RelayCommand OpenMonitorCommand { get; }
    public RelayCommand OpenAssignmentsCommand { get; }
    public RelayCommand SelectCommand { get; }

    /// <summary>Every assignment here is Classroom data, so the shared Sync
    /// control belongs in the header.</summary>
    public override bool SyncsFromClassroom => true;

    public string Search
    {
        get => _search;
        set
        {
            if (SetProperty(ref _search, value))
            {
                ApplyFilter();
            }
        }
    }

    public string ClassFilter
    {
        get => _classFilter;
        set
        {
            var normalized = string.IsNullOrWhiteSpace(value) ? AllClasses : value;
            if (SetProperty(ref _classFilter, normalized))
            {
                ApplyFilter();
            }
        }
    }

    public string ShownText => Items.Count == 1
        ? "1 assignment"
        : $"{Items.Count} assignments";

    public bool IsEmpty => Items.Count == 0;

    public string EmptyTitle => _all.Count == 0
        ? "No assignments yet"
        : "Nothing matches this filter";

    public string EmptyText => _all.Count == 0
        ? "Sync Google Classroom on the Assignments page, then come back: every assignment with a mark scheme can be corrected from here."
        : "Clear the search or pick another class.";

    /// <summary>The assignment whose preview is on screen.</summary>
    public AutoCorrectAssignment? Selected
    {
        get => _selected;
        set
        {
            if (!SetProperty(ref _selected, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasSelection));
            OnPropertyChanged(nameof(ReviewTitle));
            OnPropertyChanged(nameof(CanStart));
            StartCommand.RaiseCanExecuteChanged();

            if (value is null)
            {
                ClearPreview();
                return;
            }

            _ = LoadPreviewAsync(value);
        }
    }

    public bool HasSelection => Selected is not null;

    // ----------------------------------------------------------- review panel

    public string ReviewTitle => Selected is null
        ? "2 · Review before starting"
        : $"{Selected.Title}";

    /// <summary>True once the backend's preview for the selected assignment has
    /// arrived. The start button never appears before that.</summary>
    public bool HasPreview => _preview is not null;

    public bool IsPreviewLoading { get; private set; }

    public string PreviewSummary
    {
        get
        {
            if (_preview is null)
            {
                return Selected is null
                    ? "Choose an assignment above to see exactly what would be corrected."
                    : "Reading what this assignment would correct…";
            }

            var counts = _preview.Counts;
            if (_preview.WillCorrect == 0)
            {
                return $"Nothing to correct: {counts.Submitted} papers were handed in, "
                       + $"{counts.AlreadyCorrected} already corrected, "
                       + $"{counts.Processing} being corrected right now.";
            }

            return $"{_preview.WillCorrect} of {counts.Submitted} handed-in papers would be "
                   + $"corrected now ({counts.Students} students on the roster).";
        }
    }

    /// <summary>The papers that will actually enter the queue, by the name the
    /// backend recorded for the student.</summary>
    public ObservableCollection<string> PreviewPapers { get; } = new();

    /// <summary>Handed-in papers that are deliberately skipped, with the
    /// backend's own reason category.</summary>
    public ObservableCollection<string> SkippedPapers { get; } = new();

    public bool HasSkippedPapers => SkippedPapers.Count > 0;

    /// <summary>Why a correction cannot start (allow-list, mark scheme, …).</summary>
    public ObservableCollection<string> Blockers { get; } = new();

    public bool HasBlockers => Blockers.Count > 0;

    /// <summary>Something the backend could not verify during the preview, in
    /// its own words. Partial data is stated, never silently trusted.</summary>
    public string? SyncWarning => _preview?.SyncWarning;

    public bool HasSyncWarning => !string.IsNullOrWhiteSpace(SyncWarning);

    /// <summary>The backend's gate plus a real number of papers. Both must hold:
    /// a correction with nothing to correct is not offered.</summary>
    public bool CanStart => _preview is { CanStart: true, WillCorrect: > 0 };

    public string StartText => _preview is null or { WillCorrect: 0 }
        ? "Start correcting"
        : $"Start correcting {_preview.WillCorrect} paper{(_preview.WillCorrect == 1 ? string.Empty : "s")}";

    public AsyncCommand StartCommand { get; }

    public AsyncCommand ReviewAgainCommand => Command(async () =>
    {
        if (Selected is not null)
        {
            await LoadPreviewAsync(Selected);
        }
    });

    /// <summary>What happened last time: the real enqueued count, not a
    /// generic success banner.</summary>
    public string ResultText { get; private set; } = string.Empty;

    public bool HasResult => ResultText.Length > 0;

    // ------------------------------------------------------------- queue state

    public string QueueSummary
    {
        get => _queueSummary;
        private set => SetProperty(ref _queueSummary, value);
    }

    public string QueueDetail => "The queue is the backend's own job list. Open Correction monitor to watch each paper.";

    // ------------------------------------------------- advanced: the scheduler

    public ObservableCollection<AutomationJobDto> AutomationJobs { get; } = new();

    public AutomationJobDto? SelectedAutomationJob
    {
        get => _automationJob;
        set
        {
            if (SetProperty(ref _automationJob, value))
            {
                OnPropertyChanged(nameof(HasSelectedAutomationJob));
            }
        }
    }

    public bool HasSelectedAutomationJob => _automationJob is not null;

    public ObservableCollection<int> Intervals { get; } = new() { 5, 10, 15, 30 };
    public ObservableCollection<ChoiceOption> ReportDetails { get; } = new()
    {
        new("Summary", "summary"),
        new("Standard", "standard"),
        new("Detailed", "detailed"),
    };

    public string WorkerText => _status is null
        ? "Background worker state unknown"
        : (_status.WorkerEnabled ? "Background worker running" : "Background worker stopped")
          + $" · checks every {_status.IntervalMinutes} min · last check {_status.LastCheckText}";

    public bool HasAutomationJobs => AutomationJobs.Count > 0;

    public bool Enabled
    {
        get => _enabled;
        set => SetProperty(ref _enabled, value);
    }

    /// <summary>Kept nullable: a ComboBox writes null back while its item list is
    /// rebuilt, and a non-nullable int rejects that write.</summary>
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

    public bool IsEmptyAutomation => AutomationJobs.Count == 0;

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

    public AsyncCommand SaveSettingsCommand => Command(async () =>
    {
        if (IntervalMinutes is null or <= 0)
        {
            Error = "Choose how often the background check runs: the backend will not accept 0 minutes.";
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
        await ReloadAdvancedAsync();
        Status = "Background check settings saved.";
    });

    public AsyncCommand ToggleWorkerCommand => Command(async () =>
    {
        await Session.Api.SetAutomationWorkerAsync(!(_status?.WorkerEnabled ?? false));
        await ReloadAdvancedAsync();
        Status = "Background worker state updated.";
    });

    public AsyncCommand RunNowCommand => Command(async () =>
    {
        if (!Session.Dialogs.Confirm("Check for new work now",
                "Scan Classroom once and advance anything already waiting in the background queue?\n\n"
                + "This is the scheduler's own scan; it does not correct an assignment you have not allowed."))
        {
            return;
        }

        await Session.Api.RunAutomationNowAsync();
        await LoadAsync();
        Status = "Background check finished; pending jobs advanced.";
    });

    public AsyncCommand ReconcileCommand => Command(async () =>
    {
        await Session.Api.ReconcileAutomationAsync();
        await LoadAsync();
        Status = "Background jobs re-checked against Google Classroom.";
    });

    public AsyncCommand ApproveCommand => Command(async () =>
    {
        if (SelectedAutomationJob is null)
        {
            return;
        }

        await Session.Api.ApproveAutomationJobAsync(SelectedAutomationJob.Id);
        await LoadAsync();
        Status = "Queued job released for correction.";
    });

    public AsyncCommand RetryAutomationCommand => Command(async () =>
    {
        if (SelectedAutomationJob is null)
        {
            return;
        }

        await Session.Api.RetryAutomationJobAsync(SelectedAutomationJob.Id);
        await LoadAsync();
        Status = "Failed background job retried.";
    });

    public AsyncCommand ClearLogCommand => Command(async () =>
    {
        if (!Session.Dialogs.Confirm("Clear activity log", "Clear the background worker's activity log?"))
        {
            return;
        }

        await Session.Api.ClearAutomationLogAsync();
        await ReloadAdvancedAsync();
    });

    // ------------------------------------------------------------------ loading

    protected override async Task LoadCoreAsync()
    {
        var assignments = await Session.Api.AssignmentsAsync();
        var jobs = await Session.Api.JobsAsync(limit: 200);

        _all.Clear();
        foreach (var assignment in assignments.Assignments)
        {
            if (assignment.Local || assignment.DeletedInClassroom)
            {
                continue;
            }

            _all.Add(new AutoCorrectAssignment(assignment));
        }

        RebuildClassFilters();
        ApplyFilter();
        UpdateQueue(jobs);

        var previous = Selected?.Id;
        if (previous is not null)
        {
            Selected = _all.FirstOrDefault(a => a.Id == previous);
        }

        await ReloadAdvancedAsync();
        OnPropertyChanged(nameof(IsEmpty));
        OnPropertyChanged(nameof(EmptyTitle));
        OnPropertyChanged(nameof(EmptyText));
    }

    private async Task ReloadAdvancedAsync()
    {
        _status = await Session.Api.AutomationStatusAsync();
        var jobs = await Session.Api.AutomationJobsAsync(limit: 200);
        var log = await Session.Api.AutomationLogAsync();

        AutomationJobs.Clear();
        foreach (var job in jobs.Jobs)
        {
            AutomationJobs.Add(job);
        }

        OnPropertyChanged(nameof(HasAutomationJobs));
        OnPropertyChanged(nameof(IsEmptyAutomation));

        Enabled = _status.Enabled;
        ReviewMode = _status.ReviewMode;
        DraftOnly = _status.DraftOnly;
        AttachReport = _status.AttachReport;
        SubmitGrade = _status.SubmitGrade;
        ReturnWork = _status.ReturnWork;
        ReportDetail = _status.ReportDetail ?? "detailed";

        // The list must exist before the value is applied: clearing it makes the
        // ComboBox write null back through SelectedItem.
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
        OnPropertyChanged(nameof(IntervalMinutes));
        OnPropertyChanged(nameof(WorkerText));

        FillLog(log);
    }

    private void UpdateQueue(JobListDto jobs)
    {
        var queued = jobs.Jobs.Count(j => j.IsQueued);
        var active = jobs.Jobs.Count(j => j.IsActive && !j.IsQueued);
        var completed = jobs.Jobs.Count(j => j.IsCompleted);
        var failed = jobs.Jobs.Count(j => j.IsFailed);
        QueueSummary = $"Queue: {queued} waiting · {active} in progress · "
                       + $"{completed} done · {failed} failed";
        Status = QueueSummary;
        OnPropertyChanged(nameof(QueueSummary));
    }

    // ------------------------------------------------------------- the preview

    public async Task LoadPreviewAsync(AutoCorrectAssignment assignment)
    {
        var request = ++_previewRequest;
        ClearPreview();
        IsPreviewLoading = true;
        OnPropertyChanged(nameof(IsPreviewLoading));
        OnPropertyChanged(nameof(PreviewSummary));

        try
        {
            var preview = await Session.Api.CorrectAllPreviewAsync(assignment.Id);
            if (request != _previewRequest)
            {
                return;
            }

            _preview = preview;
            FillPreview(preview);
        }
        catch (ApiException ex)
        {
            if (request != _previewRequest)
            {
                return;
            }

            Blockers.Clear();
            Blockers.Add(ex.Display);
            OnPropertyChanged(nameof(HasBlockers));
            Error = ex.Display;
        }
        catch (Exception ex)
        {
            Session.Log.Warn("assignment preview failed: " + ex.Message);
            if (request == _previewRequest)
            {
                Error = UserMessage.For(ex);
            }
        }
        finally
        {
            if (request == _previewRequest)
            {
                IsPreviewLoading = false;
                OnPropertyChanged(nameof(IsPreviewLoading));
                OnPropertyChanged(nameof(HasPreview));
                OnPropertyChanged(nameof(PreviewSummary));
                OnPropertyChanged(nameof(CanStart));
                OnPropertyChanged(nameof(StartText));
                StartCommand.RaiseCanExecuteChanged();
            }
        }
    }

    private void FillPreview(CorrectAllPreviewDto preview)
    {
        PreviewPapers.Clear();
        foreach (var student in preview.Eligible)
        {
            PreviewPapers.Add(string.IsNullOrWhiteSpace(student.StudentName)
                ? "A paper with no student name recorded"
                : student.StudentName!);
        }

        SkippedPapers.Clear();
        var counts = preview.Counts;
        void Skipped(int count, string reason)
        {
            if (count > 0)
            {
                SkippedPapers.Add($"{count} {reason}");
            }
        }

        Skipped(counts.AlreadyCorrected, "already corrected (use Job history to re-mark one)");
        Skipped(counts.NeedsReview, "corrected but still awaiting a decision");
        Skipped(counts.Processing, "being corrected right now");
        Skipped(counts.Invalid, "handed in but the file cannot be read");
        Skipped(counts.Draft, "opened but never handed in");
        Skipped(counts.Missing, "never handed in");
        Skipped(counts.Unknown, "with a submission the backend could not verify");

        Blockers.Clear();
        foreach (var blocker in preview.Blockers)
        {
            Blockers.Add(blocker);
        }

        if (!preview.Allowlisted)
        {
            Blockers.Add("Allow it first: People → Classroom → “Allow-list assignment”.");
        }

        if (!preview.HasMarkScheme)
        {
            Blockers.Add("Upload the mark scheme on People → Classroom, then review again.");
        }

        OnPropertyChanged(nameof(HasBlockers));
        OnPropertyChanged(nameof(HasSkippedPapers));
        OnPropertyChanged(nameof(HasSyncWarning));
        OnPropertyChanged(nameof(CanStart));
        OnPropertyChanged(nameof(StartText));
        OnPropertyChanged(nameof(PreviewSummary));
        OnPropertyChanged(nameof(HasPreview));
        StartCommand.RaiseCanExecuteChanged();
    }

    private void ClearPreview()
    {
        _preview = null;
        PreviewPapers.Clear();
        SkippedPapers.Clear();
        Blockers.Clear();
        OnPropertyChanged(nameof(HasPreview));
        OnPropertyChanged(nameof(HasBlockers));
        OnPropertyChanged(nameof(HasSkippedPapers));
        OnPropertyChanged(nameof(HasSyncWarning));
        OnPropertyChanged(nameof(CanStart));
        OnPropertyChanged(nameof(StartText));
        OnPropertyChanged(nameof(PreviewSummary));
        StartCommand.RaiseCanExecuteChanged();
    }

    // --------------------------------------------------------------- the action

    private async Task StartAsync()
    {
        if (Selected is null || _preview is null)
        {
            return;
        }

        var preview = _preview;
        var assignment = Selected;

        // The confirmation repeats the backend's own numbers, and says what is
        // skipped. Nothing is started without this answer.
        var message =
            $"Assignment: {assignment.Title}\n"
            + $"Class: {assignment.ClassText}\n\n"
            + $"Papers to correct: {preview.WillCorrect}\n"
            + $"Already corrected (skipped): {preview.Counts.AlreadyCorrected}\n"
            + $"Being corrected (skipped): {preview.Counts.Processing}\n"
            + $"Never handed in (skipped): {preview.Counts.Missing + preview.Counts.Draft}\n\n"
            + "Classroom is checked first, then each paper is queued separately: "
            + "one failure never stops the rest. Progress appears in Correction monitor.";
        if (!Session.Dialogs.Confirm("Start correcting?", message))
        {
            return;
        }

        // The command's own RunAsync owns the busy flag and reports failures;
        // this method only decides what to call and what the outcome was.
        var result = await Session.Api.CorrectAllAsync(
            assignment.CourseId ?? preview.CourseId ?? string.Empty,
            assignment.Id, refresh: true);

        ResultText = Describe(result, preview);
        OnPropertyChanged(nameof(ResultText));
        OnPropertyChanged(nameof(HasResult));
        Status = ResultText;

        var jobs = await Session.Api.JobsAsync(limit: 200);
        UpdateQueue(jobs);
        await LoadPreviewAsync(assignment);
    }

    /// <summary>What the backend actually queued, said plainly. A partial run is
    /// reported as partial: the numbers come from the response, not from hope.</summary>
    private static string Describe(JsonElement raw, CorrectAllPreviewDto preview)
    {
        int Get(string name) =>
            raw.ValueKind == JsonValueKind.Object && raw.TryGetProperty(name, out var value)
                && value.TryGetInt32(out var number) ? number : 0;

        int CountArray(string name) =>
            raw.ValueKind == JsonValueKind.Object && raw.TryGetProperty(name, out var value)
                && value.ValueKind == JsonValueKind.Array ? value.GetArrayLength() : 0;

        var enqueued = Get("enqueued");
        var duplicates = CountArray("duplicates");
        var failed = CountArray("enqueue_failed");

        var text = enqueued == 0
            ? "Nothing was queued."
            : $"Queued {enqueued} paper{(enqueued == 1 ? string.Empty : "s")}.";

        if (duplicates > 0)
        {
            text += $" {duplicates} were already in the queue.";
        }

        if (failed > 0)
        {
            text += $" {failed} could not be queued — open Correction monitor for the error.";
        }

        var skipped = preview.Counts.Missing + preview.Counts.Draft;
        if (skipped > 0)
        {
            text += $" {skipped} skipped because nothing was handed in.";
        }

        return text;
    }

    // ---------------------------------------------------------------- filtering

    private void RebuildClassFilters()
    {
        var wanted = new List<string> { AllClasses };
        wanted.AddRange(_all.Select(a => a.ClassText)
            .Where(label => label.Length > 0 && label != "—")
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .OrderBy(label => label, StringComparer.OrdinalIgnoreCase));

        for (var i = ClassFilters.Count - 1; i >= 0; i--)
        {
            if (!wanted.Contains(ClassFilters[i]))
            {
                ClassFilters.RemoveAt(i);
            }
        }

        for (var i = 0; i < wanted.Count; i++)
        {
            if (i >= ClassFilters.Count)
            {
                ClassFilters.Add(wanted[i]);
            }
            else if (!string.Equals(ClassFilters[i], wanted[i], StringComparison.Ordinal))
            {
                ClassFilters.Insert(i, wanted[i]);
            }
        }

        var restored = ClassFilters.Contains(_classFilter) ? _classFilter : AllClasses;
        _classFilter = restored;
        OnPropertyChanged(nameof(ClassFilter));
    }

    private void ApplyFilter()
    {
        var needle = Search.Trim();
        Items.Clear();
        foreach (var assignment in _all)
        {
            if (!string.Equals(ClassFilter, AllClasses, StringComparison.Ordinal)
                && !string.Equals(assignment.ClassText, ClassFilter, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (needle.Length > 0
                && assignment.Title.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0
                && assignment.ClassText.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
            {
                continue;
            }

            Items.Add(assignment);
        }

        OnPropertyChanged(nameof(ShownText));
        OnPropertyChanged(nameof(IsEmpty));
        OnPropertyChanged(nameof(EmptyTitle));
        OnPropertyChanged(nameof(EmptyText));
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
            LogLines.Add("No background activity yet. Runs appear here after each scheduled check.");
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
}

/// <summary>
/// One assignment on the Auto-correct page: the backend's own counts, phrased
/// the way a teacher asks the question. Nothing here is invented — a missing
/// number stays blank rather than becoming a zero that looks like a fact.
/// </summary>
public sealed class AutoCorrectAssignment
{
    public AutoCorrectAssignment(AssignmentDto dto)
    {
        Dto = dto;
    }

    public AssignmentDto Dto { get; }

    public string Id => Dto.Id;

    public string? CourseId => Dto.CourseId;

    public string Title => string.IsNullOrWhiteSpace(Dto.DisplayTitle) ? "Untitled assignment" : Dto.DisplayTitle;

    public string ClassText => string.IsNullOrWhiteSpace(Dto.CourseName)
        ? "Class not recorded"
        : Dto.CourseLabel;

    public string DueText => string.IsNullOrWhiteSpace(Dto.DueDate)
        ? "No due date"
        : "Due " + Dto.DueDate;

    public int Students => Dto.StudentsTotal;

    public int Submitted => Dto.Submitted;

    public int Awaiting => Math.Max(0, Dto.Submitted - Dto.Checked - Dto.Processing);

    public string RosterText => Students == 1 ? "1 student" : $"{Students} students";

    public string SubmittedText => $"{Submitted} handed in";

    public string AwaitingText => $"{Awaiting} to correct";

    public string ProcessingText => $"{Dto.Processing} in progress";

    public bool HasProcessing => Dto.Processing > 0;

    public string CorrectedText => $"{Dto.Checked} corrected";

    public string FailedText => $"{Dto.Failed} failed";

    public string SchemeText => Dto.HasMarkScheme ? "Mark scheme ready" : "No mark scheme";

    public bool CanCorrect => Dto.HasMarkScheme && Dto.Allowlisted != 0;

    /// <summary>The one sentence that says whether this can be corrected right
    /// now, in the teacher's terms.</summary>
    public string ReadyText
    {
        get
        {
            if (Dto.Allowlisted == 0 && !Dto.HasMarkScheme)
            {
                return "Not ready: no mark scheme and not allow-listed";
            }

            if (!Dto.HasMarkScheme)
            {
                return "Not ready: upload the mark scheme";
            }

            if (Dto.Allowlisted == 0)
            {
                return "Not ready: allow-list it in Classroom";
            }

            return Awaiting > 0
                ? $"{Awaiting} paper{(Awaiting == 1 ? string.Empty : "s")} ready to correct"
                : "Nothing waiting to be corrected";
        }
    }

    public bool IsReady => CanCorrect && Awaiting > 0;

    public bool IsNotReady => !CanCorrect;

    public string OpenText => "Open details";

    public override string ToString() => Title;
}
