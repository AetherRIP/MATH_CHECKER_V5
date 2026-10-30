using System.Collections.ObjectModel;
using System.Globalization;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One decision a teacher can record for a reference item.</summary>
public sealed record BenchmarkDecisionOption(string Code, string Label)
{
    public override string ToString() => Label;
}

/// <summary>
/// A reference item as the list renders it. The API's status, reason and
/// eligibility are copied verbatim; no certified/eligible decision is made in
/// the client.
/// </summary>
public sealed class BenchmarkReferenceRow
{
    public BenchmarkReferenceRow(BenchmarkReferenceDto data)
    {
        Data = data;
    }

    public BenchmarkReferenceDto Data { get; }

    public string ReferenceId => Data.ReferenceId;
    public string PaperId => Data.PaperId;
    public string PaperTitle => Data.PaperTitle;
    public string QuestionLabel => string.IsNullOrWhiteSpace(Data.Question)
        ? Data.ReferenceId
        : $"Q{Data.Question}" + (string.IsNullOrWhiteSpace(Data.Subquestion)
            ? string.Empty
            : $"({Data.Subquestion})");
    public string LevelLabel => string.Equals(Data.Level, "subquestion",
        StringComparison.OrdinalIgnoreCase) ? "Subquestion" : "Question";
    public string MaxText => $"{Data.MaxMarks} marks";
    public string SchemeText => $"{Data.SchemeMark} / {Data.MaxMarks}";
    public string Status => Data.Reference.Status;
    public string StatusLabel => BenchmarkLabels.Status(Data.Reference.Status);
    public string Reason => Data.Reference.Reason;
    public string ReasonLabel => BenchmarkLabels.Reason(Data.Reference.Reason);
    public string TeacherMarkText => Data.Reference.TeacherMark is null
        ? "Not confirmed"
        : $"{Data.Reference.TeacherMark} / {Data.MaxMarks}";
    public string AiMarkText => Data.Ai?.Mark is null
        ? "No AI mark"
        : $"{Data.Ai.Mark} / {Data.Ai.Max ?? Data.MaxMarks}";
    public string ProvenanceLabel => BenchmarkLabels.Provenance(Data.Provenance);
    public bool IsEligible => Data.Reference.Eligible;

    public string Mention => $"{QuestionLabel} of {PaperId}";

    public override string ToString() => $"{Mention} ({StatusLabel})";
}

/// <summary>One recorded assessment in the append-only history.</summary>
public sealed class BenchmarkAssessmentRow
{
    public BenchmarkAssessmentRow(BenchmarkAssessmentDto data, int maxMarks)
    {
        Data = data;
        MaxMarks = maxMarks;
    }

    public BenchmarkAssessmentDto Data { get; }
    public int MaxMarks { get; }

    public string SeqText => $"#{Data.Seq}";
    public string AssessorText => string.IsNullOrWhiteSpace(Data.Role)
        ? Data.Assessor
        : $"{Data.Assessor} · {Data.Role}";
    public string MarkText => Data.Awarded is null ? "No mark" : $"{Data.Awarded} / {MaxMarks}";
    public string StatusLabel => BenchmarkLabels.Status(Data.Status);
    public string NoteText => string.IsNullOrWhiteSpace(Data.Note) ? "No note recorded." : Data.Note;
    public string AlternativeText => Data.AlternativeMethods.Count == 0
        ? string.Empty
        : "Accepted alternatives: " + string.Join("; ", Data.AlternativeMethods);
    public bool HasAlternatives => Data.AlternativeMethods.Count > 0;
    public string WhenText => BenchmarkLabels.Timestamp(Data.CreatedAt);
}

internal static class BenchmarkLabels
{
    public static string Status(string? status) => (status ?? string.Empty).ToLowerInvariant() switch
    {
        "verified" => "Teacher verified",
        "disputed" => "Disputed",
        "incomplete" => "Incomplete",
        "unverified" => "Unverified",
        _ => string.IsNullOrWhiteSpace(status) ? "Unknown" : status,
    };

    public static string Reason(string? reason) => (reason ?? string.Empty).ToLowerInvariant() switch
    {
        "no_teacher_assessment" => "No teacher has assessed this reference yet.",
        "no_verified_assessment" => "An assessment exists, but none is a verified teacher mark.",
        "teachers_disagree" => "Two verified teacher marks disagree; both are preserved.",
        "teacher_dispute" => "A teacher marked this reference disputed.",
        "assessment_incomplete" => "The latest teacher assessment is incomplete.",
        "invalid_teacher_mark" => "The teacher mark is outside this item's allowed range.",
        "not_teacher_verifiable" => "This reference's provenance is not eligible for teacher certification.",
        "" => "No eligibility reason was supplied.",
        _ => reason!,
    };

    public static string Provenance(string? provenance) => (provenance ?? string.Empty)
        .ToLowerInvariant() switch
    {
        "teacher_verified" => "Teacher verified",
        "official_reference" => "Official reference (scheme-derived)",
        "ai_generated" => "AI generated",
        "unverified" => "Unverified",
        _ => string.IsNullOrWhiteSpace(provenance) ? "Unknown provenance" : provenance,
    };

