using System.Collections.ObjectModel;
using System.Text.Json;
using System.Windows.Media;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Core.Workspace;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One review row, described the way a teacher reads it. The backend
/// id stays on the row because every action needs it, but it is never the
/// column a person has to read: question, assignment and student say which
/// case this is.</summary>
public sealed class SchemeRow
{
    public string Id { get; init; } = string.Empty;
    public string Question { get; init; } = string.Empty;
    public string Assignment { get; init; } = string.Empty;
    public string Student { get; init; } = string.Empty;
    public string Status { get; init; } = string.Empty;
    public string Detail { get; init; } = string.Empty;

    /// <summary>One line that names the case in a dialog, without a uuid.</summary>
    public string Mention => string.IsNullOrWhiteSpace(Question)
        ? Assignment
        : $"question {Question}" + (string.IsNullOrWhiteSpace(Assignment) ? string.Empty : $" of {Assignment}");

    public override string ToString() => $"{Mention} ({Status})";
}

/// <summary>One question of an assignment's mark scheme, as the backend
/// extracted it, with whether it is currently being marked.</summary>
public sealed class SchemeQuestionRow
{
    public string CourseworkId { get; init; } = string.Empty;

    /// <summary>The identifier to send back: "12", "4(a)", "Q12" — exactly what
    /// the blueprint called it.</summary>
    public string Number { get; init; } = string.Empty;

    public string Title { get; init; } = string.Empty;
    public string KindText { get; init; } = string.Empty;
    public string MarksText { get; init; } = string.Empty;
    public string WhereText { get; init; } = string.Empty;
    public string Criteria { get; init; } = string.Empty;
    public bool IsIgnored { get; init; }

    /// <summary>"Skipped" or "Marked" — the state, in one word.</summary>
    public string StateText => IsIgnored ? "Skipped" : "Marked";

    /// <summary>What the button does, in the teacher's words.</summary>
    public string ActionText => IsIgnored ? "Mark it again" : "Skip it";

    public string DetailText => string.IsNullOrWhiteSpace(Criteria)
        ? KindText
        : $"{KindText} · {Criteria}";

    public override string ToString() => $"{Title} ({StateText})";
}

public sealed class SchemeReviewViewModel : PageViewModel
{
    /// <summary>The review status that means "still needs a decision". A row stops
    /// carrying it only once someone has decided it (asking for further
    /// investigation leaves it open), which is why this is the set the grid lists
    /// and the number its chip prints.</summary>
    private const string OpenReviewStatus = "AWAITING_REVIEW";

    /// <summary>The decisions the backend accepts, each with the wording a
    /// teacher is shown. The code travels to the API; the label never does.</summary>
    private static readonly (string Code, string Label)[] _decisions =
    {
        ("CONFIRM_SCHEME_CORRECT", "The mark scheme answer is correct"),
        ("CONFIRM_SCHEME_INCORRECT", "The mark scheme answer is wrong"),
        ("ACCEPT_ALTERNATIVE_ANSWER", "Accept the student's alternative answer"),
        ("AWARD_PARTIAL_CREDIT", "Award partial credit"),
        ("REJECT_BULK_CORRECTION", "Reject the bulk correction"),
        ("REQUEST_FURTHER_INVESTIGATION", "Ask for further investigation"),
    };

    private readonly IPageImageDecoder _decoder;

    private SchemeRow? _selectedDiscrepancy;
    private SchemeRow? _selectedProposal;
    private string _detailNote = "Select a discrepancy to see its record.";
    private string _detailRaw = string.Empty;
    private string _summary = string.Empty;

    private string _findingTitle = "Nothing selected";
    private string _findingNote = "Select a discrepancy above to see what the mark scheme says and where.";
    private string _schemeSays = string.Empty;
    private string _studentSays = string.Empty;
    private string _independentCheck = string.Empty;
    private string _schemeWhere = string.Empty;
    private string _schemeImageNote = string.Empty;
    private string _schemeImageCaption = string.Empty;
    private ImageSource? _schemeImage;
    private string _findingCourseworkId = string.Empty;
    private string _findingQuestion = string.Empty;
    private bool _selectedIgnored;
    private string? _detailAssignment;
    private int? _detailPage;
    private string _questionsTitle = "Questions in this mark scheme";
    private string _questionsNote = "Select a discrepancy from an assignment to see the questions it marks.";
    private int _detailRequest;

