using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Mvvm;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One handling option, with the wording a teacher reads. The code
/// travels to the API; the label never does.</summary>
public sealed class DecisionOption
{
    public string Code { get; init; } = string.Empty;
    public string Label { get; init; } = string.Empty;
    public override string ToString() => Label;
}

/// <summary>
/// One question or sub-question of an assignment's mark scheme, with what the
/// scheme says, how far it can be trusted, and how it is to be handled.
///
/// The decision is settable from the row itself (a combo box in the grid) so a
/// long scheme can be reviewed without opening every question, and from the
/// detail panel of the selected row. Both go through the same callback, so
/// there is exactly one code path that saves a decision.
/// </summary>
public sealed class MarkSchemeRow : ObservableObject
{
    private string _decision = "AUTO";
    private string _note = string.Empty;
    private string? _answerText;

    /// <summary>Set while the page is filling the row from the backend, so a
    /// load does not look like the teacher changing their mind.</summary>
    internal bool Quiet { get; set; }

    internal Action<MarkSchemeRow>? Changed { get; set; }

    public string QuestionKey { get; init; } = string.Empty;
    public string Label { get; init; } = string.Empty;
    public int Depth { get; init; }
    public int MaxMarks { get; init; }
    public int? Page { get; init; }
    public bool Reliable { get; init; }
    public string ExtractionStatus { get; init; } = string.Empty;

    public List<string> Answers { get; init; } = new();
    public string Criteria { get; init; } = string.Empty;

    /// <summary>True once the teacher has actually chosen a handling decision;
    /// false while the row still carries the proposed default.</summary>
    public bool Decided { get; set; }

    public bool Skipped { get; set; }

    /// <summary>Indent for the hierarchy: a sub-question sits under its
    /// question, so a long scheme reads as a tree rather than a flat list.</summary>
    public string LabelIndent => Depth > 0 ? "      " : string.Empty;

    /// <summary>The label as the grid prints it: a sub-question is indented
    /// under its question, so a long scheme reads as a tree.</summary>
    public string DisplayLabel => Depth > 0 ? $"    ↳ {Label}" : Label;

    public string KindText => Depth > 0 ? "Sub-question" : "Question";

    public string MarksText => MaxMarks == 1 ? "1 mark" : $"{MaxMarks} marks";

    /// <summary>This row came from Demo Mode's synthetic structure rather than
    /// from the attached scheme.</summary>
    public bool IsDemo => ExtractionStatus == "DEMO";

    /// <summary>What the grid shows is still Demo Mode's placeholder, so it is
    /// styled as a placeholder rather than as an answer. An answer the teacher
    /// typed is theirs and is never dressed up as fake.</summary>
    public bool AnswerIsPlaceholder => IsDemo && !IsEdited;

    public string WhereText => Page is null ? string.Empty : $"scheme page {Page}";

