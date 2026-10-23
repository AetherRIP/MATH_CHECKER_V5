using System.Collections.ObjectModel;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One tab inside a student profile (Overview, Assignments, …).</summary>
public sealed class ProfileSectionVm
{
    public ProfileSectionVm(string key, string title)
    {
        Key = key;
        Title = title;
    }

    public string Key { get; }
    public string Title { get; }

    /// <summary>UI Automation and the SpaceTabs item template both read this.</summary>
    public override string ToString() => Title;
}

/// <summary>One score in the small progress chart.</summary>
public sealed class ScorePointVm
{
    public ScorePointVm(string label, double percentage)
    {
        Label = label;
        Percentage = percentage;
        // A 0% bar is still visible; 72 DIP is a full bar.
        BarHeight = Math.Clamp(percentage, 4, 100) / 100 * 72;
    }

    public string Label { get; }
    public double Percentage { get; }
    public double BarHeight { get; }
    public string ValueText => $"{Percentage:0.#}%";
}

/// <summary>Aggregated question marks for one topic across stored corrections.</summary>
public sealed class TopicPerformanceVm
{
    public TopicPerformanceVm(string topic, double earned, double available, int questions)
    {
        Topic = topic;
        Earned = earned;
        Available = available;
        Questions = questions;
        Percentage = available > 0 ? earned / available * 100 : 0;
        BarHeight = Math.Clamp(Percentage, 3, 100) / 100 * 64;
    }

    public string Topic { get; }
    public double Earned { get; }
    public double Available { get; }
    public int Questions { get; }
    public double Percentage { get; }
    public double BarHeight { get; }

    public string ValueText => $"{Earned:0.#}/{Available:0.#} · {Percentage:0.#}%";

    public string QuestionText => Questions == 1 ? "1 question" : $"{Questions} questions";
}

/// <summary>
/// The Student Profile: identity, academic summary, assignment history,
/// correction detail, progress and the teacher's local details — all from
/// <c>GET /api/students/{id}</c> and the stored corrections it names.
///
/// It is deliberately not a <see cref="PageViewModel"/>: it lives inside the
/// Students page (a selected student replaces the list) and raises events for
/// the page to close it or refetch, which keeps one navigation model.
/// </summary>
public sealed class StudentProfileViewModel : ObservableObject
{
    private const int MaxDetailRequests = 10;
    private const int MaxTrendPoints = 12;

    private readonly AppSession _session;
    private readonly Dictionary<string, ResultDetailDto> _details = new(StringComparer.Ordinal);
    private readonly SemaphoreSlim _detailGate = new(1, 1);
    private StudentProfileDto _profile;

    private ProfileSectionVm? _selectedSection;
    private StudentAssignmentDto? _selectedAssignment;
    private StudentCorrectionDto? _selectedCorrection;
    private string _assignmentFilter = AssignmentFiltersList[0];
    private bool _isEditingDetails;
    private string _nicknameDraft = string.Empty;
    private string _classDraft = string.Empty;
    private string _status = string.Empty;
    private string? _error;
    private bool _isBusy;
    private bool _detailsLoaded;
    private string? _detailLoadError;

    private static readonly string[] AssignmentFiltersList =
    {
        "All", "Submitted", "Outstanding", "Late", "Corrected",
    };

    public StudentProfileViewModel(AppSession session, StudentProfileDto profile)
    {
        _session = session;
        _profile = profile;

        Sections = new[]
        {
            new ProfileSectionVm("overview", "Overview"),
            new ProfileSectionVm("assignments", "Assignments"),
            new ProfileSectionVm("corrections", "Corrections"),
            new ProfileSectionVm("progress", "Progress"),
            new ProfileSectionVm("details", "Details"),
        };
        _selectedSection = Sections[0];

        BackCommand = new RelayCommand(() => BackRequested?.Invoke());
        RefreshCommand = new AsyncCommand(async () =>
        {
            if (ReloadRequested is { } reload)
            {
                await reload().ConfigureAwait(true);
            }
        }, onError: HandleError);
        ReportCommand = new AsyncCommand(ExportReportAsync, onError: HandleError);
        OpenResultsCommand = new RelayCommand(() => _session.GoTo("results"));
        BeginEditCommand = new RelayCommand(BeginEdit);
        CancelEditCommand = new RelayCommand(CancelEdit);
        SaveDetailsCommand = new AsyncCommand(SaveDetailsAsync, onError: HandleError);
        OpenSelectedInDeskCommand = new AsyncCommand(OpenSelectedInDeskAsync,
            () => SelectedCorrection is not null, HandleError);
        OpenAssignmentCorrectionCommand = new AsyncCommand(OpenAssignmentCorrectionAsync,
            () => SelectedAssignment?.Result?.Id is not null, HandleError);
        ExportAssignmentReportCommand = new AsyncCommand(ExportAssignmentReportAsync,
            () => SelectedAssignment?.Result is not null, HandleError);
        ExportCorrectionReportCommand = new AsyncCommand(ExportCorrectionReportAsync,
            () => SelectedCorrection?.CourseworkId is not null, HandleError);

        ApplyCorrectionTitles();
        RebuildAssignments();
        RebuildCorrections();
        // The trend needs only the profile's own percentages, not the question
        // detail, so it is available immediately.
        RebuildTrend();
        // Assign the field, not the property: constructing a profile must not
        // fire a request before the user has even opened a tab.
        _selectedCorrection = Corrections.FirstOrDefault();
    }

