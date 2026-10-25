using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Core.Workspace;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// The Marking Desk: the paper, the question rail and the decision panel for
/// one correction. Accept is local and never writes; changing a mark writes one
/// integer; approving is the durable whole-paper decision.
/// </summary>
public sealed class MarkingDeskViewModel : PageViewModel
{
    private readonly Stack<MarkChange> _changes = new();
    private CancellationTokenSource _lifetime = new();

    private ResultDetailDto? _detail;
    private VerificationDto? _verification;
    private ResultReviewHistoryDto? _reviews;
    private QuestionVm? _activeQuestion;
    private PaperProvenance _provenance = PaperProvenance.Ai;
    private bool _hasPaper;
    private bool _isApproving;
    private bool _isApprovePanelOpen;
    private bool _approveRequiresMark;
    private string? _approveError;
    private string _wholePaperMarkText = string.Empty;
    private bool _isShortcutsOpen;
    private int _acceptedCount;
    private int _changedCount;
    private int _toCheckCount;
    private int _attentionCount;
    private string _progressText = string.Empty;

    public MarkingDeskViewModel(AppSession session, IPageImageDecoder? paperDecoder = null)
        : base(session, "desk", "Marking desk")
    {
        Paper = new PaperPaneViewModel(session, decoder: paperDecoder);

        AcceptCommand = new RelayCommand(() => _ = AcceptAndAdvanceAsync());
        CommitDraftCommand = new RelayCommand(() => _ = CommitDraftAsync());
        StageMarkCommand = new RelayCommand(parameter =>
        {
            if (TryInteger(parameter, out var value))
            {
                StageMark(value);
            }
        });
        ClearDraftCommand = new RelayCommand(ClearDraft);
        UndoCommand = new RelayCommand(() => _ = UndoLastChangeAsync());
        ApproveCommand = new RelayCommand(OpenApprovePanel);
        ConfirmApproveCommand = new RelayCommand(() => _ = ApproveAsync());
        CancelApproveCommand = new RelayCommand(CloseApprovePanel);
        RemarkCommand = new RelayCommand(() => _ = RemarkAsync());
        ExportPdfCommand = new RelayCommand(() => _ = ExportAsync("pdf"));
        NextQuestionCommand = new RelayCommand(() => MoveQuestion(1));
        PreviousQuestionCommand = new RelayCommand(() => MoveQuestion(-1));
        NextPaperCommand = new RelayCommand(() => _ = MovePaperAsync(1));
        PreviousPaperCommand = new RelayCommand(() => _ = MovePaperAsync(-1));
        ToggleShortcutsCommand = new RelayCommand(() => IsShortcutsOpen = !IsShortcutsOpen);
        OpenResultsCommand = new RelayCommand(() => session.GoTo("results"));
    }

    // ------------------------------------------------------------------ identity

    public PaperPaneViewModel Paper { get; }

    public string? ResultId { get; private set; }

    public ResultDetailDto? Detail
    {
        get => _detail;
        private set
        {
            if (!SetProperty(ref _detail, value))
            {
                return;
            }

            OnPropertyChanged(nameof(StudentName));
            OnPropertyChanged(nameof(PaperLine));
            OnPropertyChanged(nameof(ScoreText));
            OnPropertyChanged(nameof(PercentageText));
            OnPropertyChanged(nameof(GradeText));
            OnPropertyChanged(nameof(WholePaperMax));
            OnPropertyChanged(nameof(IsPartial));
            OnPropertyChanged(nameof(CanApprove));
            OnPropertyChanged(nameof(ApproveHint));
        }
    }

    public VerificationDto? Verification => _verification;

    public string StudentName => Detail?.Name ?? "Student";

    public string PaperLine => string.IsNullOrWhiteSpace(Detail?.ClassLabel)
        ? "Correction result"
        : Detail!.ClassLabel!;

    public string ScoreText => Detail?.ScoreText ?? "—";