    /// <summary>What the marker will use: the teacher's edit when there is one,
    /// otherwise the extracted answer. Never invents one.</summary>
    public string EffectiveAnswer
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(_answerText))
            {
                return _answerText!;
            }

            return Answers.Count > 0 ? Answers[0] : string.Empty;
        }
    }

    public string AnswerText
    {
        get => _answerText ?? string.Empty;
        set
        {
            var clean = string.IsNullOrWhiteSpace(value) ? null : value;
            if (SetProperty(ref _answerText, clean))
            {
                OnPropertyChanged(nameof(EffectiveAnswer));
                OnPropertyChanged(nameof(IsEdited));
                OnPropertyChanged(nameof(AnswerSummary));
                OnPropertyChanged(nameof(AnswerIsPlaceholder));
            }
        }
    }

    public bool IsEdited => _answerText is not null;

    /// <summary>One line for the grid: the answer, or why there isn't one.</summary>
    public string AnswerSummary
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(EffectiveAnswer))
            {
                return IsEdited ? $"{EffectiveAnswer}   (edited)" : EffectiveAnswer;
            }

            return ExtractionStatus switch
            {
                "MISSING" => "No answer or guidance in the scheme",
                "CRITERIA_ONLY" => "Marking guidance only — no answer to check",
                _ => "No answer extracted",
            };
        }
    }

    /// <summary>The state in one phrase, for the status chip.</summary>
    public string StateText => Skipped
        ? "Skipped"
        : Decision switch
        {
            "MANUAL" => "Manual check",
            "AUTO" => "Automatic",
            _ => Decision,
        };

    public string StatusText => ExtractionStatus switch
    {
        "RELIABLE" => "answer extracted",
        "CRITERIA_ONLY" => "guidance only",
        "MISSING" => "nothing extracted",
        "DEMO" => "demo structure",
        _ => ExtractionStatus,
    };

    /// <summary>True when the row needs a person to look at it: no decision yet,
    /// or the extraction could not be trusted.</summary>
    public bool NeedsAttention => !Decided || !Reliable;

    public string Note
    {
        get => _note;
        set => SetProperty(ref _note, value);
    }

    public string Decision
    {
        get => _decision;
        set
        {
            if (!SetProperty(ref _decision, value ?? "AUTO"))
            {
                return;
            }

            OnPropertyChanged(nameof(StateText));
            if (!Quiet)
            {
                Changed?.Invoke(this);
            }
        }
    }

    public override string ToString() =>
        Depth > 0 ? $"Sub-question {Label}" : $"Question {Label}";
}

/// <summary>One recorded change, as the review page lists it.</summary>
public sealed class DecisionHistoryRow
{
    public string When { get; init; } = string.Empty;
    public string What { get; init; } = string.Empty;
    public string Reason { get; init; } = string.Empty;
    public string Where { get; init; } = string.Empty;

    public override string ToString() => What;
}

/// <summary>
/// The Mark scheme page: every question and sub-question of one assignment's
/// mark scheme, with the expected answer, how far the extraction can be
/// trusted, and the handling decision that correction will obey.
///
/// Nothing is decided on the teacher's behalf. A row with no explicit decision
/// shows the PROPOSED default and stays in the "unresolved" count until it is
/// decided or acknowledged at sign-off, and a skipped question is written
/// through the same exclusion table the marking worker already reads — so the
/// number on this screen and the paper that gets marked cannot disagree.
/// </summary>
public sealed class MarkSchemeViewModel : PageViewModel
{
    private const string FilterAll = "All questions";
    private const string FilterAutomatic = "Automatic";
    private const string FilterManual = "Manual check";
    private const string FilterSkipped = "Skipped";
    private const string FilterUnresolved = "No decision yet";
    private const string FilterUncertain = "Untrusted extraction";

    private readonly List<MarkSchemeRow> _all = new();
    private string _courseworkId = string.Empty;
    private string _assignmentTitle = string.Empty;
    private string _search = string.Empty;
    private string _filter = FilterAll;
    private MarkSchemeRow? _selected;
    private string _answerDraft = string.Empty;
    private string _noteDraft = string.Empty;
    private string _summaryLine = "No mark scheme is open.";
    private string _countsLine = string.Empty;
    private int _unresolved;
    private int _automatic;
    private int _manual;
    private int _skipped;
    private int _totalMarks;
    private int _skippedMarks;
    private int _markedMarks;
    private string _finalizeStatus = string.Empty;
    private bool _needsRead;
    private string _readHint = string.Empty;
    private bool _lastReadSucceeded;
    private string _demoNotice = string.Empty;
    private bool _demo;