    public event Action? BackRequested;

    /// <summary>Raised for the "Refresh" action; the page refetches the profile
    /// so a stale panel can never outlive a backend change.</summary>
    public event Func<Task>? ReloadRequested;

    /// <summary>Raised after a successful save so the student list can pick up
    /// the new class label (the list owns that row).</summary>
    public event Action? ProfileSaved;

    public StudentProfileDto Profile => _profile;

    public IReadOnlyList<ProfileSectionVm> Sections { get; }

    public ProfileSectionVm? SelectedSection
    {
        get => _selectedSection;
        set
        {
            if (!SetProperty(ref _selectedSection, value) || value is null)
            {
                return;
            }

            OnPropertyChanged(nameof(IsOverview));
            OnPropertyChanged(nameof(IsAssignments));
            OnPropertyChanged(nameof(IsCorrections));
            OnPropertyChanged(nameof(IsProgress));
            OnPropertyChanged(nameof(IsDetails));

            // Topic figures and question detail share one cached batch of
            // result reads; opening the tabs that need it triggers it once.
            if (value.Key is "overview" or "corrections" or "progress")
            {
                _ = EnsureDetailsAsync();
            }

            if (value.Key == "corrections")
            {
                if (SelectedCorrection is null)
                {
                    SelectedCorrection = Corrections.FirstOrDefault();
                }
                else
                {
                    // The cached batch may already hold this correction; if it
                    // does, showing it is synchronous and costs no request.
                    _ = LoadSelectedCorrectionAsync();
                }
            }
        }
    }

    public bool IsOverview => SelectedSection?.Key == "overview";
    public bool IsAssignments => SelectedSection?.Key == "assignments";
    public bool IsCorrections => SelectedSection?.Key == "corrections";
    public bool IsProgress => SelectedSection?.Key == "progress";
    public bool IsDetails => SelectedSection?.Key == "details";

    public ObservableCollection<StudentAssignmentDto> Assignments { get; } = new();
    public ObservableCollection<StudentCorrectionDto> Corrections { get; } = new();
    public ObservableCollection<QuestionMarkDto> Questions { get; } = new();
    public ObservableCollection<TopicPerformanceVm> Topics { get; } = new();
    public ObservableCollection<ScorePointVm> Trend { get; } = new();
    public List<string> ClassOptions { get; private set; } = new();
    public IReadOnlyList<string> AssignmentFilters => AssignmentFiltersList;

    public string AssignmentFilter
    {
        get => _assignmentFilter;
        set
        {
            // A blank or unknown value would leave the combo box showing
            // nothing while the list silently showed everything. Normalise to
            // "All" so the control and the data always agree.
            var normalized = string.IsNullOrWhiteSpace(value) ? "All" : value;
            if (Array.IndexOf(AssignmentFiltersList, normalized) < 0)
            {
                normalized = "All";
            }

            if (!SetProperty(ref _assignmentFilter, normalized))
            {
                return;
            }

            RebuildAssignments();
        }
    }

    public StudentAssignmentDto? SelectedAssignment
    {
        get => _selectedAssignment;
        set
        {
            if (!SetProperty(ref _selectedAssignment, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasSelectedAssignment));
            OnPropertyChanged(nameof(SelectedAssignmentHint));
            OpenAssignmentCorrectionCommand.RaiseCanExecuteChanged();
            ExportAssignmentReportCommand.RaiseCanExecuteChanged();
        }
    }

    public bool HasSelectedAssignment => SelectedAssignment is not null;