    public string PercentageText => Detail?.PercentageText ?? string.Empty;

    public string GradeText => Detail?.ProvisionalGrade is { Length: > 0 } grade
        ? $"Grade {grade}"
        : string.Empty;

    public bool HasPaper
    {
        get => _hasPaper;
        private set
        {
            if (SetProperty(ref _hasPaper, value))
            {
                OnPropertyChanged(nameof(HasNoPaper));
            }
        }
    }

    public bool HasNoPaper => !HasPaper;

    // ---------------------------------------------------------------- provenance

    public string ProvenanceLabel => _provenance.Label;
    public string ProvenanceGlyph => _provenance.Glyph;
    public string ProvenanceTone => _provenance.Tone;
    public bool IsApproved => _provenance.IsApproved;

    // ----------------------------------------------------------------- questions

    public ObservableCollection<QuestionVm> Questions { get; } = new();

    public ObservableCollection<MarkChoiceVm> MarkChoices { get; } = new();

    public QuestionVm? ActiveQuestion
    {
        get => _activeQuestion;
        set
        {
            if (!SetProperty(ref _activeQuestion, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasActiveQuestion));
            OnPropertyChanged(nameof(HasNoActiveQuestion));
            OnPropertyChanged(nameof(QuestionPosition));
            OnPropertyChanged(nameof(QuestionError));
            OnPropertyChanged(nameof(HasQuestionError));
            RebuildMarkChoices();
        }
    }

    public bool HasActiveQuestion => ActiveQuestion is not null;

    public bool HasNoActiveQuestion => ActiveQuestion is null;

    public bool HasQuestionData => Questions.Count > 0;

    public bool HasNoQuestionData => !HasQuestionData;

    public string QuestionPosition
    {
        get
        {
            if (ActiveQuestion is null)
            {
                return string.Empty;
            }

            var index = Questions.IndexOf(ActiveQuestion);
            return index < 0 ? string.Empty : $"{index + 1} of {Questions.Count}";
        }
    }

    public string? QuestionError => ActiveQuestion?.Error;

    public bool HasQuestionError => !string.IsNullOrWhiteSpace(QuestionError);

    public bool HasDraft => ActiveQuestion?.HasDraft == true;

    public string SetMarkText => ActiveQuestion?.Draft is { } draft ? $"Set {draft}" : "Set mark";

    public bool CanStage => ActiveQuestion?.CanMark == true;

    // -------------------------------------------------------------------- counts

    public int AcceptedCount => _acceptedCount;
    public int ChangedCount => _changedCount;
    public int ToCheckCount => _toCheckCount;
    public int AttentionCount => _attentionCount;

    public string ProgressText => _progressText;

    public bool HasChanges => _changedCount > 0;

    // -------------------------------------------------------------------- approval

    public bool IsApproving
    {
        get => _isApproving;
        private set
        {
            if (SetProperty(ref _isApproving, value))
            {
                OnPropertyChanged(nameof(CanApprove));
            }
        }
    }

    public bool IsApprovePanelOpen
    {
        get => _isApprovePanelOpen;
        private set => SetProperty(ref _isApprovePanelOpen, value);
    }

    public bool ApproveRequiresMark
    {
        get => _approveRequiresMark;
        private set
        {
            if (SetProperty(ref _approveRequiresMark, value))
            {
                OnPropertyChanged(nameof(ApproveHint));
            }
        }
    }

    public string? ApproveError
    {
        get => _approveError;
        private set
        {
            if (SetProperty(ref _approveError, value))
            {
                OnPropertyChanged(nameof(HasApproveError));
            }
        }
    }

    public bool HasApproveError => !string.IsNullOrWhiteSpace(ApproveError);

    public string WholePaperMarkText
    {
        get => _wholePaperMarkText;
        set
        {
            if (SetProperty(ref _wholePaperMarkText, value))
            {
                ApproveError = null;
            }
        }
    }