    public MarkSchemeViewModel(AppSession session)
        : base(session, "markscheme", "Mark scheme")
    {
        Group = "People";
        DecisionOptions = new List<DecisionOption>
        {
            new() { Code = "AUTO", Label = "Mark automatically" },
            new() { Code = "MANUAL", Label = "Manual independent check" },
            new() { Code = "SKIP", Label = "Skip question" },
        };
        Filters = new List<string>
        {
            FilterAll, FilterAutomatic, FilterManual, FilterSkipped,
            FilterUnresolved, FilterUncertain,
        };
        SaveAnswerCommand = Command(SaveAnswerAsync, () => HasSelection);
        RestoreAnswerCommand = Command(RestoreAnswerAsync,
            () => HasSelection && Selected!.IsEdited);
        SaveNoteCommand = Command(SaveNoteAsync, () => HasSelection);
        FinalizeCommand = Command(FinalizeAsync, () => _all.Count > 0);
        SelectDecisionCommand = Command(SelectDecisionAsync);
        ReadSchemeCommand = Command(ReadSchemeAsync, () => NeedsRead,
            after: AfterReadAsync);
    }

    /// <summary>Every question and sub-question, filtered by the grid's controls.</summary>
    public ObservableCollection<MarkSchemeRow> Questions { get; } = new();

    public ObservableCollection<DecisionHistoryRow> History { get; } = new();

    public IReadOnlyList<DecisionOption> DecisionOptions { get; }

    public IReadOnlyList<string> Filters { get; }

    public AsyncCommand SaveAnswerCommand { get; }
    public AsyncCommand RestoreAnswerCommand { get; }
    public AsyncCommand SaveNoteCommand { get; }
    public AsyncCommand FinalizeCommand { get; }
    public AsyncCommand SelectDecisionCommand { get; }

    /// <summary>Reads the attached PDF into questions, on demand.
    ///
    /// Without this the page could only ever be opened AFTER a paper had been
    /// corrected: the structure is produced as a side effect of a correction. A
    /// teacher who had just uploaded a scheme had nothing to review and no way
    /// to make one.</summary>
    public AsyncCommand ReadSchemeCommand { get; }

    /// <summary>True while the scheme is on disk but has not been read, which is
    /// the state the Read button exists for.</summary>
    public bool NeedsRead
    {
        get => _needsRead;
        private set
        {
            if (SetProperty(ref _needsRead, value))
            {
                OnPropertyChanged(nameof(EmptyQuestionsText));
            }
        }
    }

    /// <summary>What an empty question list means right now. "Open an assignment"
    /// is wrong while an assignment with an unread scheme is on screen: there is
    /// nothing to review YET, and one click away from having everything.</summary>
    /// <remarks>
    /// The no-assignment branch completes the sentence in the summary card above
    /// rather than repeating it: the page said the same thing twice, once at the
    /// top and once in the empty list.
    /// </remarks>
    public string EmptyQuestionsText => NeedsRead
        ? "Read the mark scheme to list its questions and decide how each one is handled."
        : "Its questions appear here once an assignment is open.";

    /// <summary>What reading will do, or why it produced nothing.</summary>
    public string ReadHint
    {
        get => _readHint;
        private set => SetProperty(ref _readHint, value);
    }

    /// <summary>Says, in the page's own voice, that this structure came from
    /// Demo Mode and not from a read PDF. Empty when the scheme is real, so a
    /// normal scheme shows no notice at all.</summary>
    public string DemoNotice
    {
        get => _demoNotice;
        private set
        {
            if (SetProperty(ref _demoNotice, value))
            {
                OnPropertyChanged(nameof(HasDemoNotice));
            }
        }
    }

    public bool HasDemoNotice => !string.IsNullOrEmpty(_demoNotice);

    public string AssignmentTitle
    {
        get => _assignmentTitle;
        private set => SetProperty(ref _assignmentTitle, value);
    }

    public string SummaryLine
    {
        get => _summaryLine;
        private set => SetProperty(ref _summaryLine, value);
    }

    /// <summary>The counts and totals shown above the list.</summary>
    public string CountsLine
    {
        get => _countsLine;
        private set => SetProperty(ref _countsLine, value);
    }

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

    public string Filter
    {
        get => _filter;
        set
        {
            if (SetProperty(ref _filter, value))
            {
                ApplyFilter();
            }
        }
    }