    public string SelectedAssignmentHint => SelectedAssignment is null
        ? "Select an assignment to open its correction."
        : SelectedAssignment.ReportHint;

    public StudentCorrectionDto? SelectedCorrection
    {
        get => _selectedCorrection;
        set
        {
            if (!SetProperty(ref _selectedCorrection, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasSelectedCorrection));
            OnPropertyChanged(nameof(SelectedCorrectionTitle));
            OnPropertyChanged(nameof(SelectedCorrectionScore));
            OnPropertyChanged(nameof(SelectedCorrectionStatus));
            OnPropertyChanged(nameof(SelectedCorrectionWhen));
            OpenSelectedInDeskCommand.RaiseCanExecuteChanged();
            ExportCorrectionReportCommand.RaiseCanExecuteChanged();
            _ = LoadSelectedCorrectionAsync();
        }
    }

    public bool HasSelectedCorrection => SelectedCorrection is not null;

    public string SelectedCorrectionTitle => SelectedCorrection?.TitleText ?? "Correction";

    public string SelectedCorrectionScore => SelectedCorrection is null
        ? "—"
        : $"{SelectedCorrection.ScoreText}  ({SelectedCorrection.PercentageText})";

    public string SelectedCorrectionStatus => SelectedCorrection?.StatusText ?? "—";

    public string SelectedCorrectionWhen => SelectedCorrection?.CreatedText ?? "—";

    public string CorrectionDetailNote { get; private set; } =
        "Select a correction to see its question breakdown.";

    public bool HasCorrectionDetailNote => !string.IsNullOrWhiteSpace(CorrectionDetailNote);

    public bool QuestionsEmpty => Questions.Count == 0;

    // ------------------------------------------------------ summary figures
    // Every value below is copied from the backend's own numbers. A missing
    // figure shows an em dash; it is never coerced to 0 or to a percentage.

    public string AssignmentsText => _profile.Classroom.Available
        ? _profile.Classroom.AssignmentsTotal.ToString()
        : "—";

    public string SubmittedText => _profile.Classroom.Available
        ? $"{_profile.Classroom.Submitted}/{_profile.Classroom.AssignmentsTotal}"
        : "—";

    public string OutstandingText => _profile.Classroom.Available
        ? _profile.Classroom.NotSubmitted.ToString()
        : "—";

    public string CorrectionsValueText => _profile.Corrections.Total.ToString();

    public string McAverageText => _profile.Performance.McAverage is null
        ? "No corrections yet"
        : ProfileText.Percent(_profile.Performance.McAverage);

    public string OverallText => _profile.Performance.TotalAvailable > 0
        ? $"{ProfileText.Mark(_profile.Performance.TotalEarned)} / "
          + $"{ProfileText.Mark(_profile.Performance.TotalAvailable)}"
          + (_profile.Performance.OverallPercentage is null
              ? string.Empty
              : $" ({ProfileText.Percent(_profile.Performance.OverallPercentage)})")
        : "—";

    public string ClassroomAverageText => _profile.Performance.ClassroomAverage is null
        ? "Not graded"
        : ProfileText.Percent(_profile.Performance.ClassroomAverage);

    public string ClassroomSpreadText => _profile.Performance.ClassroomHighest is null
        ? "Not available"
        : $"{ProfileText.Percent(_profile.Performance.ClassroomHighest)} / "
          + ProfileText.Percent(_profile.Performance.ClassroomLowest);

    public string ClassroomGradedText =>
        $"{_profile.Performance.ClassroomGraded} graded · "
        + $"{_profile.Performance.ClassroomNotGraded} not graded";

    public string McSpreadText => _profile.Performance.McHighest is null
        ? "Not available"
        : $"{ProfileText.Percent(_profile.Performance.McHighest)} / "
          + ProfileText.Percent(_profile.Performance.McLowest);

    public string ZerosText =>
        $"{_profile.Performance.ClassroomZeros} Classroom · {_profile.Performance.McZeros} Math Checker";

    public string PendingText => _profile.Corrections.Pending.ToString();

    public string NeedsReviewText => _profile.Corrections.NeedsReview.ToString();

    public string OnTimeText => _profile.Behavior.OnTime.ToString();

    public string LateCountText => _profile.Behavior.Late.ToString();

    public string MissingCountText => _profile.Behavior.Missing.ToString();

    public string DraftText => _profile.Behavior.Draft.ToString();

    public string TurnedInText => _profile.Behavior.TurnedIn.ToString();

    public string ReturnedText => _profile.Behavior.Returned.ToString();

    public string CorrectedCountText => _profile.Performance.Corrected.ToString();