    public SchemeReviewViewModel(AppSession session, IPageImageDecoder? decoder = null)
        : base(session, "scheme", "Mark scheme review")
    {
        _decoder = decoder ?? new WpfPageImageDecoder();
        Group = "Review";
    }

    public ObservableCollection<SchemeRow> Discrepancies { get; } = new();
    public ObservableCollection<SchemeRow> Proposals { get; } = new();

    /// <summary>Every question of the selected discrepancy's assignment, so a
    /// teacher can take any of them out of marking — not only the one that
    /// raised this discrepancy.</summary>
    public ObservableCollection<SchemeQuestionRow> Questions { get; } = new();

    public SchemeRow? SelectedDiscrepancy
    {
        get => _selectedDiscrepancy;
        set
        {
            if (!SetProperty(ref _selectedDiscrepancy, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasDiscrepancy));
            if (value is not null)
            {
                _ = LoadDetailAsync(value.Id);
            }
        }
    }

    public SchemeRow? SelectedProposal
    {
        get => _selectedProposal;
        set
        {
            if (SetProperty(ref _selectedProposal, value))
            {
                OnPropertyChanged(nameof(HasProposal));
            }
        }
    }

    /// <summary>A discrepancy drives staging and the decision record; a proposal
    /// drives approve and reject. Both commands return early without one.</summary>
    public bool HasDiscrepancy => _selectedDiscrepancy is not null;

    public bool HasProposal => _selectedProposal is not null;

    // ------------------------------------------------------- the finding card

    /// <summary>What kind of mark-scheme problem this is, in one line.</summary>
    public string FindingTitle
    {
        get => _findingTitle;
        private set => SetProperty(ref _findingTitle, value);
    }

    /// <summary>Why it was flagged — the engine's own reason, verbatim.</summary>
    public string FindingNote
    {
        get => _findingNote;
        private set => SetProperty(ref _findingNote, value);
    }

    /// <summary>What the uploaded mark scheme says for this question.</summary>
    public string SchemeSays
    {
        get => _schemeSays;
        private set
        {
            if (SetProperty(ref _schemeSays, value))
            {
                OnPropertyChanged(nameof(HasSchemeSays));
            }
        }
    }

    public bool HasSchemeSays => !string.IsNullOrWhiteSpace(SchemeSays);

    /// <summary>What the student wrote, read from the stored result.</summary>
    public string StudentSays
    {
        get => _studentSays;
        private set
        {
            if (SetProperty(ref _studentSays, value))
            {
                OnPropertyChanged(nameof(HasStudentSays));
            }
        }
    }

    public bool HasStudentSays => !string.IsNullOrWhiteSpace(StudentSays);

    /// <summary>The engine's independent answer and the reason it disagrees.</summary>
    public string IndependentCheck
    {
        get => _independentCheck;
        private set
        {
            if (SetProperty(ref _independentCheck, value))
            {
                OnPropertyChanged(nameof(HasIndependentCheck));
            }
        }
    }

    public bool HasIndependentCheck => !string.IsNullOrWhiteSpace(IndependentCheck);

    /// <summary>Where in the actual mark scheme the question is: page and any
    /// printed label the scheme used.</summary>
    public string SchemeWhere
    {
        get => _schemeWhere;
        private set
        {
            if (SetProperty(ref _schemeWhere, value))
            {
                OnPropertyChanged(nameof(HasSchemeWhere));
            }
        }
    }

    public bool HasSchemeWhere => !string.IsNullOrWhiteSpace(SchemeWhere);

    public string SchemeImageCaption
    {
        get => _schemeImageCaption;
        private set => SetProperty(ref _schemeImageCaption, value);
    }

    /// <summary>Why there is no page image (no page known, no PDF, a read that
    /// failed). Never a blank panel with no explanation.</summary>
    public string SchemeImageNote
    {
        get => _schemeImageNote;
        private set
        {
            if (SetProperty(ref _schemeImageNote, value))
            {
                OnPropertyChanged(nameof(HasSchemeImageNote));
            }
        }
    }

    public bool HasSchemeImageNote => !string.IsNullOrWhiteSpace(SchemeImageNote);

    /// <summary>The real uploaded page of the mark scheme, rendered by the
    /// backend. Never a reconstruction of it.</summary>
    public ImageSource? SchemeImage
    {
        get => _schemeImage;
        private set
        {
            if (SetProperty(ref _schemeImage, value))
            {
                OnPropertyChanged(nameof(HasSchemeImage));
            }
        }
    }