    public MarkSchemeRow? Selected
    {
        get => _selected;
        set
        {
            if (!SetProperty(ref _selected, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasSelection));
            OnPropertyChanged(nameof(SelectedTitle));
            OnPropertyChanged(nameof(SelectedWhere));
            OnPropertyChanged(nameof(SelectedAnswer));
            OnPropertyChanged(nameof(SelectedAnswerIsPlaceholder));
            OnPropertyChanged(nameof(SelectedCriteria));
            OnPropertyChanged(nameof(SelectedStatus));
            ApplySelection();
            _ = LoadHistoryAsync();
        }
    }

    public bool HasSelection => _selected is not null;

    public string SelectedTitle => _selected is null
        ? "Choose a question"
        : $"{_selected.KindText} {_selected.Label}";

    public string SelectedWhere => _selected is null
        ? string.Empty
        : string.Join("  ·  ", new[]
        {
            _selected.MarksText,
            _selected.WhereText,
            _selected.StatusText,
        }.Where(part => !string.IsNullOrWhiteSpace(part)));

    /// <summary>The answers the scheme actually gave, one per line.</summary>
    public string SelectedAnswer => _selected is null
        ? string.Empty
        : _selected.Answers.Count > 0
            ? string.Join("\n", _selected.Answers)
            : _selected.ExtractionStatus == "MISSING"
                ? "The scheme has no answer and no marking guidance for this entry."
                : "The scheme gives marking guidance but no answer.";

    /// <summary>The selected answer is still Demo Mode's placeholder, so the
    /// detail panel can show it as such instead of as the scheme's wording.</summary>
    public bool SelectedAnswerIsPlaceholder => _selected?.AnswerIsPlaceholder == true;

    public string SelectedCriteria => _selected?.Criteria ?? string.Empty;

    public string SelectedStatus => _selected is null
        ? string.Empty
        : _selected.Decided
            ? $"Handled as: {DecisionLabel(_selected.Decision)}  ·  set by you"
            : $"Proposed: {DecisionLabel(_selected.Decision)}  ·  not decided yet";

    public string AnswerDraft
    {
        get => _answerDraft;
        set => SetProperty(ref _answerDraft, value);
    }

    public string NoteDraft
    {
        get => _noteDraft;
        set => SetProperty(ref _noteDraft, value);
    }

    public string FinalizeStatus
    {
        get => _finalizeStatus;
        private set => SetProperty(ref _finalizeStatus, value);
    }

    /// <summary>The assignment this page is showing, for the deep link.</summary>
    public string CourseworkId => _courseworkId;

    // ------------------------------------------------------------------ load

    protected override async Task LoadCoreAsync()
    {
        var pending = Session.TakePendingSchemeAssignment();
        if (!string.IsNullOrWhiteSpace(pending))
        {
            _courseworkId = pending;
        }

        if (string.IsNullOrWhiteSpace(_courseworkId))
        {
            Reset("Open an assignment and choose “Review mark scheme” to see its questions.");
            return;
        }

        try
        {
            var payload = await Session.Api.SchemeDecisionsAsync(_courseworkId)
                .ConfigureAwait(true);
            Apply(payload);
        }
        catch (ApiException ex) when (ex.Code == "scheme_not_read")
        {
            // A scheme that exists but has not been read is a next step, not a
            // failure: it says so in the page's own voice instead of raising a
            // red error strip over an assignment that is set up correctly, and
            // it offers the action that fixes it.
            Reset(ex.Display);
            NeedsRead = true;
            ReadHint = "Reading it now turns the attached PDF into questions "
                       + "you can review and decide about.";
            ReadSchemeCommand.RaiseCanExecuteChanged();
        }
    }

    private void Reset(string message)
    {
        _all.Clear();
        Questions.Clear();
        History.Clear();
        Selected = null;
        AssignmentTitle = string.Empty;
        SummaryLine = message;
        CountsLine = string.Empty;
        NeedsRead = false;
        ReadHint = string.Empty;
    }