    public string FailedJobsText => _profile.Corrections.Failed.ToString();

    public string LastCorrectionText => _profile.Corrections.LastCorrectedAt is null
        ? "Never corrected"
        : EpochTime.Format(_profile.Corrections.LastCorrectedAt);

    /// <summary>Every stored correction, newest first. Older backends only
    /// send the 10 newest, so the list degrades to that instead of going blank.</summary>
    private IReadOnlyList<StudentCorrectionDto> AllCorrections =>
        _profile.Corrections.History.Count > 0
            ? _profile.Corrections.History
            : _profile.Corrections.Recent;

    public bool ShowTrackingWarning => !_profile.Classroom.Available;

    public string TrackingWarning =>
        "No Google Classroom data for this student yet. Submission and outstanding "
        + "figures are hidden rather than guessed — sync the class in Classroom to fill them in.";

    public string CorrectionCountText
    {
        get
        {
            var total = _profile.Corrections.Total;
            if (total == 0)
            {
                return "No corrections yet";
            }

            var shown = Corrections.Count;
            var list = shown < total
                ? $"{shown} of {total} stored corrections"
                : $"{total} stored correction{(total == 1 ? string.Empty : "s")}";
            return list + (_profile.Corrections.NeedsReview > 0
                ? $" · {_profile.Corrections.NeedsReview} need review"
                : string.Empty);
        }
    }

    public bool HasTopics => Topics.Count > 0;
    public bool HasTrend => Trend.Count >= 2;
    public bool HasRepeatedDifficulties { get; private set; }

    public string TrendNote
    {
        get
        {
            if (Trend.Count == 0)
            {
                return "No stored corrections yet, so there is no trend to show.";
            }

            if (Trend.Count == 1)
            {
                return "One correction so far — a trend needs at least two.";
            }

            return Trend.Count < _profile.Corrections.Total
                ? $"Percentages from the {Trend.Count} most recent of "
                  + $"{_profile.Corrections.Total} stored corrections, oldest first."
                : $"Percentages from all {Trend.Count} stored corrections, oldest first.";
        }
    }

    public string TopicNote => _detailLoadError
        ?? (HasTopics
            ? $"Topic marks aggregate the question breakdowns of the "
              + $"{Math.Min(MaxDetailRequests, _profile.Corrections.Recent.Count)} most recent "
              + "corrections; unclassified questions are reported as Unclassified."
            : "No topic breakdown was stored in the recent corrections, so topic performance cannot be shown.");

    public string StrengthText { get; private set; } = "Not enough topic data yet.";
    public string FocusText { get; private set; } = "Not enough topic data yet.";
    public string RepeatedText { get; private set; } = "No repeated difficulty detected in the available data.";

    public bool IsEditingDetails
    {
        get => _isEditingDetails;
        private set
        {
            if (SetProperty(ref _isEditingDetails, value))
            {
                OnPropertyChanged(nameof(IsReadingDetails));
            }
        }
    }

    public bool IsReadingDetails => !IsEditingDetails;

    public string NicknameDraft
    {
        get => _nicknameDraft;
        set => SetProperty(ref _nicknameDraft, value);
    }

    public string ClassDraft
    {
        get => _classDraft;
        set => SetProperty(ref _classDraft, value);
    }

    public bool IsBusy
    {
        get => _isBusy;
        private set => SetProperty(ref _isBusy, value);
    }

    public string? Error
    {
        get => _error;
        private set
        {
            if (SetProperty(ref _error, value))
            {
                OnPropertyChanged(nameof(HasError));
            }
        }
    }

    public bool HasError => !string.IsNullOrWhiteSpace(Error);

    public string Status
    {
        get => _status;
        private set => SetProperty(ref _status, value);
    }

    public RelayCommand BackCommand { get; }
    public AsyncCommand RefreshCommand { get; }
    public AsyncCommand ReportCommand { get; }
    public RelayCommand OpenResultsCommand { get; }
    public RelayCommand BeginEditCommand { get; }
    public RelayCommand CancelEditCommand { get; }
    public AsyncCommand SaveDetailsCommand { get; }
    public AsyncCommand OpenSelectedInDeskCommand { get; }
    public AsyncCommand OpenAssignmentCorrectionCommand { get; }
    public AsyncCommand ExportAssignmentReportCommand { get; }
    public AsyncCommand ExportCorrectionReportCommand { get; }

    // ------------------------------------------------------------------ load