    public bool HasSchemeImage => SchemeImage is not null;

    /// <summary>True when the selected discrepancy's question is already out of
    /// marking, so the button offers to put it back.</summary>
    public bool SelectedQuestionIgnored
    {
        get => _selectedIgnored;
        private set
        {
            if (SetProperty(ref _selectedIgnored, value))
            {
                OnPropertyChanged(nameof(IgnoreSelectedText));
            }
        }
    }

    public string IgnoreSelectedText => SelectedQuestionIgnored
        ? $"Mark {_findingQuestion} again"
        : $"Skip {_findingQuestion} from marking";

    /// <summary>Only offered when the assignment and question are both known.</summary>
    public bool CanIgnoreSelected =>
        !string.IsNullOrWhiteSpace(_findingCourseworkId) && !string.IsNullOrWhiteSpace(_findingQuestion);

    public string QuestionsTitle
    {
        get => _questionsTitle;
        private set => SetProperty(ref _questionsTitle, value);
    }

    public string QuestionsNote
    {
        get => _questionsNote;
        private set => SetProperty(ref _questionsNote, value);
    }

    public bool HasQuestions => Questions.Count > 0;

    /// <summary>The discrepancy record read as a sentence and labelled facts;
    /// the raw payload stays inside technical details.</summary>
    public ObservableCollection<FactRow> DetailFacts { get; } = new();

    public string DetailNote
    {
        get => _detailNote;
        private set
        {
            if (SetProperty(ref _detailNote, value))
            {
                OnPropertyChanged(nameof(HasDetailNote));
            }
        }
    }

    public string DetailRaw
    {
        get => _detailRaw;
        private set
        {
            if (SetProperty(ref _detailRaw, value))
            {
                OnPropertyChanged(nameof(HasDetailRaw));
            }
        }
    }

    public bool HasDetailNote => !string.IsNullOrWhiteSpace(DetailNote);

    public bool HasDetailFacts => DetailFacts.Count > 0;

    public bool HasDetailRaw => !string.IsNullOrWhiteSpace(DetailRaw);

    private void ShowPayload(JsonElement payload, string fallbackNote)
    {
        DetailFacts.Clear();
        foreach (var fact in PayloadFacts.Of(payload))
        {
            DetailFacts.Add(fact);
        }

        DetailRaw = PayloadFacts.Raw(payload);
        var summary = PayloadFacts.Summarize(payload);
        DetailNote = string.IsNullOrWhiteSpace(summary) ? fallbackNote : summary;
        OnPropertyChanged(nameof(HasDetailFacts));
    }

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    /// <summary>Skips (or restores) one question of the assignment currently
    /// shown. Existing results keep their marks; later corrections skip it.</summary>
    public AsyncCommand ToggleQuestionCommand => Command(async parameter =>
    {
        if (parameter is not SchemeQuestionRow row
            || string.IsNullOrWhiteSpace(row.CourseworkId)
            || string.IsNullOrWhiteSpace(row.Number))
        {
            return;
        }

        await SetIgnoredAsync(row.CourseworkId, row.Number, !row.IsIgnored);
    });

    /// <summary>Skip (or restore) the question this discrepancy is about, from
    /// the finding card, without having to find it in the list.
    ///
    /// The command deliberately has no CanExecute gate: WPF evaluates that once
    /// when the button is bound, and the assignment is only known after the
    /// detail has loaded — a gate would leave the button dead forever. The body
    /// checks the same condition, and IsEnabled is bound to it.</summary>
    public AsyncCommand IgnoreSelectedCommand => Command(async () =>
    {
        if (!CanIgnoreSelected)
        {
            return;
        }

        await SetIgnoredAsync(_findingCourseworkId, _findingQuestion, !SelectedQuestionIgnored);
    });