    /// <summary>
    /// For an incomplete correction the ceiling is the paper's real total, not
    /// the marked subset: validating against <c>Available</c> would reject the
    /// very mark the backend requires.
    /// </summary>
    public int? WholePaperMax => IsPartial
        ? (int?)Verification?.PaperTotalMarks
        : (int?)Detail?.Available;

    public bool IsPartial => Verification?.Partial == true;

    public bool CanApprove => Detail is not null && !IsApproved && !IsApproving;

    public string ApproveHint
    {
        get
        {
            if (IsApproved)
            {
                return "This paper is approved. The engine's marks are now a teacher decision.";
            }

            if (IsPartial || ApproveRequiresMark)
            {
                return $"This correction is incomplete. Enter the student's mark out of "
                       + $"{WholePaperMax?.ToString() ?? "?"} to approve it.";
            }

            return "Approving records a durable teacher decision. No per-question override is written.";
        }
    }

    public bool IsShortcutsOpen
    {
        get => _isShortcutsOpen;
        private set => SetProperty(ref _isShortcutsOpen, value);
    }

    public string ShortcutText =>
        "N / P  next / previous question\n"
        + "Shift+N / Shift+P  next / previous paper\n"
        + "Enter  set staged mark, or accept when nothing is staged\n"
        + "Space  accept (paper pane focused)\n"
        + "0–9  stage that mark\n"
        + "Backspace / Esc  clear the staged mark\n"
        + "A  approve paper    R  re-mark with AI    E  export PDF\n"
        + "Ctrl+Z  undo the last change    Ctrl+ +/−/0  zoom\n"
        + "PgUp / PgDn  previous / next page    F6  cycle panes";

    // ------------------------------------------------------------------- commands

    public RelayCommand AcceptCommand { get; }
    public RelayCommand CommitDraftCommand { get; }
    public RelayCommand StageMarkCommand { get; }
    public RelayCommand ClearDraftCommand { get; }
    public RelayCommand UndoCommand { get; }
    public RelayCommand ApproveCommand { get; }
    public RelayCommand ConfirmApproveCommand { get; }
    public RelayCommand CancelApproveCommand { get; }
    public RelayCommand RemarkCommand { get; }
    public RelayCommand ExportPdfCommand { get; }
    public RelayCommand NextQuestionCommand { get; }
    public RelayCommand PreviousQuestionCommand { get; }
    public RelayCommand NextPaperCommand { get; }
    public RelayCommand PreviousPaperCommand { get; }
    public RelayCommand ToggleShortcutsCommand { get; }
    public RelayCommand OpenResultsCommand { get; }

    // ------------------------------------------------------------------ lifecycle

    protected override async Task LoadCoreAsync()
    {
        var pending = Session.TakePendingResultId();
        var resultId = pending ?? ResultId;
        if (string.IsNullOrWhiteSpace(resultId))
        {
            HasPaper = false;
            return;
        }

        await OpenAsync(resultId);
    }