    /// <summary>Loads the class vocabulary and the cached correction details.
    /// Both are optional enrichments: a failure is reported, never fatal.</summary>
    public async Task LoadAsync()
    {
        try
        {
            var classes = await _session.Api.StudentClassesAsync().ConfigureAwait(true);
            ClassOptions = classes.Classes ?? new List<string>();
            OnPropertyChanged(nameof(ClassOptions));
        }
        catch (Exception ex)
        {
            _session.Log.Warn("student class vocabulary unavailable: " + ex.Message);
        }

        await EnsureDetailsAsync().ConfigureAwait(true);
    }

    /// <summary>Reads the stored corrections ONCE (max 10) and keeps them for
    /// question detail and topic figures. Re-entry is serialised so selecting
    /// tabs quickly cannot fire duplicate requests.</summary>
    public async Task EnsureDetailsAsync()
    {
        if (_detailsLoaded || _detailGate.CurrentCount == 0)
        {
            return;
        }

        await _detailGate.WaitAsync().ConfigureAwait(true);
        try
        {
            if (_detailsLoaded)
            {
                return;
            }

            IsBusy = true;
            _detailLoadError = null;
            var failures = 0;
            var ids = _profile.Corrections.Recent
                .Where(c => !string.IsNullOrWhiteSpace(c.Id) && !_details.ContainsKey(c.Id))
                .Take(MaxDetailRequests)
                .Select(c => c.Id)
                .ToList();

            var reads = ids.Select(async id =>
            {
                try
                {
                    var detail = await _session.Api.ResultAsync(id).ConfigureAwait(true);
                    _details[id] = detail;
                }
                catch (Exception ex)
                {
                    failures++;
                    _session.Log.Warn($"correction {id} unavailable for the profile: {ex.Message}");
                }
            });
            await Task.WhenAll(reads).ConfigureAwait(true);

            if (failures > 0)
            {
                _detailLoadError = failures == 1
                    ? "One stored correction could not be read; its questions are missing here."
                    : $"{failures} stored corrections could not be read; their questions are missing here.";
            }

            _detailsLoaded = true;
            RebuildInsights();
            FillQuestions();
            OnPropertyChanged(nameof(TopicNote));
        }
        finally
        {
            IsBusy = false;
            _detailGate.Release();
        }
    }

    private async Task LoadSelectedCorrectionAsync()
    {
        var id = SelectedCorrection?.Id;
        if (string.IsNullOrWhiteSpace(id))
        {
            Questions.Clear();
            CorrectionDetailNote = "Select a correction to see its question breakdown.";
            NotifyQuestions();
            return;
        }

        if (_details.TryGetValue(id, out var cached))
        {
            ShowDetail(cached);
            return;
        }

        // Do not leave the previous correction's questions under the new
        // selection while its detail is in flight.
        Questions.Clear();
        CorrectionDetailNote = "Loading this correction's question breakdown…";
        NotifyQuestions();

        try
        {
            IsBusy = true;
            var detail = await _session.Api.ResultAsync(id).ConfigureAwait(true);
            _details[id] = detail;
            if (SelectedCorrection?.Id == id)
            {
                ShowDetail(detail);
            }

            RebuildInsights();
        }
        catch (ApiException ex) when (ex.IsNotFound)
        {
            Questions.Clear();
            CorrectionDetailNote = "The stored correction no longer exists.";
            NotifyQuestions();
        }
        catch (Exception ex)
        {
            Questions.Clear();
            CorrectionDetailNote = "Could not load this correction: " + ex.Message;
            NotifyQuestions();
            _session.Log.Warn("correction detail unavailable: " + ex.Message);
        }
        finally
        {
            IsBusy = false;
        }
    }

    private void ShowDetail(ResultDetailDto detail)
    {
        Questions.Clear();
        foreach (var question in detail.Questions())
        {
            Questions.Add(question);
        }

        CorrectionDetailNote = Questions.Count > 0
            ? "Marks, topics and lost marks as recorded by the correction engine."
            : "This correction was stored without a question breakdown.";
        NotifyQuestions();
        RebuildInsights();
    }

    private void FillQuestions()
    {
        var id = SelectedCorrection?.Id;
        if (!string.IsNullOrWhiteSpace(id) && _details.TryGetValue(id, out var detail))
        {
            ShowDetail(detail);
        }
    }

    private void NotifyQuestions()
    {
        OnPropertyChanged(nameof(QuestionsEmpty));
        OnPropertyChanged(nameof(CorrectionDetailNote));
        OnPropertyChanged(nameof(HasCorrectionDetailNote));
    }