    /// <summary>Asks the backend to read the attached scheme now.</summary>
    private async Task ReadSchemeAsync()
    {
        _lastReadSucceeded = false;
        var payload = await Session.Api.ReadMarkSchemeAsync(_courseworkId).ConfigureAwait(true);
        _lastReadSucceeded = payload.ValueKind == JsonValueKind.Object
                             && payload.TryGetProperty("read", out var read)
                             && read.ValueKind == JsonValueKind.True;
        if (_lastReadSucceeded)
        {
            return;
        }

        // Best-effort, like every reader of a scheme: say why, invent nothing.
        var reason = Text(payload, "reason") ?? "the mark scheme could not be read";
        SummaryLine = "The mark scheme is attached but could not be read: " + reason + ".";
    }

    /// <summary>Runs once the read has finished and the busy window has closed,
    /// so the reload is not swallowed by <see cref="PageViewModel.LoadAsync"/>.</summary>
    private async Task AfterReadAsync()
    {
        if (_lastReadSucceeded)
        {
            await LoadAsync().ConfigureAwait(true);
            Status = "Mark scheme read — its questions are ready to review.";
            return;
        }

        NeedsRead = true;
        ReadHint = "Nothing was read, so there is still nothing to review.";
        ReadSchemeCommand.RaiseCanExecuteChanged();
    }

    private void Apply(JsonElement payload)
    {
        _all.Clear();
        Questions.Clear();
        NeedsRead = false;
        ReadHint = string.Empty;
        DemoNotice = string.Empty;

        AssignmentTitle = Text(payload, "assignment_title") ?? _assignmentTitle;
        _all.AddRange(ReadQuestions(payload));

        var summary = payload.TryGetProperty("summary", out var s) ? s : default;
        _unresolved = Int(summary, "unresolved");
        _automatic = Int(summary, "automatic");
        _manual = Int(summary, "manual");
        _skipped = Int(summary, "skipped");
        _totalMarks = Int(summary, "total_marks");
        _skippedMarks = Int(summary, "skipped_marks");
        _markedMarks = Int(summary, "marked_marks");

        var questions = Int(summary, "questions");
        var parts = Int(summary, "subquestions");
        CountsLine =
            $"{questions} questions"
            + (parts > 0 ? $" with {parts} sub-questions" : string.Empty)
            + $"  ·  {_automatic} automatic  ·  {_manual} manual  ·  {_skipped} skipped"
            + (_unresolved > 0 ? $"  ·  {_unresolved} undecided" : "  ·  all decided")
            + $"  ·  {_markedMarks} of {_totalMarks} marks marked"
            + (_skippedMarks > 0 ? $" ({_skippedMarks} skipped)" : string.Empty);

        var orphans = payload.TryGetProperty("orphaned", out var o)
                      && o.ValueKind == JsonValueKind.Array ? o.GetArrayLength() : 0;
        // Demo Mode builds a labelled synthetic structure so the page and its
        // decisions can be explored without an API key. It must never pass for
        // a read scheme, so the page says which one it is looking at.
        //
        // A decision, answer or note response repeats the rows WITHOUT the
        // `scheme` block, so the label is only rewritten when that block is
        // present: otherwise saving a decision would erase the very notice that
        // says this structure is synthetic.
        if (payload.TryGetProperty("scheme", out var schemeInfo)
            && schemeInfo.ValueKind == JsonValueKind.Object)
        {
            _demo = schemeInfo.TryGetProperty("demo", out var d)
                    && d.ValueKind == JsonValueKind.True;
        }

        DemoNotice = _demo
            ? "Demo Mode: this structure is synthetic, not read from the attached PDF. "
              + "Decisions and answer edits work exactly as they will on a real scheme."
            : string.Empty;
        if (_demo)
        {
            SummaryLine = $"{_all.Count} demonstration entries — a stand-in for a read scheme, "
                          + "so every question can be reviewed without an API key.";
            OnPropertyChanged(nameof(HasQuestions));
            ApplyFilter();
            Selected = _all.FirstOrDefault();
            return;
        }

        SummaryLine = orphans > 0
            ? $"{_all.Count} entries. {orphans} saved decision(s) belong to questions that "
              + "are no longer in this scheme — the scheme was re-read, so review them again."
            : $"{_all.Count} entries read from the attached mark scheme.";

        OnPropertyChanged(nameof(HasQuestions));
        ApplyFilter();
        Selected = _all.FirstOrDefault();
    }