    public static string Timestamp(string? raw)
    {
        if (DateTimeOffset.TryParse(raw, CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal, out var value))
        {
            return value.ToLocalTime().ToString("dd MMM yyyy HH:mm", CultureInfo.InvariantCulture);
        }

        return string.IsNullOrWhiteSpace(raw) ? "Time not recorded" : raw;
    }
}

/// <summary>
/// The native Accuracy Benchmark page.
///
/// It consumes the same /api/benchmark routes as the browser page and never
/// computes eligibility or accuracy itself. A teacher mark is sent only after
/// the confirmation box is ticked; the backend remains the authority on
/// conflicts, provenance and certification.
/// </summary>
public sealed class BenchmarkAccuracyViewModel : PageViewModel
{
    private readonly Dictionary<string, BenchmarkDraft> _drafts = new(StringComparer.Ordinal);
    private BenchmarkOverviewDto? _overview;
    private BenchmarkReferenceRow? _selected;
    private BenchmarkReferenceDto? _detail;
    private CancellationTokenSource? _detailCts;
    private int _detailRequest;
    private bool _isDetailBusy;
    private string? _detailError;
    private bool _isConfirmed;
    private string _markText = string.Empty;
    private string _assessor = string.Empty;
    private string _assessorRole = string.Empty;
    private string _note = string.Empty;
    private string _alternativeText = string.Empty;
    private BenchmarkDecisionOption? _decision;
    private BenchmarkReproductionDto? _reproduction;
    private string? _workflowStatus;
    private bool _restoringDraft;

    public BenchmarkAccuracyViewModel(AppSession session)
        : base(session, "accuracy", "Accuracy benchmark")
    {
        Group = "Review";
        DecisionOptions = new[]
        {
            new BenchmarkDecisionOption("verified", "Verified — I assessed this mark myself"),
            new BenchmarkDecisionOption("disputed", "Disputed — this reference needs resolution"),
            new BenchmarkDecisionOption("incomplete", "Incomplete — I cannot assess it yet"),
        };
        _decision = DecisionOptions[0];

        SubmitCommand = CommandWithReload(SubmitAsync,
            "Assessment recorded. The backend resolved the reference's new status.",
            canExecute: () => CanSubmit);
        ReproduceCommand = Command(ReproduceAsync,
            canExecute: () => HasLatestRun && _reproduction is null);
        RetryDetailCommand = new AsyncCommand(ReloadDetailAsync);
    }

    public ObservableCollection<BenchmarkReferenceRow> References { get; } = new();
    public ObservableCollection<string> SchemeLines { get; } = new();
    public ObservableCollection<string> AnswerLines { get; } = new();
    public ObservableCollection<string> Categories { get; } = new();
    public ObservableCollection<BenchmarkAssessmentRow> Assessments { get; } = new();
    public ObservableCollection<string> Warnings { get; } = new();
    public ObservableCollection<string> Limitations { get; } = new();

    public IReadOnlyList<BenchmarkDecisionOption> DecisionOptions { get; }

    public AsyncCommand SubmitCommand { get; }
    public AsyncCommand ReproduceCommand { get; }
    public AsyncCommand RetryDetailCommand { get; }

    public bool HasReferences => References.Count > 0;
    public bool HasNoReferences => References.Count == 0;
    public string Summary => References.Count == 0 && _overview is not null
        ? "No reference items are available in the configured dataset."
        : $"{References.Count} reference items. A teacher mark becomes eligible only after explicit confirmation.";

    // ------------------------------------------------------------- certification

    public bool CertifiedAvailable => _overview?.CertifiedAvailable == true;
    public bool ShowCertifiedMetrics => CertifiedAvailable;
    public bool ShowUnavailable => !CertifiedAvailable;

    public string CertifiedHeadline => CertifiedAvailable
        ? "Teacher-Verified Benchmark Accuracy"
        : "Certified accuracy unavailable";

    public string CertifiedStatusText
    {
        get
        {
            if (CertifiedAvailable)
            {
                return "Measured only against references with an explicit teacher confirmation.";
            }

            if (VerifiedCount == 0)
            {
                return "Certified accuracy unavailable. Not 0%. Accuracy has not yet been measured "
                       + "against teacher-verified references.";
            }

            return "Certified accuracy unavailable. " + (_overview?.CertificationReason
                ?? "The latest run is not certified for the current dataset and teacher assessments.");
        }
    }

    public bool HasCertificationReason => !string.IsNullOrWhiteSpace(CertificationReason);

    public string CertificationReason => _overview?.CertificationReason ?? string.Empty;