    // ---------------------------------------------------------------- rebuild

    private void RebuildAssignments()
    {
        Assignments.Clear();
        foreach (var assignment in _profile.Classroom.Assignments)
        {
            var include = AssignmentFilter switch
            {
                "Submitted" => assignment.Submitted,
                "Outstanding" => assignment.Outstanding,
                "Late" => assignment.Late == true,
                "Corrected" => assignment.HasCorrection,
                _ => true,
            };
            if (include)
            {
                Assignments.Add(assignment);
            }
        }

        // A row hidden by the filter must not stay selected: the actions beside
        // the list (open / export) would then act on a row the teacher cannot
        // see.
        if (SelectedAssignment is not null && !Assignments.Contains(SelectedAssignment))
        {
            SelectedAssignment = null;
        }

        OnPropertyChanged(nameof(AssignmentCountText));
        OnPropertyChanged(nameof(AssignmentsEmpty));
    }

    public string AssignmentCountText
    {
        get
        {
            var total = _profile.Classroom.Assignments.Count;
            return total == 0
                ? "No assignments tracked"
                : $"{Assignments.Count} of {total} shown";
        }
    }

    public bool AssignmentsEmpty => Assignments.Count == 0;

    private void RebuildCorrections()
    {
        Corrections.Clear();
        foreach (var correction in AllCorrections)
        {
            Corrections.Add(correction);
        }

        if (Corrections.Count == 0)
        {
            CorrectionDetailNote = "This student has no stored corrections yet.";
            NotifyQuestions();
        }

        OnPropertyChanged(nameof(CorrectionsEmpty));
        OnPropertyChanged(nameof(HasCorrections));
        OnPropertyChanged(nameof(CorrectionCountText));
    }

    public bool CorrectionsEmpty => Corrections.Count == 0;

    public bool HasCorrections => Corrections.Count > 0;

    private void ApplyCorrectionTitles()
    {
        var titles = _profile.Classroom.Assignments
            .Where(a => !string.IsNullOrWhiteSpace(a.CourseworkId))
            .GroupBy(a => a.CourseworkId!, StringComparer.Ordinal)
            .ToDictionary(g => g.Key, g => g.First().TitleText, StringComparer.Ordinal);
        foreach (var correction in AllCorrections)
        {
            if (correction.CourseworkId is not null
                && titles.TryGetValue(correction.CourseworkId, out var title))
            {
                correction.AssignmentTitle = title;
            }
        }
    }

    private void RebuildInsights()
    {
        // --- topic aggregation from the stored question breakdowns ----------
        Topics.Clear();
        var byTopic = new Dictionary<string, (double Earned, double Available, int Count)>(
            StringComparer.OrdinalIgnoreCase);
        foreach (var correction in _profile.Corrections.Recent)
        {
            if (!_details.TryGetValue(correction.Id, out var detail))
            {
                continue;
            }

            foreach (var question in detail.Questions())
            {
                var max = question.MaxMarks ?? question.Max ?? question.Available;
                if (max is null or <= 0)
                {
                    continue;
                }

                var award = question.AwardedMarks ?? question.Awarded ?? question.Earned ?? 0;
                var topic = string.IsNullOrWhiteSpace(question.Topic) ? "Unclassified" : question.Topic!.Trim();
                byTopic.TryGetValue(topic, out var current);
                byTopic[topic] = (current.Earned + award, current.Available + max.Value, current.Count + 1);
            }
        }

        foreach (var (topic, value) in byTopic
                     .OrderBy(pair => pair.Value.Available > 0
                         ? pair.Value.Earned / pair.Value.Available : 1)
                     .ThenBy(pair => pair.Key, StringComparer.OrdinalIgnoreCase))
        {
            Topics.Add(new TopicPerformanceVm(topic, value.Earned, value.Available, value.Count));
        }

        var repeated = Topics
            .Where(t => t.Questions >= 2 && t.Percentage < 60)
            .Select(t => t.Topic)
            .ToList();
        HasRepeatedDifficulties = repeated.Count > 0;
        RepeatedText = repeated.Count == 0
            ? "No repeated difficulty detected in the available data."
            : "Repeated difficulty: " + string.Join(", ", repeated);

        if (Topics.Count == 0)
        {
            StrengthText = "Not enough topic data yet.";
            FocusText = "Not enough topic data yet.";
        }
        else
        {
            var highest = Topics.OrderByDescending(t => t.Percentage).Take(2).ToList();
            var lowest = Topics.OrderBy(t => t.Percentage).Take(2).ToList();
            StrengthText = string.Join(" · ", highest.Select(t => $"{t.Topic} ({t.Percentage:0.#}%)"));
            FocusText = string.Join(" · ", lowest.Select(t => $"{t.Topic} ({t.Percentage:0.#}%)"));
        }

        OnPropertyChanged(nameof(HasTopics));
        OnPropertyChanged(nameof(HasRepeatedDifficulties));
        OnPropertyChanged(nameof(RepeatedText));
        OnPropertyChanged(nameof(StrengthText));
        OnPropertyChanged(nameof(FocusText));
    }