    public AsyncCommand ProposeCommand => Command(async () =>
    {
        if (SelectedDiscrepancy is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Stage a correction",
                "Build a staged correction for this discrepancy? This changes nothing until a proposal "
                + "is explicitly approved."))
        {
            return;
        }

        var payload = await Session.Api.ProposeDiscrepancyAsync(SelectedDiscrepancy.Id);
        ShowPayload(payload, "A staged proposal was created. Nothing changes until it is approved.");
        Status = "Proposal staged. Approving it is a separate, explicit step.";
    }, after: LoadAsync);

    public AsyncCommand DecideCommand => Command(async () =>
    {
        if (SelectedDiscrepancy is null)
        {
            return;
        }

        // The dialog lists what each decision means and accepts either its
        // number or its wording, so a teacher never has to type an enum value.
        var menu = string.Join("\n", _decisions.Select((d, index) => $"{index + 1}. {d.Label}"));
        var answer = Session.Dialogs.AskText("Record a decision",
            $"What is the outcome for {SelectedDiscrepancy.Mention}?\n\n{menu}\n\nType a number or the wording of one decision:",
            "1");
        if (answer is null)
        {
            return;
        }

        var chosen = ResolveDecision(answer);
        if (chosen is null)
        {
            Error = "That is not one of the listed decisions. Pick a number from 1 to "
                    + _decisions.Length + ".";
            return;
        }

        var payload = await Session.Api.DecideDiscrepancyAsync(SelectedDiscrepancy.Id, chosen.Value.Code);
        ShowPayload(payload, $"Decision recorded: {chosen.Value.Label.ToLowerInvariant()}.");

        // Whether the row is still open is the backend's answer, not this
        // screen's opinion: asking for further investigation leaves it awaiting
        // review, so the message has to say which of the two happened.
        Status = string.Equals(NestedText(payload, "discrepancy", "review_status"),
            OpenReviewStatus, StringComparison.OrdinalIgnoreCase)
            ? $"Decision recorded: {chosen.Value.Label.ToLowerInvariant()}. It stays in the open list until"
              + " an outcome is recorded."
            : $"Decision recorded: {chosen.Value.Label.ToLowerInvariant()}. This discrepancy has left the"
              + " open list.";

        // The list, the summary and the finding card are reloaded once this
        // command's busy window closes: see the note on RunAsync's `after`.
    }, after: LoadAsync);

    public AsyncCommand ApproveProposalCommand => Command(async () =>
    {
        if (SelectedProposal is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Approve bulk correction",
                $"Apply the staged correction for {SelectedProposal.Mention}? "
                + "This can change previously stored marks. The backend requires an explicit "
                + "confirmation and records the approval."))
        {
            return;
        }

        ShowPayload(await Session.Api.DecideProposalAsync(SelectedProposal.Id, "approve"),
            "Proposal approved; the staged corrections are applied.");
        Status = "Proposal approved.";
    }, after: LoadAsync);

    public AsyncCommand RejectProposalCommand => Command(async () =>
    {
        if (SelectedProposal is null)
        {
            return;
        }

        ShowPayload(await Session.Api.DecideProposalAsync(SelectedProposal.Id, "reject"),
            "Proposal rejected; no marks changed.");
        Status = "Proposal rejected.";
    }, after: LoadAsync);

    protected override async Task LoadCoreAsync()
    {
        var summary = await Session.Api.SchemeSummaryAsync();
        // OPEN discrepancies only. The unfiltered endpoint is the whole audit
        // table, so asking for it left rows this list counts as "open" — and
        // kept showing them after a decision had closed them.
        var discrepancies = await Session.Api.DiscrepanciesAsync(OpenReviewStatus);
        var proposals = await Session.Api.ProposalsAsync();

        var previousDiscrepancy = SelectedDiscrepancy?.Id;
        var previousProposal = SelectedProposal?.Id;

        Discrepancies.Clear();
        foreach (var row in ReadItems(discrepancies))
        {
            Discrepancies.Add(row);
        }

        Proposals.Clear();
        foreach (var row in ReadItems(proposals))
        {
            Proposals.Add(row);
        }

        SelectedDiscrepancy = Discrepancies.FirstOrDefault(r => r.Id == previousDiscrepancy)
                              ?? Discrepancies.FirstOrDefault();
        SelectedProposal = Proposals.FirstOrDefault(r => r.Id == previousProposal) ?? Proposals.FirstOrDefault();

        if (Discrepancies.Count == 0)
        {
            // Nothing is open, so nothing may keep describing the row that was
            // just decided. Leaving the last finding on screen is the same stale
            // state in a different place.
            ClearFinding("Nothing is awaiting review",
                "Every discrepancy has been decided. A decided record stays in the backend's audit "
                + "history and no longer needs an answer here.");
        }

        var open = Number(summary, "open_discrepancies");
        var awaiting = Number(summary, "awaiting_approval");
        Summary = $"{Plural(open, "discrepancy", "discrepancies")} awaiting review · "
                  + $"{Plural(awaiting, "proposal", "proposals")} awaiting approval";
    }

    /// <summary>Accepts "3", "3." or the wording of a listed decision.</summary>
    private static (string Code, string Label)? ResolveDecision(string answer)
    {
        var text = answer.Trim();
        if (int.TryParse(text.TrimEnd('.', ')'), out var index)
            && index >= 1 && index <= _decisions.Length)
        {
            return _decisions[index - 1];
        }

        var match = _decisions.FirstOrDefault(d =>
            string.Equals(d.Label, text, StringComparison.OrdinalIgnoreCase)
            || string.Equals(d.Code, text, StringComparison.OrdinalIgnoreCase));
        return match == default ? null : match;
    }

    private static int Number(JsonElement payload, string property) =>
        payload.TryGetProperty(property, out var value) && value.ValueKind == JsonValueKind.Number
            ? value.GetInt32()
            : 0;

    private static string Plural(int count, string one, string many) =>
        $"{count} {(count == 1 ? one : many)}";

    private async Task LoadDetailAsync(string id)
    {
        var request = ++_detailRequest;
        try
        {
            var payload = await Session.Api.DiscrepancyAsync(id);
            if (request != _detailRequest)
            {
                return;
            }

            ApplyFinding(payload);
            ShowPayload(payload, "This discrepancy's record is shown below.");
            await LoadImageAsync(request);
            await LoadQuestionsAsync(_findingCourseworkId, request);
        }
        catch (ApiException ex)
        {
            if (request != _detailRequest)
            {
                return;
            }

            DetailFacts.Clear();
            OnPropertyChanged(nameof(HasDetailFacts));
            DetailNote = ex.Display;
            DetailRaw = ex.Technical;
            ClearFinding(ex.Display);
        }
        catch (Exception ex)
        {
            // A payload this page cannot read is reported, never a silently
            // half-filled card: the teacher has to know the record is missing.
            if (request != _detailRequest)
            {
                return;
            }

            Session.Log.Error("scheme review detail failed to render", ex);
            DetailFacts.Clear();
            OnPropertyChanged(nameof(HasDetailFacts));
            DetailNote = UserMessage.For(ex);
            ClearFinding(UserMessage.For(ex));
        }
    }

    private void ClearFinding(string note) => ClearFinding("This discrepancy could not be read", note);

    private void ClearFinding(string title, string note)
    {
        FindingTitle = title;
        FindingNote = note;
        SchemeSays = string.Empty;
        StudentSays = string.Empty;
        IndependentCheck = string.Empty;
        SchemeWhere = string.Empty;
        SchemeImage = null;
        SchemeImageCaption = string.Empty;
        SchemeImageNote = string.Empty;
        _findingCourseworkId = string.Empty;
        _findingQuestion = string.Empty;
        _detailAssignment = null;
        _detailPage = null;
        SelectedQuestionIgnored = false;
        Questions.Clear();
        QuestionsTitle = "Questions in this mark scheme";
        QuestionsNote = "Nothing to show until a discrepancy can be read.";
        RaiseQuestions();
    }

    /// <summary>Turns the detail payload into the finding card. Every value is
    /// read from the backend: a missing answer says so instead of filling in.</summary>
    private void ApplyFinding(JsonElement payload)
    {
        var row = payload.TryGetProperty("discrepancy", out var d) ? d : default;
        var scheme = payload.TryGetProperty("scheme", out var s) ? s : default;
        var answer = payload.TryGetProperty("answer", out var a) ? a : default;

        var question = Text(row, "question_number") ?? string.Empty;
        _findingQuestion = question;
        _findingCourseworkId = Text(row, "coursework_id") ?? string.Empty;
        var assignment = Text(row, "assignment_title");
        var maxMarks = NumberOrNull(row, "max_marks");
        var awarded = NumberOrNull(row, "awarded_marks");

        FindingTitle = BuildTitle(question, row, scheme);
        FindingNote = FirstNonEmpty(
            Text(row, "verified_reasoning"),
            Text(row, "suspicion"),
            NestedText(row, "evidence", "reasoning"),
            "The backend recorded this question as a mark-scheme disagreement.");

        var schemeAnswer = Text(row, "scheme_answer");
        var criteria = Text(scheme, "criteria");
        var printed = Text(scheme, "printed_label");
        var label = !string.IsNullOrWhiteSpace(printed) ? $" (printed as {printed})" : string.Empty;
        SchemeSays = string.IsNullOrWhiteSpace(schemeAnswer)
            ? $"Question {question}: the scheme gives criteria but no separate answer{label}."
              + (string.IsNullOrWhiteSpace(criteria) ? "" : $"\n{criteria}")
            : $"Question {question}{label}: {schemeAnswer}"
              + (string.IsNullOrWhiteSpace(criteria) ? "" : $"\nCriteria: {criteria}");

        var studentAnswer = Text(answer, "student_answer");
        StudentSays = string.IsNullOrWhiteSpace(studentAnswer)
            ? "No student answer was stored for this question."
            : studentAnswer!;

        var verified = Text(row, "verified_answer");
        var confidence = FractionOrNull(row, "confidence");
        IndependentCheck = string.IsNullOrWhiteSpace(verified)
            ? string.Empty
            : $"An independent solve gives: {verified}"
              + (confidence is null ? "" : $"  ·  confidence {confidence:0.##}");

        var page = NumberOrNull(scheme, "page");
        var marks = maxMarks is null
            ? string.Empty
            : awarded is null
                ? $"{maxMarks} marks available"
                : $"{awarded} of {maxMarks} marks awarded";
        SchemeWhere = page is null
            ? (string.IsNullOrWhiteSpace(marks) ? string.Empty : marks)
            : $"Mark-scheme page {page}" + (string.IsNullOrWhiteSpace(marks) ? "" : $"  ·  {marks}");

        _detailAssignment = assignment;
        _detailPage = page;
        SelectedQuestionIgnored = payload.TryGetProperty("ignored", out var ignored)
                                  && ignored.ValueKind == JsonValueKind.True;
        OnPropertyChanged(nameof(CanIgnoreSelected));
    }

    private static string BuildTitle(string question, JsonElement row, JsonElement scheme)
    {
        var kind = (Text(row, "state") ?? string.Empty) switch
        {
            "VALID_ALTERNATIVE" => "the student's answer is a valid alternative the scheme does not list",
            "UNCERTAIN" => "the question or the scheme's answer is ambiguous",
            "CONFIRMED_DISCREPANCY" => "a teacher confirmed the mark scheme is wrong here",
            "POSSIBLE_DISCREPANCY" => "the mark scheme's answer looks wrong",
            "HUMAN_REVIEWED" => "a teacher has already reviewed this question",
            _ => "the mark scheme disagrees with the mathematics",
        };
        var printed = Text(scheme, "printed_label");
        var suffix = string.IsNullOrWhiteSpace(printed) ? string.Empty : $" (scheme prints it as {printed})";
        return $"Q{question} — {kind}{suffix}";
    }

    /// <summary>The rendered page of the real mark scheme, when the backend can
    /// produce it. A failure is stated, never hidden behind an empty box.</summary>
    private async Task LoadImageAsync(int request)
    {
        SchemeImage = null;
        SchemeImageCaption = string.Empty;
        SchemeImageNote = string.Empty;

        if (string.IsNullOrWhiteSpace(_findingCourseworkId) || _detailPage is null)
        {
            SchemeImageNote = "The backend did not say which page of the mark scheme this question is on.";
            return;
        }

        try
        {
            var file = await Session.Api.SchemePageImageAsync(_findingCourseworkId, _detailPage.Value);
            var decoded = await _decoder.DecodeAsync(file.Bytes);
            if (request != _detailRequest)
            {
                return;
            }

            SchemeImage = decoded.Source;
            SchemeImageCaption = $"Page {_detailPage} of the uploaded mark scheme · {file.SizeText}";
            if (SchemeImage is null)
            {
                SchemeImageNote = "The page was downloaded but could not be displayed.";
            }
        }
        catch (ApiException ex)
        {
            if (request == _detailRequest)
            {
                SchemeImageNote = "The page of the mark scheme could not be opened: " + ex.Display;
            }
        }
        catch (Exception ex) when (ex is HttpRequestException or IOException or NotSupportedException)
        {
            if (request == _detailRequest)
            {
                SchemeImageNote = "The page of the mark scheme could not be opened: " + UserMessage.For(ex);
            }
        }
    }

    /// <summary>Every question the assignment's scheme declares, with its skip
    /// state and the running total so the effect on marks is visible.</summary>
    private async Task LoadQuestionsAsync(string courseworkId, int request)
    {
        Questions.Clear();
        if (string.IsNullOrWhiteSpace(courseworkId))
        {
            QuestionsNote = "This discrepancy is not linked to an assignment, so there is no "
                            + "scheme to list.";
            RaiseQuestions();
            return;
        }

        try
        {
            var payload = await Session.Api.SchemeQuestionsAsync(courseworkId);
            if (request != _detailRequest)
            {
                return;
            }

            foreach (var row in ReadQuestions(payload, courseworkId))
            {
                Questions.Add(row);
            }

            var total = NumberOrNull(payload, "total_marks");
            var marked = NumberOrNull(payload, "marked_total");
            var skipped = Questions.Count(q => q.IsIgnored);
            var questions = Questions.Count(q => q.KindText == "Question");
            var parts = Questions.Count - questions;
            QuestionsTitle = string.IsNullOrWhiteSpace(_detailAssignment)
                ? "Questions in this mark scheme"
                : $"Questions in “{_detailAssignment}”";
            QuestionsNote = Questions.Count == 0
                ? "This scheme has no per-question structure, so nothing can be skipped individually."
                : $"{questions} questions"
                  + (parts == 0 ? string.Empty : $" with {Plural(parts, "subquestion", "subquestions")}")
                  + $" · {Plural(skipped, "skipped question", "skipped questions")} · "
                  + $"marked total {(marked?.ToString() ?? "—")} of {(total?.ToString() ?? "—")} marks.";
        }
        catch (ApiException ex)
        {
            if (request == _detailRequest)
            {
                QuestionsNote = "The question list could not be read: " + ex.Display;
            }
        }

        RaiseQuestions();
    }

    private static IEnumerable<SchemeQuestionRow> ReadQuestions(JsonElement payload, string courseworkId)
    {
        if (!payload.TryGetProperty("questions", out var questions)
            || questions.ValueKind != JsonValueKind.Array)
        {
            yield break;
        }

        foreach (var question in questions.EnumerateArray())
        {
            var number = Text(question, "question_number") ?? string.Empty;
            if (string.IsNullOrWhiteSpace(number))
            {
                continue;
            }

            yield return new SchemeQuestionRow
            {
                CourseworkId = courseworkId,
                Number = number,
                Title = Label(number),
                KindText = "Question",
                MarksText = Marks(NumberOrNull(question, "max_marks")),
                WhereText = Page(NumberOrNull(question, "page")),
                Criteria = Text(question, "criteria") ?? string.Empty,
                IsIgnored = IsTrue(question, "ignored"),
            };

            if (!question.TryGetProperty("parts", out var parts)
                || parts.ValueKind != JsonValueKind.Array)
            {
                continue;
            }

            foreach (var part in parts.EnumerateArray())
            {
                var partNumber = Text(part, "number") ?? string.Empty;
                if (string.IsNullOrWhiteSpace(partNumber))
                {
                    continue;
                }

                yield return new SchemeQuestionRow
                {
                    CourseworkId = courseworkId,
                    Number = partNumber,
                    Title = Label(partNumber),
                    KindText = $"Subquestion of Q{number}",
                    MarksText = Marks(NumberOrNull(part, "max_marks")),
                    WhereText = string.Empty,
                    Criteria = string.Empty,
                    IsIgnored = IsTrue(part, "ignored"),
                };
            }
        }
    }

    private static string Label(string number) =>
        number.StartsWith('Q') || number.StartsWith('q') ? number.ToUpperInvariant() : $"Q{number}";

    private static string Marks(int? marks) => marks is null ? "—" : $"{marks} marks";

    private static string Page(int? page) => page is null ? string.Empty : $"page {page}";

    private async Task SetIgnoredAsync(string courseworkId, string question, bool ignore)
    {
        if (string.IsNullOrWhiteSpace(courseworkId) || string.IsNullOrWhiteSpace(question))
        {
            return;
        }

        var payload = ignore
            ? await Session.Api.IgnoreSchemeQuestionAsync(courseworkId, question)
            : await Session.Api.RestoreSchemeQuestionAsync(courseworkId, question);

        ShowPayload(payload, ignore
            ? $"Question {question} is no longer marked in this assignment."
            : $"Question {question} is being marked again.");

        // The finding card and the question list are refreshed through their own
        // loaders: LoadAsync's busy guard would skip a page reload from inside a
        // command that is already running.
        if (SelectedDiscrepancy is not null)
        {
            await LoadDetailAsync(SelectedDiscrepancy.Id);
        }

        Status = ignore
            ? $"Question {question} is skipped. Its marks are no longer in the total; "
              + "papers corrected from now on leave it out."
            : $"Question {question} is marked again, and its marks count towards the total.";
    }

    /// <summary>Announces a question-list change, including the derived flags
    /// the buttons and empty states read.</summary>
    private void RaiseQuestions()
    {
        OnPropertyChanged(nameof(HasQuestions));
        OnPropertyChanged(nameof(CanIgnoreSelected));
        OnPropertyChanged(nameof(IgnoreSelectedText));
    }

    private static bool IsTrue(JsonElement element, string property) =>
        element.TryGetProperty(property, out var value) && value.ValueKind == JsonValueKind.True;

    /// <summary>An integral field, or null when it is absent or not a whole
    /// number — 0.95 confidence must not crash a mark lookup.</summary>
    private static int? NumberOrNull(JsonElement element, string property) =>
        element.ValueKind == JsonValueKind.Object
        && element.TryGetProperty(property, out var value)
        && value.ValueKind == JsonValueKind.Number
        && value.TryGetInt32(out var number)
            ? number
            : null;

    private static double? FractionOrNull(JsonElement element, string property) =>
        element.ValueKind == JsonValueKind.Object
        && element.TryGetProperty(property, out var value)
        && value.ValueKind == JsonValueKind.Number
        && value.TryGetDouble(out var number)
            ? number
            : null;

    private static string? NestedText(JsonElement element, string property, string inner)
    {
        if (element.ValueKind != JsonValueKind.Object
            || !element.TryGetProperty(property, out var nested)
            || nested.ValueKind != JsonValueKind.Object)
        {
            return null;
        }

        return Text(nested, inner);
    }

    private static string FirstNonEmpty(params string?[] values) =>
        values.FirstOrDefault(v => !string.IsNullOrWhiteSpace(v)) ?? string.Empty;

    private static IEnumerable<SchemeRow> ReadItems(JsonElement payload)
    {
        if (!payload.TryGetProperty("items", out var items) || items.ValueKind != JsonValueKind.Array)
        {
            yield break;
        }

        foreach (var item in items.EnumerateArray())
        {
            var id = Text(item, "id") ?? string.Empty;
            var question = Text(item, "question_number") ?? Text(item, "question_id");
            var assignment = Text(item, "title") ?? Text(item, "assignment_title")
                             ?? Text(item, "coursework_id");
            var student = Text(item, "student_name");
            var status = PayloadFacts.StatusLabel(
                Text(item, "review_status") ?? Text(item, "status") ?? Text(item, "state"));
            var detail = Text(item, "suspicion") ?? Text(item, "detail")
                         ?? Text(item, "message") ?? Text(item, "reason")
                         ?? NestedText(item, "evidence", "reasoning") ?? string.Empty;

            // A proposal describes a batch rather than one student, so it says
            // how many papers it touches instead of repeating a student name.
            if (string.IsNullOrWhiteSpace(detail)
                && item.TryGetProperty("affected_count", out var affected)
                && affected.ValueKind == JsonValueKind.Number)
            {
                var changed = Text(item, "changed_count") ?? "0";
                detail = $"{affected.GetInt32()} papers checked · {changed} marks would change";
            }

            yield return new SchemeRow
            {
                Id = id,
                Question = string.IsNullOrWhiteSpace(question) ? "—" : $"Q{question!.TrimStart('Q', 'q')}",
                Assignment = string.IsNullOrWhiteSpace(assignment) ? "—" : assignment!,
                Student = student ?? string.Empty,
                Status = status,
                Detail = detail,
            };
        }
    }

    private static string? Text(JsonElement element, string property)
    {
        if (element.ValueKind != JsonValueKind.Object
            || !element.TryGetProperty(property, out var value))
        {
            return null;
        }

        return value.ValueKind switch
        {
            JsonValueKind.String => value.GetString(),
            JsonValueKind.Null or JsonValueKind.Undefined => null,
            _ => value.ToString(),
        };
    }
}