    public string CertifiedItemsText => MetricText(_overview?.CertifiedAccuracy?.Items);
    public string CertifiedQuestionsText => MetricText(_overview?.CertifiedAccuracy?.Questions);
    public string CertifiedSubquestionsText => MetricText(_overview?.CertifiedAccuracy?.Subquestions);
    public string CertifiedTotalText => TotalMetricText(_overview?.CertifiedAccuracy?.Total);
    public string CertifiedMaeText => FormatMarks(CertifiedAvailable
        ? _overview?.LatestRun?.Error?.MeanAbsoluteError
        : null);
    public string CertifiedOverText => DirectionText(
        _overview?.LatestRun?.Error?.Overmarking, "overmarked");
    public string CertifiedUnderText => DirectionText(
        _overview?.LatestRun?.Error?.Undermarking, "undermarked");
    public string CertifiedCoverageText => _overview?.LatestRun?.Coverage is { } coverage
        ? $"{FormatPercent(coverage.ItemCoverage)} items · {coverage.MissingMarkCount} missing"
        : "—";
    public string CertifiedReviewText => _overview?.LatestRun?.Review is { } review
        ? $"{FormatPercent(review.ReviewFlagRate)} flagged · {FormatPercent(review.HumanReviewRate)} disagreed"
        : "—";
    public string CertifiedSampleText => _overview?.CertifiedAccuracy?.Sample is { } sample
        ? $"{sample.ReferencesExpected} references across {sample.PapersExpected} scripts · "
          + $"{sample.MarksAvailable} marks"
        : "No measured sample.";

    public int VerifiedCount => Count("verified");
    public int UnverifiedCount => Count("unverified");
    public int DisputedCount => Count("disputed");
    public int IncompleteCount => Count("incomplete");
    public int HoldoutCount => _overview?.Dataset.Partitions.TryGetValue("holdout", out var count) == true
        ? count
        : 0;

    public string VerifiedCountText => VerifiedCount.ToString(CultureInfo.InvariantCulture);
    public string UnverifiedCountText => UnverifiedCount.ToString(CultureInfo.InvariantCulture);
    public string DisputedCountText => DisputedCount.ToString(CultureInfo.InvariantCulture);
    public string IncompleteCountText => IncompleteCount.ToString(CultureInfo.InvariantCulture);
    public string HoldoutCountText => HoldoutCount.ToString(CultureInfo.InvariantCulture);

    // ---------------------------------------------------------------- dataset

    public string DatasetVersionText => string.IsNullOrWhiteSpace(_overview?.Dataset.Version)
        ? "—"
        : $"v{_overview!.Dataset.Version}";
    public string DatasetReferenceCountText => $"{_overview?.Dataset.ItemCount ?? 0} references";
    public string DatasetCompositionText
    {
        get
        {
            var dataset = _overview?.Dataset;
            return dataset is null
                ? "—"
                : $"{dataset.PaperCount} papers · {dataset.QuestionCount} questions · "
                  + $"{dataset.SubquestionCount} subquestions · {dataset.MarksAvailable} marks";
        }
    }

    public string DatasetShaText => string.IsNullOrWhiteSpace(_overview?.Dataset.Sha256)
        ? "—"
        : _overview!.Dataset.Sha256[..Math.Min(12, _overview.Dataset.Sha256.Length)] + "…";

    public string DatasetPartitionsText => _overview is null
        ? "—"
        : string.Join(" · ", _overview.Dataset.Partitions.OrderBy(p => p.Key)
            .Select(p => $"{p.Key} {p.Value}"));

    public string DatasetProvenanceText => _overview is null
        ? "—"
        : string.Join(" · ", _overview.Verification.Provenance
            .OrderBy(p => p.Key)
            .Select(p => $"{BenchmarkLabels.Provenance(p.Key)} {p.Value}"));

    public string RunsRootText => _overview?.RunsRoot ?? "—";
    public bool HasWarnings => Warnings.Count > 0;

    // --------------------------------------------------------------- latest run

    public bool HasLatestRun => _overview?.LatestRun is not null;
    public string LatestRunId => _overview?.LatestRun?.RunId ?? "No run recorded";
    public string LatestRunStatus => _overview?.LatestRun is null
        ? "No benchmark run"
        : $"{_overview.LatestRun.Status} · {(CertifiedAvailable ? "certified" : "not certified")}";
    public string LatestRunModel => _overview?.LatestRun?.Model ?? "—";
    public string LatestRunMode => BenchmarkMode(_overview?.LatestRun?.Mode);
    public string LatestRunStarted => BenchmarkLabels.Timestamp(_overview?.LatestRun?.StartedAt);
    public string LatestRunFinished => BenchmarkLabels.Timestamp(_overview?.LatestRun?.FinishedAt);
    public string LatestRunDataset => _overview?.LatestRun is null
        ? "—"
        : $"dataset v{_overview.LatestRun.DatasetVersion ?? "?"} · "
          + $"{(_overview.LatestRun.DatasetMatches ? "matches current dataset" : "different dataset")}";
    public string LatestRunVerification => _overview?.LatestRun is null
        ? "—"
        : _overview.LatestRun.VerificationMatches
            ? "teacher assessments match the run snapshot"
            : "teacher assessments have changed since the run";
    public string LatestRunReproduction
    {
        get
        {
            var state = _reproduction ?? _overview?.LatestRun?.Reproduction;
            if (state is null)
            {
                return "No reproduction record.";
            }

            return state.Reproduced
                ? "PASS — metrics recomputed exactly from the recorded inputs."
                : "NOT VERIFIED — " + string.Join(", ", state.Differences);
        }
    }