    public async Task OpenAsync(string resultId, string? questionNumber = null,
        CancellationToken cancellationToken = default)
    {
        CancelLifetime();
        _lifetime = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        var token = _lifetime.Token;

        ResultId = resultId;
        HasPaper = true;
        Detail = null;
        _verification = null;
        _reviews = null;
        Error = null;
        ApproveError = null;
        IsApprovePanelOpen = false;
        ApproveRequiresMark = false;
        WholePaperMarkText = string.Empty;
        _changes.Clear();
        Questions.Clear();
        ActiveQuestion = null;
        MarkChoices.Clear();
        OnPropertyChanged(nameof(HasQuestionData));
        OnPropertyChanged(nameof(HasNoQuestionData));
        RefreshCounts();
        SetProvenance(PaperProvenance.Ai);

        // The paper and the marks are independent requests: a missing original
        // must never hide the marks, and a detail failure must not erase the page.
        var paperTask = Paper.OpenAsync(resultId, token);

        try
        {
            var detail = await Session.Api.ResultAsync(resultId, token).ConfigureAwait(true);
            token.ThrowIfCancellationRequested();

            Detail = detail;
            _verification = detail.Verification();
            OnPropertyChanged(nameof(Verification));
            OnPropertyChanged(nameof(GradeText));
            OnPropertyChanged(nameof(WholePaperMax));
            OnPropertyChanged(nameof(IsPartial));
            OnPropertyChanged(nameof(ApproveHint));

            BuildQuestions(detail);
            SelectInitial(questionNumber);
            RefreshCounts();
            RefreshProvenance();
        }
        catch (OperationCanceledException)
        {
            // Navigating away or opening another paper.
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
            OnPropertyChanged(nameof(HasQuestionData));
        }
        catch (Exception ex)
        {
            Session.Log.Error("marking desk detail failed", ex);
            Error = "The correction could not be loaded: " + ex.Message;
        }

        await paperTask.ConfigureAwait(true);

        if (token.IsCancellationRequested || Detail is null)
        {
            return;
        }

        await LoadReviewHistoryAsync(token).ConfigureAwait(true);
        RefreshProvenance();
    }

    public override void OnDeactivated()
    {
        CancelLifetime();
        base.OnDeactivated();
    }

    private void CancelLifetime()
    {
        try
        {
            _lifetime.Cancel();
        }
        catch (ObjectDisposedException)
        {
            // already replaced
        }
    }

    // ------------------------------------------------------------------ decisions

    /// <summary>Enter with a draft commits it; otherwise Enter accepts and advances.</summary>
    public Task AcceptOrCommitAsync() => HasDraft ? CommitDraftAsync() : AcceptAndAdvanceAsync();

    /// <summary>Accept is local. It issues no HTTP request and advances the rail.</summary>
    public Task AcceptAndAdvanceAsync()
    {
        if (ActiveQuestion is not { } question || question.IsSaving)
        {
            return Task.CompletedTask;
        }

        question.Accept();
        question.ClearDraft();
        RebuildMarkChoices();
        RefreshCounts();
        var moved = MoveQuestion(1);
        Status = moved
            ? $"Question {question.Number} accepted."
            : $"Question {question.Number} accepted. This is the last question.";
        return Task.CompletedTask;
    }

    public void StageMark(int value)
    {
        if (ActiveQuestion is not { } question || !question.CanMark)
        {
            return;
        }

        question.Stage(value);
        RebuildMarkChoices();
        OnPropertyChanged(nameof(HasDraft));
    }

    public void ClearDraft()
    {
        if (ActiveQuestion is not { } question)
        {
            return;
        }

        question.ClearDraft();
        RebuildMarkChoices();
        OnPropertyChanged(nameof(HasDraft));
    }

    /// <summary>One integer POST, the only per-question write in the app.</summary>
    public async Task CommitDraftAsync()
    {
        if (ResultId is not { } resultId || ActiveQuestion is not { } question)
        {
            return;
        }

        if (!question.HasDraft)
        {
            await AcceptAndAdvanceAsync().ConfigureAwait(true);
            return;
        }

        if (!question.CanMark)
        {
            question.FailSave(question.MaxZeroText);
            OnPropertyChanged(nameof(QuestionError));
            OnPropertyChanged(nameof(HasQuestionError));
            return;
        }

        var draft = question.Draft!.Value;
        if (draft == question.Awarded)
        {
            // Staging the recorded value and pressing Enter is an accept, not a
            // fabricated override.
            question.ClearDraft();
            await AcceptAndAdvanceAsync().ConfigureAwait(true);
            return;
        }

        var previous = question.Awarded;
        question.BeginSave();
        RebuildMarkChoices();

        try
        {
            var response = await Session.Api
                .SaveQuestionMarkAsync(resultId, question.Number, draft, "Set in the marking desk")
                .ConfigureAwait(true);

            ApplyTotals(response);
            question.ApplyChange(draft, ReadInteger(response, "previous"));
            _changes.Push(new MarkChange(question, previous));
            if (Detail is not null)
            {
                Detail.ReviewRequired = 0;
                Detail.ManualOverride = 1;
                Detail.Confidence = "HIGH";
            }

            RefreshCounts();
            RefreshProvenance();
            RebuildMarkChoices();
            Status = $"Question {question.Number} saved as {draft} of {question.Max}.";
            MoveQuestion(1);
        }
        catch (OperationCanceledException)
        {
            question.FailSave("The save was cancelled.");
        }
        catch (ApiException ex)
        {
            question.FailSave(ex.Display);
        }
        catch (Exception ex)
        {
            Session.Log.Error("question mark save failed", ex);
            question.FailSave("The mark was not saved: " + ex.Message);
        }
        finally
        {
            RebuildMarkChoices();
            OnPropertyChanged(nameof(QuestionError));
            OnPropertyChanged(nameof(HasQuestionError));
        }
    }