    public bool HasQuestions => _all.Count > 0;

    private IEnumerable<MarkSchemeRow> ReadQuestions(JsonElement payload)
    {
        if (!payload.TryGetProperty("questions", out var questions)
            || questions.ValueKind != JsonValueKind.Array)
        {
            yield break;
        }

        foreach (var item in questions.EnumerateArray())
        {
            var label = Text(item, "question_label") ?? string.Empty;
            var key = Text(item, "question_key") ?? string.Empty;
            if (string.IsNullOrWhiteSpace(label) || string.IsNullOrWhiteSpace(key))
            {
                continue;
            }

            var answers = new List<string>();
            if (item.TryGetProperty("answers", out var a) && a.ValueKind == JsonValueKind.Array)
            {
                answers.AddRange(a.EnumerateArray()
                    .Select(v => v.GetString() ?? string.Empty)
                    .Where(v => !string.IsNullOrWhiteSpace(v)));
            }

            var row = new MarkSchemeRow
            {
                QuestionKey = key,
                Label = label,
                Depth = Int(item, "depth"),
                MaxMarks = Int(item, "max_marks"),
                Page = item.TryGetProperty("page", out var p) && p.ValueKind == JsonValueKind.Number
                    ? p.GetInt32() : null,
                Reliable = item.TryGetProperty("reliable", out var r)
                           && r.ValueKind == JsonValueKind.True,
                ExtractionStatus = Text(item, "extraction_status") ?? string.Empty,
                Criteria = Text(item, "criteria") ?? string.Empty,
                Answers = answers,
                Decided = item.TryGetProperty("decided", out var d)
                          && d.ValueKind == JsonValueKind.True,
                Skipped = item.TryGetProperty("skipped", out var sk)
                          && sk.ValueKind == JsonValueKind.True,
                Note = Text(item, "note") ?? string.Empty,
                Quiet = true,
                Changed = OnRowDecisionChanged,
            };
            // An edited answer arrives as answer_text; only present when edited.
            // The setter turns an empty value back into "not edited".
            row.AnswerText = Text(item, "answer_text") ?? string.Empty;
            row.Decision = Text(item, "decision") ?? "AUTO";
            row.Quiet = false;
            yield return row;
        }
    }

    private void ApplyFilter()
    {
        var search = (_search ?? string.Empty).Trim();
        Questions.Clear();
        foreach (var row in _all)
        {
            if (!MatchesFilter(row) || !MatchesSearch(row, search))
            {
                continue;
            }

            Questions.Add(row);
        }

        OnPropertyChanged(nameof(FilteredCount));
    }

    public string FilteredCount =>
        Questions.Count == _all.Count
            ? $"{_all.Count} shown"
            : $"{Questions.Count} of {_all.Count} shown";