    public bool CanReproduce => HasLatestRun && _reproduction is null;
    public string RunControlNote =>
        "A new evaluation is not exposed by the backend API. This page never starts live "
        + "provider inference; run the benchmark CLI after teacher verification. Recompute below "
        + "only replays a recorded run, without contacting the provider.";

    public bool HasLimitations => Limitations.Count > 0;

    // ------------------------------------------------------------- reference

    public BenchmarkReferenceRow? Selected
    {
        get => _selected;
        set
        {
            var previous = _selected;
            if (!SetProperty(ref _selected, value))
            {
                return;
            }

            SaveDraft(previous);
            OnPropertyChanged(nameof(HasSelection));
            OnPropertyChanged(nameof(SelectedMention));
            RestoreDraft(value);
            RaiseFormChanged();
            if (value is not null)
            {
                _ = LoadDetailAsync(value.ReferenceId);
            }
            else
            {
                ClearDetail();
            }
        }
    }

    public bool HasSelection => _selected is not null;
    public bool HasDetail => _detail is not null;
    public string SelectedMention => _selected?.Mention ?? "No reference selected";

    public BenchmarkReferenceDto? Detail
    {
        get => _detail;
        private set
        {
            if (SetProperty(ref _detail, value))
            {
                OnPropertyChanged(nameof(HasDetail));
            }
        }
    }

    public bool IsDetailBusy
    {
        get => _isDetailBusy;
        private set => SetProperty(ref _isDetailBusy, value);
    }

    public string? DetailError
    {
        get => _detailError;
        private set
        {
            if (SetProperty(ref _detailError, value))
            {
                OnPropertyChanged(nameof(HasDetailError));
            }
        }
    }

    public bool HasDetailError => !string.IsNullOrWhiteSpace(DetailError);

    public string DetailPaper => _detail?.PaperTitle ?? "—";
    public string DetailIdentifiers => _detail is null
        ? "—"
        : $"{_detail.PaperId} · Q{_detail.Question}"
          + (string.IsNullOrWhiteSpace(_detail.Subquestion) ? string.Empty : $"({_detail.Subquestion})")
          + $" · {DetailLevel}";
    public string DetailLevel => _detail?.Level == "subquestion" ? "subquestion" : "question";
    public string DetailBoard => _detail is null
        ? "—"
        : $"{_detail.Board} · {_detail.Qualification} · {_detail.Topic}";
    public string DetailPartition => _detail?.Partition ?? "—";
    public string DetailProvenance => BenchmarkLabels.Provenance(_detail?.Provenance);
    public string DetailMaxText => _detail is null ? "—" : $"{_detail.MaxMarks} marks";
    public string DetailPrompt => _detail?.Prompt ?? "—";
    public string DetailRuling => _detail?.Ruling ?? "—";
    public string DetailBasis => _detail?.Basis ?? "—";
    public string DetailSchemeMarkText => _detail is null
        ? "—"
        : $"{_detail.SchemeMark} / {_detail.MaxMarks}";
    public string DetailTeacherMarkText => _detail?.Reference.TeacherMark is null
        ? "Not confirmed"
        : $"{_detail.Reference.TeacherMark} / {_detail.MaxMarks}";
    public string DetailTeacherStatus => BenchmarkLabels.Status(_detail?.Reference.Status);
    public string DetailEligibility => _detail is null
        ? "—"
        : _detail.Reference.Eligible
            ? "Eligible for the certified benchmark."
            : "Not eligible. " + BenchmarkLabels.Reason(_detail.Reference.Reason);
    public bool DetailIsEligible => _detail?.Reference.Eligible == true;
    public bool DetailIsDisputed => string.Equals(_detail?.Reference.Status, "disputed",
        StringComparison.OrdinalIgnoreCase);

    public string DetailConflict => _detail?.Reference.Reason switch
    {
        "teachers_disagree" =>
            "Two verified teacher marks differ. Both assessments are preserved below; this item "
            + "stays ineligible until the disagreement is resolved by the existing backend rules.",
        "teacher_dispute" =>
            "A teacher recorded this reference as disputed. Every assessment is preserved below; "
            + "it remains ineligible until a later assessment resolves the dispute.",
        "assessment_incomplete" =>
            "The latest assessment is incomplete. It remains ineligible.",
        _ => string.Empty,
    };

    public bool HasConflict => !string.IsNullOrWhiteSpace(DetailConflict);

    public string AiMarkText
    {
        get
        {
            if (_detail is null)
            {
                return "—";
            }

            var ai = _detail.Ai;
            if (ai?.Mark is null)
            {
                return ai?.Message
                       ?? (_detail.RunId is null
                           ? "No benchmark run has compared this item yet."
                           : "No AI mark was matched to this reference in the latest run.");
            }

            var direction = string.IsNullOrWhiteSpace(ai.Direction) || ai.Direction == "exact"
                ? "exact"
                : $"{ai.Direction} by {Math.Abs(ai.Difference ?? 0)}";
            return $"{ai.Mark} / {ai.Max ?? _detail.MaxMarks} · {direction}";
        }
    }