    public async Task UndoLastChangeAsync()
    {
        if (ResultId is not { } resultId)
        {
            return;
        }

        if (_changes.Count == 0)
        {
            Status = "No mark change to undo in this session.";
            return;
        }

        var change = _changes.Pop();
        var question = change.Question;
        question.BeginSave();

        try
        {
            var response = await Session.Api
                .SaveQuestionMarkAsync(resultId, question.Number, change.Previous,
                    "Undone in the marking desk")
                .ConfigureAwait(true);

            ApplyTotals(response);
            question.ApplyUndo(change.Previous);
            ActiveQuestion = question;
            RefreshCounts();
            RebuildMarkChoices();
            Status = $"Question {question.Number} set back to {change.Previous} of {question.Max}.";
        }
        catch (Exception ex)
        {
            // The undo did not happen; keep it available.
            _changes.Push(change);
            var message = ex is ApiException api ? api.Display : ex.Message;
            question.FailSave("The change could not be undone: " + message);
            OnPropertyChanged(nameof(QuestionError));
            OnPropertyChanged(nameof(HasQuestionError));
        }
    }

    // -------------------------------------------------------------------- approve

    public void OpenApprovePanel()
    {
        if (!CanApprove)
        {
            return;
        }

        IsApprovePanelOpen = true;
        ApproveError = null;
        ApproveRequiresMark = IsPartial;
        OnPropertyChanged(nameof(ApproveHint));
    }

    public void CloseApprovePanel()
    {
        IsApprovePanelOpen = false;
        ApproveError = null;
    }

    public async Task ApproveAsync()
    {
        if (ResultId is not { } resultId || Detail is null || IsApproving)
        {
            return;
        }

        if (IsApproved)
        {
            IsApprovePanelOpen = false;
            return;
        }

        int? mark = null;
        var text = WholePaperMarkText.Trim();
        if (text.Length > 0)
        {
            if (!int.TryParse(text, out var parsed))
            {
                ApproveError = "The whole-paper mark must be a whole number.";
                return;
            }

            if (WholePaperMax is { } ceiling && (parsed < 0 || parsed > ceiling))
            {
                ApproveError = $"The mark must be between 0 and {ceiling}.";
                return;
            }

            mark = parsed;
        }

        if ((IsPartial || ApproveRequiresMark) && mark is null)
        {
            ApproveRequiresMark = true;
            ApproveError = "This correction is incomplete. Enter the student's mark out of "
                           + (WholePaperMax?.ToString() ?? "?") + " to approve it.";
            return;
        }

        IsApproving = true;
        ApproveError = null;

        try
        {
            await Session.Api.ApproveResultAsync(resultId, mark, "Approved in the marking desk")
                .ConfigureAwait(true);
            IsApprovePanelOpen = false;
            Status = "Paper approved. The engine's marks are now a teacher decision.";
            await ReloadDetailAsync(_lifetime.Token).ConfigureAwait(true);
            RefreshProvenance();
            RefreshCounts();
        }
        catch (ApiException ex) when (ex.Status == 409
                                     || string.Equals(ex.Code, "partial_correction",
                                         StringComparison.OrdinalIgnoreCase))
        {
            ApproveRequiresMark = true;
            ApproveError = ex.Message;
        }
        catch (ApiException ex)
        {
            ApproveError = ex.Display;
        }
        catch (OperationCanceledException)
        {
            // The desk moved on.
        }
        catch (Exception ex)
        {
            Session.Log.Error("approve failed", ex);
            ApproveError = "The paper was not approved: " + ex.Message;
        }
        finally
        {
            IsApproving = false;
            OnPropertyChanged(nameof(ApproveHint));
        }
    }