    private bool MatchesSearch(MarkSchemeRow row, string search)
    {
        if (string.IsNullOrWhiteSpace(search))
        {
            return true;
        }

        // Match the identifier as printed ("1(b)") and as a teacher might type
        // it ("1b", "q1b"): the label is normalised the same way the backend
        // normalises a question id, so "1b" finds "1(b)".
        var label = Normalise(row.Label);
        var term = Normalise(search);
        return label.Contains(term, StringComparison.OrdinalIgnoreCase)
               || row.AnswerSummary.Contains(search, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>A question id reduced to what makes two spellings the same:
    /// no spaces, no brackets, no leading Q.</summary>
    private static string Normalise(string text) =>
        new string((text ?? string.Empty)
                .Where(c => !char.IsWhiteSpace(c) && c != '(' && c != ')')
                .ToArray())
            .TrimStart('Q', 'q');

    private bool MatchesFilter(MarkSchemeRow row) => _filter switch
    {
        FilterAutomatic => !row.Skipped && row.Decision == "AUTO",
        FilterManual => !row.Skipped && row.Decision == "MANUAL",
        FilterSkipped => row.Skipped,
        FilterUnresolved => !row.Decided,
        FilterUncertain => !row.Reliable,
        _ => true,
    };

    // ------------------------------------------------------------- decisions

    /// <summary>One code path for a decision, whichever control made it.</summary>
    private async void OnRowDecisionChanged(MarkSchemeRow row)
    {
        await RunAsync(async () =>
        {
            var payload = await Session.Api.SetSchemeDecisionAsync(
                _courseworkId, row.Label, row.Decision, row.Note).ConfigureAwait(true);
            AfterDecision(payload, redirect: row);
        }, $"Question {row.Label} set to {DecisionLabel(row.Decision)}.").ConfigureAwait(true);
    }

    /// <summary>Set the selected question's decision from a detail-panel button.
    ///
    /// This is the ACTION the command wraps: it must not call RunAsync itself,
    /// or the page would already be busy and the wrapped call would return
    /// immediately, saving nothing.
    /// </summary>
    private async Task SelectDecisionAsync(object? parameter)
    {
        if (Selected is null || parameter is not string code)
        {
            return;
        }

        var label = Selected.Label;
        var payload = await Session.Api.SetSchemeDecisionAsync(
            _courseworkId, label, code, Selected.Note).ConfigureAwait(true);
        AfterDecision(payload, redirect: Selected);
        Status = $"Question {label} set to {DecisionLabel(code)}.";
    }

    /// <summary>
    /// Re-reads the list from the response and puts the selection back on the
    /// same QUESTION. Reloading replaces the row objects, so holding the old
    /// instance would silently select nothing after every decision.
    /// </summary>
    private void AfterDecision(JsonElement payload, MarkSchemeRow? redirect)
    {
        var key = redirect?.QuestionKey;
        Apply(payload);
        if (!string.IsNullOrWhiteSpace(key))
        {
            Selected = _all.FirstOrDefault(r => r.QuestionKey == key) ?? Selected;
        }

        // Everything already decided stays in the list the filter allows.
        OnPropertyChanged(nameof(SelectedStatus));
    }

    // Each of these is the ACTION a Command wraps. None of them may call
    // RunAsync: the wrapper already set the page busy, so an inner RunAsync
    // returns immediately and the save silently never happens.
    private async Task SaveAnswerAsync()
    {
        if (Selected is null)
        {
            return;
        }

        var label = Selected.Label;
        var payload = await Session.Api.SetSchemeAnswerAsync(
            _courseworkId, label, AnswerDraft).ConfigureAwait(true);
        Apply(payload);
        Selected = _all.FirstOrDefault(r => r.Label == label) ?? Selected;
        Status = $"Answer for question {label} saved.";
    }

    private async Task RestoreAnswerAsync()
    {
        if (Selected is null)
        {
            return;
        }

        var label = Selected.Label;
        var payload = await Session.Api.SetSchemeAnswerAsync(
            _courseworkId, label, null).ConfigureAwait(true);
        Apply(payload);
        Selected = _all.FirstOrDefault(r => r.Label == label) ?? Selected;
        Status = $"Question {label} now uses the answer from the scheme again.";
    }

    private async Task SaveNoteAsync()
    {
        if (Selected is null)
        {
            return;
        }

        var label = Selected.Label;
        var payload = await Session.Api.SetSchemeDecisionAsync(
            _courseworkId, label, Selected.Decision, NoteDraft).ConfigureAwait(true);
        Apply(payload);
        Selected = _all.FirstOrDefault(r => r.Label == label) ?? Selected;
        Status = $"Note for question {label} saved.";
    }

    private async Task FinalizeAsync()
    {
        var acknowledge = false;
        if (_unresolved > 0)
        {
            // Undecided questions are never approved by silence.
            acknowledge = Session.Dialogs.Confirm(
                "Some questions have no decision",
                $"{_unresolved} question(s) still have no handling decision. "
                + "They keep the proposed default. Finalise the review anyway?");
            if (!acknowledge)
            {
                return;
            }
        }

        var payload = await Session.Api.FinalizeSchemeReviewAsync(
            _courseworkId, acknowledge).ConfigureAwait(true);
        FinalizeStatus = $"Review finalised: {Int(payload, "automatic")} automatic, "
                          + $"{Int(payload, "manual")} manual, {Int(payload, "skipped")} skipped"
                          + (acknowledge
                              ? $", {Int(payload, "acknowledged_unresolved")} acknowledged undecided."
                              : ".");
        Status = "Mark scheme review finalised.";
    }

    // --------------------------------------------------------------- history

    private async Task LoadHistoryAsync()
    {
        History.Clear();
        if (string.IsNullOrWhiteSpace(_courseworkId))
        {
            return;
        }

        try
        {
            var payload = await Session.Api.SchemeDecisionHistoryAsync(
                _courseworkId, Selected?.Label).ConfigureAwait(true);
            if (!payload.TryGetProperty("items", out var items)
                || items.ValueKind != JsonValueKind.Array)
            {
                return;
            }

            foreach (var item in items.EnumerateArray())
            {
                History.Add(new DecisionHistoryRow
                {
                    Where = Text(item, "question_label") ?? string.Empty,
                    When = FormatTime(item),
                    What = DescribeChange(item),
                    Reason = Text(item, "reason") ?? string.Empty,
                });
            }
        }
        catch (Exception ex) when (ex is ApiException or HttpRequestException)
        {
            // A history failure must not hide the decisions themselves; the
            // page says nothing rather than claiming there were no changes.
            Session.Log.Warn("mark scheme history unavailable: " + ex.Message);
        }
    }

    private static string DescribeChange(JsonElement item)
    {
        var action = Text(item, "action") ?? string.Empty;
        var previous = Text(item, "previous_decision");
        var next = Text(item, "new_decision");
        if (action == "ANSWER")
        {
            var before = Text(item, "previous_answer");
            var after = Text(item, "new_answer");
            return after is null
                ? "Answer restored to the scheme's own wording"
                : $"Answer changed from “{before ?? "the extracted answer"}” to “{after}”";
        }

        if (action == "FINALIZED")
        {
            return "Review finalised";
        }

        if (action == "NOTE")
        {
            return "Note changed";
        }

        return previous is null
            ? $"Set to {DecisionLabel(next ?? string.Empty)}"
            : $"Changed from {DecisionLabel(previous)} to {DecisionLabel(next ?? string.Empty)}";
    }

    private static string FormatTime(JsonElement item)
    {
        if (!item.TryGetProperty("at", out var at) || at.ValueKind != JsonValueKind.Number)
        {
            return string.Empty;
        }

        try
        {
            return DateTimeOffset.FromUnixTimeSeconds((long)at.GetDouble())
                .ToLocalTime().ToString("d MMM HH:mm");
        }
        catch (ArgumentOutOfRangeException)
        {
            return string.Empty;
        }
    }

    // ---------------------------------------------------------------- helpers

    private void ApplySelection()
    {
        if (_selected is null)
        {
            AnswerDraft = string.Empty;
            NoteDraft = string.Empty;
            return;
        }

        AnswerDraft = _selected.AnswerText;
        NoteDraft = _selected.Note;
    }

    private static string DecisionLabel(string code) => code switch
    {
        "AUTO" => "mark automatically",
        "MANUAL" => "manual independent check",
        "SKIP" => "skip",
        _ => code,
    };

    private static string? Text(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object
        && element.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;

    private static int Int(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object
        && element.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.Number
            ? value.GetInt32()
            : 0;
}