    public string AiMarkDetail
    {
        get
        {
            var ai = _detail?.Ai;
            if (ai?.Mark is null)
            {
                return string.Empty;
            }

            var parts = new List<string>();
            if (!string.IsNullOrWhiteSpace(ai.MatchedBy))
            {
                parts.Add("matched by " + ai.MatchedBy.Replace('_', ' '));
            }

            if (ai.ReviewRequired)
            {
                parts.Add("the run requested review for this script");
            }

            return parts.Count == 0 ? "Matched from the latest recorded run." : string.Join(" · ", parts);
        }
    }

    public bool HasAssessments => Assessments.Count > 0;
    public bool HasAiMark => _detail?.Ai?.Mark is not null;

    // -------------------------------------------------------------- the form

    public IReadOnlyList<string> Statuses => new[] { "verified", "disputed", "incomplete" };

    public BenchmarkDecisionOption? Decision
    {
        get => _decision;
        set
        {
            if (SetProperty(ref _decision, value))
            {
                RaiseFormChanged();
            }
        }
    }

    public string MarkText
    {
        get => _markText;
        set
        {
            if (SetProperty(ref _markText, value))
            {
                RaiseFormChanged();
            }
        }
    }

    public string Assessor
    {
        get => _assessor;
        set
        {
            if (SetProperty(ref _assessor, value))
            {
                RaiseFormChanged();
            }
        }
    }

    public string AssessorRole
    {
        get => _assessorRole;
        set => SetProperty(ref _assessorRole, value);
    }

    public string Note
    {
        get => _note;
        set
        {
            if (SetProperty(ref _note, value))
            {
                RaiseFormChanged();
            }
        }
    }

    public string AlternativeText
    {
        get => _alternativeText;
        set => SetProperty(ref _alternativeText, value);
    }

    public bool IsConfirmed
    {
        get => _isConfirmed;
        set
        {
            if (SetProperty(ref _isConfirmed, value))
            {
                RaiseFormChanged();
            }
        }
    }

    public bool IsDisputeDecision => string.Equals(_decision?.Code, "disputed",
        StringComparison.OrdinalIgnoreCase);
    public bool IsIncompleteDecision => string.Equals(_decision?.Code, "incomplete",
        StringComparison.OrdinalIgnoreCase);
    public bool RequiresNote => IsDisputeDecision || IsIncompleteDecision;
    public bool RequiresMark => !RequiresNote;

    public bool CanSubmit
    {
        get
        {
            if (_detail is null || _decision is null || !_isConfirmed
                || string.IsNullOrWhiteSpace(_assessor))
            {
                return false;
            }

            if (RequiresNote && string.IsNullOrWhiteSpace(_note))
            {
                return false;
            }

            if (RequiresMark && !TryReadMark(out _))
            {
                return false;
            }

            return !RequiresNote || TryReadMarkOrBlank(out _);
        }
    }

    public string ValidationHint
    {
        get
        {
            if (_detail is null)
            {
                return "Select a reference item first.";
            }

            if (!_isConfirmed)
            {
                return "Tick the confirmation box to enable recording. Opening or closing this "
                       + "page never records an assessment.";
            }

            if (string.IsNullOrWhiteSpace(_assessor))
            {
                return "Enter your name, initials or role identifier as the assessor.";
            }

            if (RequiresMark && !TryReadMark(out _))
            {
                return $"Enter a whole-number mark from 0 to {_detail.MaxMarks}.";
            }

            if (RequiresNote && string.IsNullOrWhiteSpace(_note))
            {
                return "A disputed or incomplete assessment needs a brief explanation.";
            }

            if (RequiresNote && !TryReadMarkOrBlank(out _))
            {
                return $"The optional mark must be a whole number from 0 to {_detail.MaxMarks}, or blank.";
            }

            return "Ready. The backend will append exactly one assessment when you record it.";
        }
    }

    public string FormTitle => _detail is null
        ? "Record a teacher assessment"
        : $"Record a teacher assessment for {SelectedMention}";

    public string WorkflowStatus
    {
        get => _workflowStatus ?? string.Empty;
        private set
        {
            if (SetProperty(ref _workflowStatus, value))
            {
                OnPropertyChanged(nameof(HasWorkflowStatus));
            }
        }
    }

    public bool HasWorkflowStatus => !string.IsNullOrWhiteSpace(_workflowStatus);

    // ------------------------------------------------------------------ load

    protected override async Task LoadCoreAsync()
    {
        var overview = await Session.Api.BenchmarkOverviewAsync().ConfigureAwait(true);
        var references = await Session.Api.BenchmarkReferencesAsync().ConfigureAwait(true);
        // A fresh load re-enables reproduction of the new latest run; doing
        // this after ApplyOverview left the command disabled by the previous
        // reproduction result.
        _reproduction = null;
        ApplyOverview(overview);
        ApplyReferences(references);
        WorkflowStatus = string.Empty;
        OnPropertyChanged(nameof(CanReproduce));
        ReproduceCommand.RaiseCanExecuteChanged();
    }