    public async Task RemarkAsync()
    {
        if (ResultId is not { } resultId)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Re-mark with AI",
                "This starts a new AI attempt for this paper. The current marks stay active until "
                + "you choose the new attempt. Re-mark now?"))
        {
            return;
        }

        try
        {
            var result = await Session.Api.RemarkAsync(resultId).ConfigureAwait(true);
            Status = result.Remarked
                ? "Re-mark queued; the current marks stay active."
                : "A re-mark is already in progress for this paper.";
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
        catch (Exception ex)
        {
            Error = "The re-mark could not be started: " + ex.Message;
        }
    }

    public async Task ExportAsync(string format)
    {
        if (ResultId is not { } resultId)
        {
            return;
        }

        try
        {
            // "detailed" is the report that carries the per-question blocks, and
            // with them each subquestion's own mark; the standard PDF stops at
            // the summary table.
            var file = await Session.Api.ExportResultAsync(resultId, format,
                detail: "detailed").ConfigureAwait(true);
            var filter = format switch
            {
                "pdf" => "PDF (*.pdf)|*.pdf",
                "csv" => "CSV (*.csv)|*.csv",
                _ => "Markdown (*.md)|*.md",
            };
            var saved = DownloadSaver.Save(Session.Settings, Session.Dialogs, file, filter,
                "Save correction report", openAfter: format == "pdf");
            if (!string.IsNullOrEmpty(saved))
            {
                Session.Notify($"{format.ToUpperInvariant()} report saved: {saved}");
            }
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
        catch (Exception ex)
        {
            Error = "The report could not be exported: " + ex.Message;
        }
    }

    // ----------------------------------------------------------------- navigation

    public bool MoveQuestion(int direction)
    {
        if (ActiveQuestion is null || Questions.Count == 0)
        {
            return false;
        }

        var index = Questions.IndexOf(ActiveQuestion) + direction;
        if (index < 0 || index >= Questions.Count)
        {
            return false;
        }

        ActiveQuestion = Questions[index];
        return true;
    }

    public async Task MovePaperAsync(int direction)
    {
        if (IsBusy || ResultId is null)
        {
            return;
        }

        if (HasChanges && !IsApproved
            && !Session.Dialogs.Confirm("Leave this paper?",
                $"{ChangedCount} mark{(ChangedCount == 1 ? string.Empty : "s")} saved, but the paper "
                + "is not approved. Move to another paper anyway?"))
        {
            return;
        }

        try
        {
            var list = await Session.Api.ResultsAsync(limit: 300).ConfigureAwait(true);
            var index = list.Results.FindIndex(result => result.Id == ResultId);
            if (index < 0)
            {
                Status = "This paper is not in the current results list.";
                return;
            }

            var target = index + direction;
            if (target < 0 || target >= list.Results.Count)
            {
                Status = direction > 0 ? "This is the last paper in the list." : "This is the first paper.";
                return;
            }

            await OpenAsync(list.Results[target].Id).ConfigureAwait(true);
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
        catch (Exception ex)
        {
            Error = "The next paper could not be opened: " + ex.Message;
        }
    }

    // zoom is delegated to the paper pane so the desk's keyboard map has one target
    public void ZoomIn() => Paper.ZoomInCommand.Execute(null);
    public void ZoomOut() => Paper.ZoomOutCommand.Execute(null);
    public void FitWidth() => Paper.FitWidthCommand.Execute(null);

    // ------------------------------------------------------------------- internals

    private void BuildQuestions(ResultDetailDto detail)
    {
        Questions.Clear();
        var source = detail.Questions()
            .OrderBy(question => question.NumberText, QuestionNumberComparer.Instance)
            .ToList();

        foreach (var question in source)
        {
            Questions.Add(new QuestionVm(question, _verification?.Verified == true));
        }

        OnPropertyChanged(nameof(HasQuestionData));
        OnPropertyChanged(nameof(HasNoQuestionData));
    }

    private void SelectInitial(string? questionNumber)
    {
        if (Questions.Count == 0)
        {
            ActiveQuestion = null;
            return;
        }

        if (!string.IsNullOrWhiteSpace(questionNumber))
        {
            var requested = Questions.FirstOrDefault(question =>
                string.Equals(question.Number, questionNumber.Trim(), StringComparison.OrdinalIgnoreCase));
            if (requested is not null)
            {
                ActiveQuestion = requested;
                return;
            }
        }

        ActiveQuestion = Questions.FirstOrDefault(question =>
                           question.State is not (QuestionState.Ai or QuestionState.Verified))
                       ?? Questions[0];
    }

    private void RebuildMarkChoices()
    {
        MarkChoices.Clear();
        if (ActiveQuestion is { } question)
        {
            for (var value = 0; value <= Math.Max(0, question.Max); value++)
            {
                MarkChoices.Add(new MarkChoiceVm(value, value == question.Awarded,
                    question.Draft == value));
            }
        }

        OnPropertyChanged(nameof(HasDraft));
        OnPropertyChanged(nameof(SetMarkText));
        OnPropertyChanged(nameof(CanStage));
        OnPropertyChanged(nameof(QuestionError));
        OnPropertyChanged(nameof(HasQuestionError));
    }

    private void RefreshCounts()
    {
        _acceptedCount = Questions.Count(question =>
            !question.IsChanged && (question.IsAccepted || question.State == QuestionState.Yours));
        _changedCount = Questions.Count(question => question.IsChanged);
        _toCheckCount = Math.Max(0, Questions.Count - _acceptedCount - _changedCount);
        _attentionCount = Questions.Count(question =>
            question.IsAttention && !question.IsAccepted && !question.IsChanged);

        _progressText = Questions.Count == 0
            ? string.Empty
            : $"{_acceptedCount} accepted · {_changedCount} changed · {_toCheckCount} to check";

        OnPropertyChanged(nameof(AcceptedCount));
        OnPropertyChanged(nameof(ChangedCount));
        OnPropertyChanged(nameof(ToCheckCount));
        OnPropertyChanged(nameof(AttentionCount));
        OnPropertyChanged(nameof(ProgressText));
        OnPropertyChanged(nameof(HasChanges));
    }

    private void RefreshProvenance()
    {
        var latest = _reviews?.History.FirstOrDefault();
        SetProvenance(DeskProvenance.ForPaper(Detail, Verification, latest));
    }

    private void SetProvenance(PaperProvenance provenance)
    {
        _provenance = provenance;
        OnPropertyChanged(nameof(ProvenanceLabel));
        OnPropertyChanged(nameof(ProvenanceGlyph));
        OnPropertyChanged(nameof(ProvenanceTone));
        OnPropertyChanged(nameof(IsApproved));
        OnPropertyChanged(nameof(CanApprove));
        OnPropertyChanged(nameof(ApproveHint));
    }

    private async Task LoadReviewHistoryAsync(CancellationToken token)
    {
        if (ResultId is not { } resultId)
        {
            return;
        }

        try
        {
            _reviews = await Session.Api.ResultReviewHistoryAsync(resultId, token)
                .ConfigureAwait(true);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            // Provenance without history is still honest: it falls back to the
            // confidence/coverage fields instead of claiming an approval.
            Session.Log.Warn("review history unavailable: " + ex.Message);
            _reviews = null;
        }
    }

    private async Task ReloadDetailAsync(CancellationToken token)
    {
        if (ResultId is not { } resultId)
        {
            return;
        }

        var detail = await Session.Api.ResultAsync(resultId, token).ConfigureAwait(true);
        Detail = detail;
        _verification = detail.Verification();
        OnPropertyChanged(nameof(Verification));
        OnPropertyChanged(nameof(WholePaperMax));
        OnPropertyChanged(nameof(IsPartial));
        OnPropertyChanged(nameof(ApproveHint));
        await LoadReviewHistoryAsync(token).ConfigureAwait(true);
    }

    private void ApplyTotals(JsonElement response)
    {
        if (Detail is null)
        {
            return;
        }

        if (ReadInteger(response, "earned") is { } earned)
        {
            Detail.Earned = earned;
        }

        if (ReadInteger(response, "available") is { } available)
        {
            Detail.Available = available;
        }

        if (ReadDouble(response, "percentage") is { } percentage)
        {
            Detail.Percentage = percentage;
        }

        OnPropertyChanged(nameof(ScoreText));
        OnPropertyChanged(nameof(PercentageText));
    }

    private static int? ReadInteger(JsonElement element, string name)
    {
        if (!element.TryGetProperty(name, out var value))
        {
            return null;
        }

        return value.ValueKind switch
        {
            JsonValueKind.Number when value.TryGetInt32(out var number) => number,
            JsonValueKind.Number => (int)Math.Round(value.GetDouble()),
            JsonValueKind.String when int.TryParse(value.GetString(), out var parsed) => parsed,
            _ => null,
        };
    }

    private static double? ReadDouble(JsonElement element, string name)
    {
        if (!element.TryGetProperty(name, out var value))
        {
            return null;
        }

        return value.ValueKind switch
        {
            JsonValueKind.Number => value.GetDouble(),
            JsonValueKind.String when double.TryParse(value.GetString(), out var parsed) => parsed,
            _ => null,
        };
    }

    private static bool TryInteger(object? parameter, out int value)
    {
        switch (parameter)
        {
            case int number:
                value = number;
                return true;
            case string text when int.TryParse(text, out var parsed):
                value = parsed;
                return true;
            default:
                value = 0;
                return false;
        }
    }

    private sealed record MarkChange(QuestionVm Question, int Previous);

    /// <summary>"4(b)" must sort after "3", not before "10".</summary>
    private sealed class QuestionNumberComparer : IComparer<string>
    {
        public static readonly QuestionNumberComparer Instance = new();

        public int Compare(string? x, string? y)
        {
            var left = x ?? string.Empty;
            var right = y ?? string.Empty;
            var leftNumber = LeadingNumber(left, out var leftRest);
            var rightNumber = LeadingNumber(right, out var rightRest);

            if (leftNumber.HasValue && rightNumber.HasValue && leftNumber != rightNumber)
            {
                return leftNumber.Value.CompareTo(rightNumber.Value);
            }

            if (leftNumber.HasValue != rightNumber.HasValue)
            {
                return leftNumber.HasValue ? -1 : 1;
            }

            return string.Compare(leftRest, rightRest, StringComparison.OrdinalIgnoreCase);
        }

        private static int? LeadingNumber(string value, out string rest)
        {
            var length = 0;
            while (length < value.Length && char.IsDigit(value[length]))
            {
                length++;
            }

            rest = length == 0 ? value : value[length..];
            return length == 0 ? null : int.Parse(value[..length]);
        }
    }
}