    /// <summary>The score trend comes straight from the profile's stored
    /// percentages; it does not wait for (or depend on) question detail.</summary>
    private void RebuildTrend()
    {
        Trend.Clear();
        foreach (var correction in AllCorrections
                     .Where(c => c.Percentage is not null)
                     .Take(MaxTrendPoints)
                     .Reverse())
        {
            Trend.Add(new ScorePointVm(correction.CreatedDayText, correction.Percentage!.Value));
        }

        OnPropertyChanged(nameof(HasTrend));
        OnPropertyChanged(nameof(TrendNote));
    }

    // ---------------------------------------------------------------- actions

    private void BeginEdit()
    {
        NicknameDraft = _profile.Nickname ?? string.Empty;
        ClassDraft = _profile.ClassName ?? string.Empty;
        Error = null;
        Status = string.Empty;
        IsEditingDetails = true;
    }

    private void CancelEdit()
    {
        IsEditingDetails = false;
        Error = null;
    }

    /// <summary>Persists the nickname/class drafts. Public so a test can await
    /// the same path the Save button uses.</summary>
    public async Task SaveDetailsAsync()
    {
        Error = null;
        var nickname = NicknameDraft.Trim();
        var className = ClassDraft.Trim();
        if (nickname.Length > 60)
        {
            Error = "Nicknames are limited to 60 characters.";
            return;
        }

        if (className.Length > 40)
        {
            Error = "Class names are limited to 40 characters.";
            return;
        }

        IsBusy = true;
        try
        {
            var updated = await _session.Api.SaveStudentDetailsAsync(
                _profile.StudentId,
                nickname.Length == 0 ? null : nickname,
                className.Length == 0 ? null : className).ConfigureAwait(true);
            Apply(updated);
            IsEditingDetails = false;
            Status = "Saved — local details only; Google Classroom was not changed.";
            _session.Notify("Student details saved.");
            ProfileSaved?.Invoke();
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
        catch (Exception ex)
        {
            Error = ex.Message;
            _session.Log.Error("saving student details failed", ex);
        }
        finally
        {
            IsBusy = false;
        }
    }

    /// <summary>Replaces the whole profile after a save or a refresh, keeping
    /// the open tab and the selected correction where they still exist.</summary>
    public void Apply(StudentProfileDto profile)
    {
        _profile = profile;
        _details.Clear();
        _detailsLoaded = false;
        _selectedCorrection = null;
        _selectedAssignment = null;
        AllCorrections.ToList().ForEach(c => c.AssignmentTitle = null);

        ApplyCorrectionTitles();
        RebuildAssignments();
        RebuildCorrections();
        ClearInsights();
        RebuildTrend();
        // Same rule as construction: set the field, not the property, so the
        // batch detail load below is the only request that runs.
        _selectedCorrection = Corrections.FirstOrDefault();
        OpenSelectedInDeskCommand.RaiseCanExecuteChanged();
        ExportCorrectionReportCommand.RaiseCanExecuteChanged();
        NicknameDraft = profile.Nickname ?? string.Empty;
        ClassDraft = profile.ClassName ?? string.Empty;

        OnPropertyChanged(nameof(Profile));
        OnPropertyChanged(nameof(SelectedAssignment));
        OnPropertyChanged(nameof(HasSelectedAssignment));
        OnPropertyChanged(nameof(SelectedCorrection));
        OnPropertyChanged(nameof(HasSelectedCorrection));
        OnPropertyChanged(nameof(SelectedCorrectionTitle));
        OnPropertyChanged(nameof(SelectedCorrectionScore));
        OnPropertyChanged(nameof(SelectedCorrectionStatus));
        OnPropertyChanged(nameof(SelectedCorrectionWhen));
        OnPropertyChanged(nameof(CorrectionCountText));
        OnPropertyChanged(nameof(AssignmentCountText));
        OnPropertyChanged(nameof(TopicNote));
        NotifySummary();
        _ = EnsureDetailsAsync();
    }

    private void NotifySummary()
    {
        foreach (var name in new[]
                 {
                     nameof(AssignmentsText), nameof(SubmittedText), nameof(OutstandingText),
                     nameof(CorrectionsValueText), nameof(McAverageText), nameof(OverallText),
                     nameof(ClassroomAverageText), nameof(ClassroomSpreadText),
                     nameof(ClassroomGradedText), nameof(McSpreadText), nameof(ZerosText),
                     nameof(PendingText), nameof(NeedsReviewText), nameof(OnTimeText),
                     nameof(LateCountText), nameof(MissingCountText), nameof(DraftText),
                     nameof(TurnedInText), nameof(ReturnedText), nameof(CorrectedCountText),
                     nameof(FailedJobsText), nameof(LastCorrectionText),
                     nameof(ShowTrackingWarning), nameof(TrackingWarning), nameof(CorrectionCountText),
                 })
        {
            OnPropertyChanged(name);
        }
    }

    private void ClearInsights()
    {
        Topics.Clear();
        Questions.Clear();
        HasRepeatedDifficulties = false;
        StrengthText = "Not enough topic data yet.";
        FocusText = "Not enough topic data yet.";
        RepeatedText = "No repeated difficulty detected in the available data.";
        CorrectionDetailNote = "Select a correction to see its question breakdown.";
        OnPropertyChanged(nameof(HasTopics));
        OnPropertyChanged(nameof(HasRepeatedDifficulties));
        OnPropertyChanged(nameof(RepeatedText));
        OnPropertyChanged(nameof(StrengthText));
        OnPropertyChanged(nameof(FocusText));
        NotifyQuestions();
    }

    private async Task OpenAssignmentCorrectionAsync()
    {
        var assignment = SelectedAssignment;
        var resultId = assignment?.Result?.Id;
        if (assignment is null || string.IsNullOrWhiteSpace(resultId))
        {
            return;
        }

        var existing = Corrections.FirstOrDefault(c => c.Id == resultId);
        if (existing is null)
        {
            existing = new StudentCorrectionDto
            {
                Id = resultId,
                CourseworkId = assignment.CourseworkId,
                SubmissionId = assignment.SubmissionId,
                Earned = assignment.Result?.Earned,
                Available = assignment.Result?.Available,
                Percentage = assignment.Result?.Percentage,
                AssignmentTitle = assignment.TitleText,
            };
            Corrections.Insert(0, existing);
            OnPropertyChanged(nameof(CorrectionsEmpty));
        }

        SelectedSection = Sections.First(s => s.Key == "corrections");
        SelectedCorrection = existing;
        await Task.CompletedTask;
    }

    private Task OpenSelectedInDeskAsync()
    {
        if (SelectedCorrection is not null)
        {
            _session.OpenResult(SelectedCorrection.Id);
        }

        return Task.CompletedTask;
    }

    private async Task ExportReportAsync()
    {
        var file = await _session.Api.StudentReportAsync(_profile.StudentId).ConfigureAwait(true);
        SaveDownload(file, "PDF report (*.pdf)|*.pdf", "Save student report");
    }

    private async Task ExportAssignmentReportAsync()
    {
        var assignment = SelectedAssignment;
        if (assignment?.CourseworkId is null)
        {
            return;
        }

        var file = await _session.Api.StudentAssignmentReportAsync(
            _profile.StudentId, assignment.CourseworkId).ConfigureAwait(true);
        SaveDownload(file, "PDF report (*.pdf)|*.pdf", "Save assignment report");
    }

    private async Task ExportCorrectionReportAsync()
    {
        var coursework = SelectedCorrection?.CourseworkId;
        if (string.IsNullOrWhiteSpace(coursework))
        {
            return;
        }

        var file = await _session.Api.StudentAssignmentReportAsync(
            _profile.StudentId, coursework).ConfigureAwait(true);
        SaveDownload(file, "PDF report (*.pdf)|*.pdf", "Save assignment report");
    }

    private void SaveDownload(DownloadFile file, string filter, string title)
    {
        var saved = DownloadSaver.Save(_session.Settings, _session.Dialogs, file, filter, title, openAfter: true);
        if (!string.IsNullOrEmpty(saved))
        {
            Status = "Saved: " + saved;
            _session.Notify("Report saved: " + saved);
        }
    }

    private void HandleError(Exception ex)
    {
        Error = ex is ApiException api ? api.Display : ex.Message;
        _session.Log.Error("student profile action failed", ex);
    }
}