    private void ApplyOverview(BenchmarkOverviewDto overview)
    {
        _overview = overview;

        Warnings.Clear();
        foreach (var warning in overview.Dataset.Warnings)
        {
            Warnings.Add(warning);
        }

        Limitations.Clear();
        foreach (var limitation in overview.LatestRun?.Limitations ?? new List<string>())
        {
            Limitations.Add(limitation);
        }

        OnPropertyChanged(nameof(CertifiedAvailable));
        OnPropertyChanged(nameof(ShowCertifiedMetrics));
        OnPropertyChanged(nameof(ShowUnavailable));
        OnPropertyChanged(nameof(CertifiedHeadline));
        OnPropertyChanged(nameof(CertifiedStatusText));
        OnPropertyChanged(nameof(HasCertificationReason));
        OnPropertyChanged(nameof(CertificationReason));
        OnPropertyChanged(nameof(CertifiedItemsText));
        OnPropertyChanged(nameof(CertifiedQuestionsText));
        OnPropertyChanged(nameof(CertifiedSubquestionsText));
        OnPropertyChanged(nameof(CertifiedTotalText));
        OnPropertyChanged(nameof(CertifiedMaeText));
        OnPropertyChanged(nameof(CertifiedOverText));
        OnPropertyChanged(nameof(CertifiedUnderText));
        OnPropertyChanged(nameof(CertifiedCoverageText));
        OnPropertyChanged(nameof(CertifiedReviewText));
        OnPropertyChanged(nameof(CertifiedSampleText));
        OnPropertyChanged(nameof(VerifiedCount));
        OnPropertyChanged(nameof(UnverifiedCount));
        OnPropertyChanged(nameof(DisputedCount));
        OnPropertyChanged(nameof(IncompleteCount));
        OnPropertyChanged(nameof(HoldoutCount));
        OnPropertyChanged(nameof(VerifiedCountText));
        OnPropertyChanged(nameof(UnverifiedCountText));
        OnPropertyChanged(nameof(DisputedCountText));
        OnPropertyChanged(nameof(IncompleteCountText));
        OnPropertyChanged(nameof(HoldoutCountText));
        OnPropertyChanged(nameof(DatasetVersionText));
        OnPropertyChanged(nameof(DatasetReferenceCountText));
        OnPropertyChanged(nameof(DatasetCompositionText));
        OnPropertyChanged(nameof(DatasetShaText));
        OnPropertyChanged(nameof(DatasetPartitionsText));
        OnPropertyChanged(nameof(DatasetProvenanceText));
        OnPropertyChanged(nameof(RunsRootText));
        OnPropertyChanged(nameof(HasWarnings));
        OnPropertyChanged(nameof(HasLatestRun));
        OnPropertyChanged(nameof(LatestRunId));
        OnPropertyChanged(nameof(LatestRunStatus));
        OnPropertyChanged(nameof(LatestRunModel));
        OnPropertyChanged(nameof(LatestRunMode));
        OnPropertyChanged(nameof(LatestRunStarted));
        OnPropertyChanged(nameof(LatestRunFinished));
        OnPropertyChanged(nameof(LatestRunDataset));
        OnPropertyChanged(nameof(LatestRunVerification));
        OnPropertyChanged(nameof(LatestRunReproduction));
        OnPropertyChanged(nameof(CanReproduce));
        OnPropertyChanged(nameof(HasLimitations));
        ReproduceCommand.RaiseCanExecuteChanged();
        OnPropertyChanged(nameof(Summary));
    }

    private void ApplyReferences(BenchmarkReferencesDto references)
    {
        var keep = Selected?.ReferenceId;
        var rows = references.Items
            .Select(item => new BenchmarkReferenceRow(item))
            .OrderBy(row => row.PaperId, StringComparer.Ordinal)
            .ThenBy(row => row.QuestionLabel, StringComparer.Ordinal)
            .ToList();

        References.Clear();
        foreach (var row in rows)
        {
            References.Add(row);
        }

        OnPropertyChanged(nameof(HasReferences));
        OnPropertyChanged(nameof(HasNoReferences));
        OnPropertyChanged(nameof(Summary));
        OnPropertyChanged(nameof(FormTitle));

        Selected = rows.FirstOrDefault(row => string.Equals(row.ReferenceId, keep,
                       StringComparison.Ordinal))
                   ?? rows.FirstOrDefault();
    }

    private void SaveDraft(BenchmarkReferenceRow? row)
    {
        if (row is null || _restoringDraft)
        {
            return;
        }

        if (string.IsNullOrWhiteSpace(MarkText) && string.IsNullOrWhiteSpace(Note)
            && string.IsNullOrWhiteSpace(AlternativeText) && string.IsNullOrWhiteSpace(Assessor))
        {
            _drafts.Remove(row.ReferenceId);
            return;
        }

        _drafts[row.ReferenceId] = new BenchmarkDraft
        {
            Mark = MarkText,
            Note = Note,
            AlternativeText = AlternativeText,
            Assessor = Assessor,
            AssessorRole = AssessorRole,
            DecisionCode = Decision?.Code ?? "verified",
        };
    }

    private void RestoreDraft(BenchmarkReferenceRow? row)
    {
        _restoringDraft = true;
        try
        {
            if (row is not null && _drafts.TryGetValue(row.ReferenceId, out var draft))
            {
                MarkText = draft.Mark;
                Note = draft.Note;
                AlternativeText = draft.AlternativeText;
                Assessor = draft.Assessor;
                AssessorRole = draft.AssessorRole;
                Decision = DecisionOptions.FirstOrDefault(d =>
                               string.Equals(d.Code, draft.DecisionCode,
                                   StringComparison.OrdinalIgnoreCase))
                           ?? DecisionOptions[0];
            }
            else
            {
                MarkText = string.Empty;
                Note = string.Empty;
                AlternativeText = string.Empty;
                Decision = DecisionOptions[0];
            }

            // Confirmation is always a fresh, per-item act.
            IsConfirmed = false;
        }
        finally
        {
            _restoringDraft = false;
        }
    }

    private void ClearForm()
    {
        MarkText = string.Empty;
        Note = string.Empty;
        AlternativeText = string.Empty;
        IsConfirmed = false;
        Decision = DecisionOptions[0];
    }

    private async Task LoadDetailAsync(string referenceId)
    {
        _detailCts?.Cancel();
        _detailCts?.Dispose();
        var cts = new CancellationTokenSource();
        _detailCts = cts;
        var request = Interlocked.Increment(ref _detailRequest);

        IsDetailBusy = true;
        DetailError = null;
        try
        {
            var detail = await Session.Api.BenchmarkReferenceAsync(referenceId, cts.Token)
                .ConfigureAwait(true);
            if (request != _detailRequest || cts.IsCancellationRequested)
            {
                return;
            }

            ApplyDetail(detail);
        }
        catch (OperationCanceledException)
        {
            // A newer selection superseded this read.
        }
        catch (ApiException ex)
        {
            if (request == _detailRequest)
            {
                DetailError = ex.Display;
            }
        }
        catch (HttpRequestException ex)
        {
            if (request == _detailRequest)
            {
                DetailError = "The backend could not be reached: " + ex.Message;
            }
        }
        catch (Exception ex)
        {
            if (request == _detailRequest)
            {
                Session.Log.Error("benchmark reference detail failed", ex);
                DetailError = "The reference could not be loaded: " + ex.Message;
            }
        }
        finally
        {
            if (request == _detailRequest)
            {
                IsDetailBusy = false;
            }
        }
    }

    private async Task ReloadDetailAsync()
    {
        var id = Selected?.ReferenceId;
        if (id is not null)
        {
            await LoadDetailAsync(id).ConfigureAwait(true);
        }
    }

    private void ApplyDetail(BenchmarkReferenceDto detail)
    {
        Detail = detail;
        SchemeLines.Clear();
        foreach (var line in detail.Scheme)
        {
            SchemeLines.Add(line);
        }

        AnswerLines.Clear();
        foreach (var line in detail.StudentAnswer)
        {
            AnswerLines.Add(line);
        }

        Categories.Clear();
        foreach (var category in detail.Categories)
        {
            Categories.Add(category);
        }

        Assessments.Clear();
        // History is append-only and is what a teacher needs to see; the
        // reference block only carries the latest assessment per assessor.
        foreach (var assessment in detail.History.OrderByDescending(a => a.Seq))
        {
            Assessments.Add(new BenchmarkAssessmentRow(assessment, detail.MaxMarks));
        }

        OnPropertyChanged(nameof(DetailPaper));
        OnPropertyChanged(nameof(DetailIdentifiers));
        OnPropertyChanged(nameof(DetailLevel));
        OnPropertyChanged(nameof(DetailBoard));
        OnPropertyChanged(nameof(DetailPartition));
        OnPropertyChanged(nameof(DetailProvenance));
        OnPropertyChanged(nameof(DetailMaxText));
        OnPropertyChanged(nameof(DetailPrompt));
        OnPropertyChanged(nameof(DetailRuling));
        OnPropertyChanged(nameof(DetailBasis));
        OnPropertyChanged(nameof(DetailSchemeMarkText));
        OnPropertyChanged(nameof(DetailTeacherMarkText));
        OnPropertyChanged(nameof(DetailTeacherStatus));
        OnPropertyChanged(nameof(DetailEligibility));
        OnPropertyChanged(nameof(DetailIsEligible));
        OnPropertyChanged(nameof(DetailIsDisputed));
        OnPropertyChanged(nameof(DetailConflict));
        OnPropertyChanged(nameof(HasConflict));
        OnPropertyChanged(nameof(AiMarkText));
        OnPropertyChanged(nameof(AiMarkDetail));
        OnPropertyChanged(nameof(HasAiMark));
        OnPropertyChanged(nameof(HasAssessments));
        OnPropertyChanged(nameof(FormTitle));
    }

    private void ClearDetail()
    {
        Detail = null;
        SchemeLines.Clear();
        AnswerLines.Clear();
        Categories.Clear();
        Assessments.Clear();
        DetailError = null;
        OnPropertyChanged(nameof(HasAssessments));
        OnPropertyChanged(nameof(FormTitle));
    }

    private void RaiseFormChanged()
    {
        OnPropertyChanged(nameof(CanSubmit));
        OnPropertyChanged(nameof(ValidationHint));
        OnPropertyChanged(nameof(RequiresNote));
        OnPropertyChanged(nameof(RequiresMark));
        OnPropertyChanged(nameof(IsDisputeDecision));
        OnPropertyChanged(nameof(IsIncompleteDecision));
        SubmitCommand.RaiseCanExecuteChanged();
    }

    private async Task SubmitAsync()
    {
        var detail = _detail;
        var decision = _decision;
        if (detail is null || decision is null || !CanSubmit)
        {
            throw new InvalidOperationException(ValidationHint);
        }

        TryReadMark(out var mark);
        var alternatives = AlternativeText
            .Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)
            .Select(line => line.Trim())
            .Where(line => line.Length > 0)
            .ToList();

        var result = await Session.Api.VerifyBenchmarkReferenceAsync(
            detail.ReferenceId,
            decision.Code == "verified" ? mark : (TryReadMarkOrBlank(out var optional) ? optional : null),
            decision.Code,
            Assessor.Trim(),
            AssessorRole.Trim(),
            Note.Trim(),
            alternatives,
            IsConfirmed).ConfigureAwait(true);

        if (result.ProductionDbModified || result.ClassroomWrites != 0)
        {
            throw new InvalidOperationException(
                "The backend reported a production or Classroom write. The assessment was not "
                + "accepted as safe; inspect the backend before continuing.");
        }

        _drafts.Remove(detail.ReferenceId);
        _reproduction = null;
        ClearForm();
        WorkflowStatus = "Recorded. Re-read the reference and dataset below to see the backend's "
                         + "resolved status.";
    }

    private async Task ReproduceAsync()
    {
        var runId = _overview?.LatestRun?.RunId;
        if (string.IsNullOrWhiteSpace(runId))
        {
            return;
        }

        var result = await Session.Api.ReproduceBenchmarkRunAsync(runId).ConfigureAwait(true);
        _reproduction = result;
        WorkflowStatus = result.Reproduced
            ? "Recomputed metrics match the recorded run exactly."
            : "Recomputed metrics differ from the recorded run: " + string.Join(", ", result.Differences);
        OnPropertyChanged(nameof(LatestRunReproduction));
        OnPropertyChanged(nameof(CanReproduce));
        ReproduceCommand.RaiseCanExecuteChanged();
    }

    private bool TryReadMark(out int mark)
    {
        mark = 0;
        return int.TryParse(MarkText.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture,
                   out mark)
               && _detail is not null && mark >= 0 && mark <= _detail.MaxMarks;
    }

    private bool TryReadMarkOrBlank(out int? mark)
    {
        mark = null;
        if (string.IsNullOrWhiteSpace(MarkText))
        {
            return true;
        }

        if (TryReadMark(out var value))
        {
            mark = value;
            return true;
        }

        return false;
    }

    private int Count(string status) =>
        _overview?.Verification.Counts.TryGetValue(status, out var count) == true ? count : 0;

    private static string MetricText(BenchmarkAgreementBlockDto? block)
    {
        if (block is null || block.Compared == 0)
        {
            return "No measured denominator";
        }

        var ci = block.Ci95 is { Count: 2 }
            ? $" · 95% CI {block.Ci95[0] * 100:0.0}–{block.Ci95[1] * 100:0.0}%"
            : string.Empty;
        return $"{block.Exact}/{block.Compared} exact · {FormatPercent(block.Rate)}{ci}";
    }

    private static string TotalMetricText(BenchmarkTotalAgreementDto? block)
    {
        if (block is null || block.Compared == 0)
        {
            return "No fully covered script";
        }

        var ci = block.Ci95 is { Count: 2 }
            ? $" · 95% CI {block.Ci95[0] * 100:0.0}–{block.Ci95[1] * 100:0.0}%"
            : string.Empty;
        return $"{block.Exact}/{block.Compared} scripts exact · {FormatPercent(block.Rate)}{ci}";
    }

    private static string DirectionText(BenchmarkDirectionDto? direction, string verb)
    {
        if (direction is null || direction.Count == 0)
        {
            return "None";
        }

        return $"{FormatPercent(direction.Rate)} {verb}"
               + (direction.MeanMagnitude is null
                   ? string.Empty
                   : $" · mean {direction.MeanMagnitude:0.0#} marks");
    }

    private static string FormatPercent(double? rate) => rate is null
        ? "—"
        : $"{rate.Value * 100:0.0}%";

    private static string FormatMarks(double? value) => value is null
        ? "—"
        : $"{value.Value:0.00} marks";

    private static string BenchmarkMode(string? mode) => (mode ?? string.Empty)
        .ToLowerInvariant() switch
    {
        "live" => "Live provider inference (recorded run)",
        "stub" => "Deterministic stub (no provider inference)",
        "" => "Mode not recorded",
        _ => mode!,
    };

    public override void OnDeactivated()
    {
        _detailCts?.Cancel();
        base.OnDeactivated();
    }

    private sealed class BenchmarkDraft
    {
        public string Mark { get; init; } = string.Empty;
        public string Note { get; init; } = string.Empty;
        public string AlternativeText { get; init; } = string.Empty;
        public string Assessor { get; init; } = string.Empty;
        public string AssessorRole { get; init; } = string.Empty;
        public string DecisionCode { get; init; } = "verified";
    }
}
