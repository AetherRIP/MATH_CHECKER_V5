using System.Collections.Generic;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The Mark scheme page must let a teacher see every question and sub-question,
/// read the answer the marker will use, choose how it is handled, and change
/// that choice later — and the choices it saves are the ones correction obeys.
///
/// The stub APPLIES what the page posts, so these tests fail if the page sends
/// the wrong question, the wrong decision, or never sends anything at all.
/// </summary>
public class MarkSchemePageTests
{
    [Fact]
    public async Task Every_question_and_subquestion_is_listed_with_its_answer()
    {
        var page = await LoadedAsync();

        Assert.Null(page.Error);
        Assert.Equal("Ch2L3 — Simplifying Expressions", page.AssignmentTitle);
        Assert.Equal(5, page.Questions.Count);

        var part = page.Questions.First(q => q.Label == "1(a)");
        Assert.Equal(1, part.Depth);
        Assert.Equal("24 - 18y", part.EffectiveAnswer);
        Assert.Equal(1, part.MaxMarks);
        Assert.Equal("answer extracted", part.StatusText);
        Assert.Contains("1(a)", part.DisplayLabel);
        Assert.StartsWith("↳", part.DisplayLabel.TrimStart());

        // A question with no answer is flagged, not filled in.
        var missing = page.Questions.First(q => q.Label == "3");
        Assert.Equal("Marking guidance only — no answer to check", missing.AnswerSummary);
        Assert.Equal("Manual check", missing.StateText);
        Assert.True(missing.NeedsAttention);
    }

    [Fact]
    public async Task The_summary_reports_skipped_marks_separately_from_the_total()
    {
        var page = await LoadedAsync();

        Assert.Contains("3 questions with 2 sub-questions", page.CountsLine);
        Assert.Contains("8 of 8 marks marked", page.CountsLine);
        Assert.Contains("5 undecided", page.CountsLine);
    }

    [Fact]
    public async Task Searching_narrows_the_list_without_losing_the_others()
    {
        var page = await LoadedAsync();

        page.Filter = "No decision yet";
        page.Search = "1b";

        Assert.Single(page.Questions);
        Assert.Equal("1(b)", page.Questions[0].Label);
        Assert.Equal("1 of 5 shown", page.FilteredCount);

        page.Search = string.Empty;
        page.Filter = "All questions";
        Assert.Equal(5, page.Questions.Count);
    }

    [Fact]
    public async Task Choosing_a_decision_in_the_row_saves_it_against_that_question()
    {
        using var handler = new SchemeHandler();
        var page = await LoadedAsync(handler);

        var row = page.Questions.First(q => q.Label == "1(a)");
        row.Decision = "MANUAL";

        await WaitUntilAsync(() => handler.Decisions.Count > 0);
        Assert.Equal(("1(a)", "MANUAL"), handler.Decisions[0]);

        // The page redraws from the backend's answer, not from what it typed.
        await WaitUntilAsync(() => page.Questions.First(q => q.Label == "1(a)").Decided);
        var updated = page.Questions.First(q => q.Label == "1(a)");
        Assert.Equal("MANUAL", updated.Decision);
        Assert.Equal("Manual check", updated.StateText);
    }

    [Fact]
    public async Task Skipping_a_question_shows_the_lower_paper_total()
    {
        using var handler = new SchemeHandler();
        var page = await LoadedAsync(handler);

        page.Questions.First(q => q.Label == "1(b)").Decision = "SKIP";

        await WaitUntilAsync(() => page.CountsLine.Contains("1 skipped"));
        var skipped = page.Questions.First(q => q.Label == "1(b)");
        Assert.True(skipped.Skipped);
        Assert.Equal("Skipped", skipped.StateText);

        // 2 of the 8 marks are no longer marked, and the summary says so.
        Assert.Contains("6 of 8 marks marked", page.CountsLine);
        Assert.Contains("(2 skipped)", page.CountsLine);
    }

    [Fact]
    public async Task A_decision_keeps_the_selection_on_the_same_question()
    {
        using var handler = new SchemeHandler();
        var page = await LoadedAsync(handler);

        page.Selected = page.Questions.First(q => q.Label == "1(b)");
        page.Selected!.Decision = "SKIP";

        await WaitUntilAsync(() => page.Selected?.Label == "1(b)" && page.Selected.Skipped);
        Assert.Equal("Sub-question 1(b)", page.SelectedTitle);
    }

    [Fact]
    public async Task Editing_an_answer_sends_the_new_answer_and_shows_it()
    {
        using var handler = new SchemeHandler();
        var page = await LoadedAsync(handler);

        page.Selected = page.Questions.First(q => q.Label == "1(a)");
        page.AnswerDraft = "18y - 24";
        page.SaveAnswerCommand.Execute(null);

        await WaitUntilAsync(() => handler.Answers.Count > 0 || page.Error is not null);
        Assert.Null(page.Error);
        Assert.Equal(("1(a)", "18y - 24"), handler.Answers[0]);

        await WaitUntilAsync(() => page.Questions.First(q => q.Label == "1(a)").IsEdited);
        var edited = page.Questions.First(q => q.Label == "1(a)");
        Assert.Equal("18y - 24", edited.EffectiveAnswer);
        Assert.Contains("(edited)", edited.AnswerSummary);
    }

    [Fact]
    public async Task Restoring_an_answer_sends_a_null_so_the_scheme_answer_returns()
    {
        using var handler = new SchemeHandler();
        var page = await LoadedAsync(handler);

        page.Selected = page.Questions.First(q => q.Label == "1(a)");
        page.AnswerDraft = "something else";
        page.SaveAnswerCommand.Execute(null);
        await WaitUntilAsync(() => handler.Answers.Count > 0);

        page.Selected = page.Questions.First(q => q.Label == "1(a)");
        page.RestoreAnswerCommand.Execute(null);
        await WaitUntilAsync(() => handler.Answers.Count > 1);

        Assert.Equal(("1(a)", null), handler.Answers[^1]);
        await WaitUntilAsync(() => !page.Questions.First(q => q.Label == "1(a)").IsEdited);
        var restored = page.Questions.First(q => q.Label == "1(a)");
        Assert.Equal("24 - 18y", restored.EffectiveAnswer);
    }

    [Fact]
    public async Task The_history_shows_what_changed_and_why()
    {
        var page = await LoadedAsync();

        await WaitUntilAsync(() => page.History.Count > 0);
        Assert.Contains("Changed from manual independent check to skip",
            page.History[0].What);
        Assert.Equal("not on the printed paper", page.History[0].Reason);
        Assert.Equal("1(b)", page.History[0].Where);
    }

    [Fact]
    public async Task Finalising_with_undecided_questions_asks_before_acknowledging()
    {
        using var handler = new SchemeHandler();
        var dialogs = new RecordingDialogs { Answer = false };
        var page = await LoadedAsync(handler, dialogs);

        page.FinalizeCommand.Execute(null);

        // Declining sends nothing at all: no silent approval.
        await WaitUntilAsync(() => dialogs.Asked);
        await Task.Delay(60);
        Assert.Empty(handler.Finalised);
        Assert.Equal(string.Empty, page.FinalizeStatus);
    }

    [Fact]
    public async Task Finalising_after_acknowledging_records_the_review()
    {
        using var handler = new SchemeHandler();
        var dialogs = new RecordingDialogs { Answer = true };
        var page = await LoadedAsync(handler, dialogs);

        page.FinalizeCommand.Execute(null);

        await WaitUntilAsync(() => handler.Finalised.Count > 0);
        Assert.Single(handler.Finalised);
        Assert.True(handler.Finalised[0]);
        Assert.Contains("Review finalised", page.FinalizeStatus);
        Assert.Contains("acknowledged", page.FinalizeStatus);
    }

    [Fact]
    public async Task With_no_assignment_the_page_says_what_to_do()
    {
        using var handler = new SchemeHandler();
        var page = await LoadedAsync(handler, openAssignment: false);

        Assert.Null(page.Error);
        Assert.False(page.HasQuestions);
        Assert.Contains("Open an assignment", page.SummaryLine);
        Assert.Empty(handler.Requests);
    }

    [Fact]
    public async Task A_backend_refusal_is_shown_rather_than_swallowed()
    {
        using var handler = new SchemeHandler { RefuseDecisions = true };
        var page = await LoadedAsync(handler);

        page.Questions.First(q => q.Label == "1(a)").Decision = "MANUAL";

        await WaitUntilAsync(() => page.Error is not null);
        Assert.Contains("Unsupported decision", page.Error);
    }

    // ------------------------------------------------------------------ setup

    private static async Task<MarkSchemeViewModel> LoadedAsync(
        SchemeHandler? handler = null, IDialogService? dialogs = null,
        bool openAssignment = true)
    {
        handler ??= new SchemeHandler();
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        var session = new AppSession(settings, new MemoryLog(), host, api,
            dialogs ?? new SilentDialogs());
        if (openAssignment)
        {
            session.OpenMarkScheme("cw-1");
        }

        var page = new MarkSchemeViewModel(session);
        await page.LoadAsync();
        return page;
    }

    private static async Task WaitUntilAsync(Func<bool> condition, int timeoutMs = 5000)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        while (DateTime.UtcNow < deadline)
        {
            if (condition())
            {
                return;
            }

            await Task.Delay(25);
        }

        Assert.Fail("condition was not met before the timeout");
    }

    /// <summary>
    /// Opening the review page before the first paper is corrected is normal, and
    /// the page has to say so. It used to show a red "No mark scheme is attached
    /// to this assignment yet" over an assignment whose scheme was uploaded — a
    /// failure message about a file that is already there.
    /// </summary>
    [Fact]
    public async Task An_attached_but_unread_scheme_explains_itself_instead_of_erroring()
    {
        var page = await LoadedAsync(new SchemeHandler { DecisionsUnread = true });

        Assert.False(page.HasError);
        Assert.Null(page.Error);
        Assert.Empty(page.Questions);
        Assert.Contains("attached", page.SummaryLine, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("read", page.SummaryLine, StringComparison.OrdinalIgnoreCase);
        // It must not offer a decision on a question it has not got.
        Assert.False(page.FinalizeCommand.CanExecute(null));
        // ...but it must offer the one action that produces them, and the empty
        // list must not tell the teacher to open an assignment they are on.
        Assert.True(page.NeedsRead);
        Assert.True(page.ReadSchemeCommand.CanExecute(null));
        Assert.Contains("Read the mark scheme", page.EmptyQuestionsText,
            StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("Open an assignment", page.EmptyQuestionsText,
            StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// The gap this closes: the structure used to exist only as a side effect of
    /// correcting a paper, so a freshly uploaded scheme had nothing to review and
    /// no way to produce it. Reading on demand fills the page in place.
    /// </summary>
    [Fact]
    public async Task Reading_an_unread_scheme_makes_its_questions_reviewable()
    {
        var handler = new SchemeHandler { DecisionsUnread = true };
        var page = await LoadedAsync(handler);
        Assert.Empty(page.Questions);
        Assert.True(page.NeedsRead);

        page.ReadSchemeCommand.Execute(null);
        await WaitUntilAsync(() => page.Questions.Count > 0);

        Assert.Equal(1, handler.ReadCount);
        Assert.Equal(5, page.Questions.Count);
        Assert.False(page.NeedsRead);
        Assert.DoesNotContain("attached but has not been read", page.SummaryLine,
            StringComparison.OrdinalIgnoreCase);
        Assert.Contains("read", page.Status ?? string.Empty, StringComparison.OrdinalIgnoreCase);
        // The page is now the review page it claims to be.
        Assert.True(page.FinalizeCommand.CanExecute(null));
        Assert.Equal("1(a)", page.Questions.First(q => q.Label == "1(a)").Label);
    }

    /// <summary>A read that produces nothing must not look like a read that
    /// worked: the button stays, the reason is shown, and no error strip appears.</summary>
    [Fact]
    public async Task A_read_that_produces_nothing_keeps_the_button_and_says_why()
    {
        var handler = new SchemeHandler { DecisionsUnread = true, ReadSucceeds = false };
        var page = await LoadedAsync(handler);

        page.ReadSchemeCommand.Execute(null);
        await WaitUntilAsync(() => !page.IsBusy
                                  && page.ReadHint.Contains("Nothing was read",
                                      StringComparison.OrdinalIgnoreCase));

        Assert.False(page.HasError);
        Assert.Empty(page.Questions);
        Assert.True(page.NeedsRead);
        Assert.True(page.ReadSchemeCommand.CanExecute(null));
        Assert.Contains("mock mode", page.SummaryLine, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// Demo Mode exists so the review page and its decisions can be explored
    /// without an API key. The structure it serves is synthetic, so the page has
    /// to say that plainly — a demo that reads like a real extraction would teach
    /// the teacher to trust numbers no PDF ever produced.
    /// </summary>
    [Fact]
    public async Task A_demo_structure_is_labelled_and_still_decidable()
    {
        var handler = new SchemeHandler { DemoMode = true };
        var page = await LoadedAsync(handler);

        // Labelled: the notice names Demo Mode and says it is not a read PDF.
        Assert.True(page.HasDemoNotice);
        Assert.Contains("demo mode", page.DemoNotice, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("synthetic", page.DemoNotice, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("not read from the attached", page.DemoNotice,
            StringComparison.OrdinalIgnoreCase);
        Assert.Contains("demonstration", page.SummaryLine, StringComparison.OrdinalIgnoreCase);

        // Labelled per row too, so no single entry can pass for a real answer.
        Assert.All(page.Questions, q => Assert.Contains("demo", q.StatusText,
            StringComparison.OrdinalIgnoreCase));

        // ...and the page still works: a decision is saved, exactly as on a real scheme.
        page.Questions.First(q => q.Label == "1(a)").Decision = "MANUAL";
        await WaitUntilAsync(() => handler.Decisions.Count > 0);
        Assert.Equal(("1(a)", "MANUAL"), handler.Decisions[0]);

        // The change is recorded in the history the page shows, so the demo
        // exercise teaches the real workflow rather than a cut-down one.
        await WaitUntilAsync(() => page.History.Count > 0 && page.History[0].Where == "1(a)");
        Assert.Contains("manual independent check", page.History[0].What,
            StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// A demo answer must read as a placeholder, not as the scheme's wording.
    /// It is tagged in the text AND drawn as a placeholder, so it is obvious at a
    /// glance in a narrow column — and the moment the teacher types their own
    /// answer, that answer is styled as a real one again.
    /// </summary>
    [Fact]
    public async Task A_demo_answer_reads_as_a_placeholder_until_the_teacher_types()
    {
        var handler = new SchemeHandler { DemoMode = true };
        var page = await LoadedAsync(handler);

        var row = page.Questions.First(q => q.Label == "1(a)");
        page.Selected = row;
        Assert.StartsWith("[demo]", row.AnswerSummary, StringComparison.OrdinalIgnoreCase);
        Assert.True(row.AnswerIsPlaceholder);
        Assert.True(page.SelectedAnswerIsPlaceholder);

        page.AnswerDraft = "18y - 24";
        page.SaveAnswerCommand.Execute(null);
        await WaitUntilAsync(() => handler.Answers.Count > 0);

        var edited = page.Questions.First(q => q.Label == "1(a)");
        Assert.True(edited.IsEdited);
        // Their text is theirs: no placeholder styling, and it says it is edited.
        Assert.False(edited.AnswerIsPlaceholder);
        Assert.Contains("18y - 24", edited.AnswerSummary);
        Assert.Contains("(edited)", edited.AnswerSummary);
        Assert.False(page.SelectedAnswerIsPlaceholder);
    }

    /// <summary>
    /// Restoring a demo answer puts the demo placeholder back — it must not leave
    /// the row blank, and the restore has to be visible in the decision history.
    /// </summary>
    [Fact]
    public async Task Restoring_a_demo_answer_returns_the_placeholder_and_records_it()
    {
        var handler = new SchemeHandler { DemoMode = true };
        var page = await LoadedAsync(handler);

        page.Selected = page.Questions.First(q => q.Label == "1(b)");
        page.AnswerDraft = "y = 2 or y = -10";
        page.SaveAnswerCommand.Execute(null);
        await WaitUntilAsync(() => handler.Answers.Count > 0
                                  && page.Questions.Any(q => q.Label == "1(b)" && q.IsEdited));

        // The edit itself is in the history.
        await WaitUntilAsync(() => page.History.Count > 0
                                  && page.History[0].What.StartsWith("Answer changed",
                                      StringComparison.OrdinalIgnoreCase));
        Assert.Contains("y = 2 or y = -10", page.History[0].What);

        page.RestoreAnswerCommand.Execute(null);
        await WaitUntilAsync(() => handler.Answers.Count > 1);

        var restored = page.Questions.First(q => q.Label == "1(b)");
        Assert.False(restored.IsEdited);
        Assert.True(restored.AnswerIsPlaceholder);
        Assert.StartsWith("[demo]", restored.AnswerSummary, StringComparison.OrdinalIgnoreCase);

        await WaitUntilAsync(() => page.History.Count > 0
                                  && page.History[0].What.Contains("restored",
                                      StringComparison.OrdinalIgnoreCase));
        Assert.Contains("restored", page.History[0].What, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// A decision, answer or note response repeats the rows WITHOUT the scheme
    /// block. The label was read from that block on every apply, so saving a
    /// decision on a demo scheme silently relabelled it as a real extraction —
    /// exactly the moment the teacher starts trusting the numbers.
    /// </summary>
    [Fact]
    public async Task The_demo_label_survives_saving_a_decision()
    {
        var handler = new SchemeHandler { DemoMode = true };
        var page = await LoadedAsync(handler);
        Assert.True(page.HasDemoNotice);

        page.Questions.First(q => q.Label == "1(b)").Decision = "SKIP";
        await WaitUntilAsync(() => handler.Decisions.Count > 0);
        await WaitUntilAsync(() => page.CountsLine.Contains("1 skipped"));

        Assert.True(page.HasDemoNotice);
        Assert.Contains("synthetic", page.DemoNotice, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("demonstration", page.SummaryLine, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// A real scheme must show no notice at all: a warning that appears on every
    /// scheme is noise, and it would make the demo label meaningless.
    /// </summary>
    [Fact]
    public async Task A_read_scheme_carries_no_demo_notice()
    {
        var page = await LoadedAsync();

        Assert.False(page.HasDemoNotice);
        Assert.Equal(string.Empty, page.DemoNotice);
        Assert.Equal("answer extracted", page.Questions.First(q => q.Label == "1(a)").StatusText);
    }

    // ------------------------------------------------------------------- stub

    /// <summary>A backend that APPLIES what the page posts.</summary>
    private sealed class SchemeHandler : HttpMessageHandler
    {
        private readonly List<Dictionary<string, object?>> _rows = new()
        {
            Row("1", "1", 0, 3, new[] { "24 - 18y" }, "RELIABLE", "AUTO", decided: false),
            Row("1(a)", "1a", 1, 1, new[] { "24 - 18y" }, "RELIABLE", "AUTO", decided: false),
            Row("1(b)", "1b", 1, 2, new[] { "y^2 + 8y - 20" }, "RELIABLE", "AUTO", decided: false),
            Row("2", "2", 0, 1, new[] { "10p - 15" }, "RELIABLE", "AUTO", decided: false),
            Row("3", "3", 0, 4, Array.Empty<string>(), "CRITERIA_ONLY", "MANUAL", decided: false,
                criteria: "M1 A1 cao"),
        };

        /// <summary>What the history endpoint returns: the decisions and answer
        /// changes this stub has actually recorded, newest first.</summary>
        private readonly List<Dictionary<string, object?>> _history = new()
        {
            new()
            {
                ["question_key"] = "1b", ["question_label"] = "1(b)",
                ["action"] = "DECISION", ["previous_decision"] = "MANUAL",
                ["new_decision"] = "SKIP", ["previous_answer"] = null,
                ["new_answer"] = null, ["reason"] = "not on the printed paper",
                ["source"] = "HUMAN", ["actor"] = "teacher", ["at"] = 1793000000.0,
            },
        };

        public List<string> Requests { get; } = new();
        public List<(string Question, string Decision)> Decisions { get; } = new();
        public List<(string Question, string? Answer)> Answers { get; } = new();
        public List<bool> Finalised { get; } = new();
        public bool RefuseDecisions { get; set; }

        /// <summary>The scheme is on disk but has never been read, which is the
        /// ordinary state before the first paper of an assignment is corrected.</summary>
        public bool DecisionsUnread { get; set; }

        /// <summary>Whether the on-demand read finds a structure.</summary>
        public bool ReadSucceeds { get; set; } = true;

        /// <summary>Demo Mode serves a labelled synthetic structure instead of
        /// questions read from the PDF.</summary>
        public bool DemoMode { get; set; }

        public int ReadCount { get; private set; }

        private static Dictionary<string, object?> Row(string label, string key, int depth,
            int marks, string[] answers, string status, string decision, bool decided,
            string criteria = "", string? answerText = null)
        {
            return new Dictionary<string, object?>
            {
                ["question_key"] = key,
                ["question_label"] = label,
                ["depth"] = depth,
                ["max_marks"] = marks,
                ["page"] = 2,
                ["answers"] = answers,
                ["answer_text"] = answerText,
                ["criteria"] = criteria,
                ["extraction_status"] = status,
                ["reliable"] = status == "RELIABLE",
                ["decision"] = decision,
                ["decided"] = decided,
                ["skipped"] = false,
                ["note"] = string.Empty,
            };
        }

        /// <summary>The backend repeats the rows after an edit, but only the
        /// GET carries the scheme block. The stub matches that, so a page that
        /// reads its scheme state from the wrong response fails here.</summary>
        private string Payload(bool withScheme = true)
        {
            // Demo Mode serves placeholder answers, not the stub's real ones —
            // the same shape the backend builds, tag first.
            var rows = DemoMode
                ? _rows.Select(r => new Dictionary<string, object?>(r)
                {
                    ["extraction_status"] = "DEMO",
                    ["reliable"] = false,
                    ["answers"] = new[]
                    {
                        $"[demo] placeholder answer for question {r["question_label"]}"
                        + " — type over it to try editing",
                    },
                }).ToList()
                : _rows;
            var skipped = rows.Where(r => (bool)r["skipped"]!).ToList();
            var manual = rows.Where(r => !(bool)r["skipped"]! && (string)r["decision"]! == "MANUAL").Count();
            var auto = rows.Where(r => !(bool)r["skipped"]! && (string)r["decision"]! == "AUTO").Count();
            var marked = 8 - skipped.Sum(r => (int)r["max_marks"]!);

            return JsonSerializer.Serialize(new Dictionary<string, object?>
            {
                ["coursework_id"] = "cw-1",
                ["assignment_title"] = "Ch2L3 — Simplifying Expressions",
                ["questions"] = rows,
                ["orphaned"] = new List<object>(),
                ["summary"] = new Dictionary<string, object?>
                {
                    ["questions"] = 3,
                    ["subquestions"] = 2,
                    ["total"] = 5,
                    ["automatic"] = auto,
                    ["manual"] = manual,
                    ["skipped"] = skipped.Count,
                    ["unresolved"] = rows.Count(r => !(bool)r["decided"]!),
                    ["unreliable"] = rows.Count(r => (string)r["extraction_status"]! != "RELIABLE"),
                    ["total_marks"] = 8,
                    ["skipped_marks"] = 8 - marked,
                    ["marked_marks"] = marked,
                    ["manual_marks"] = 0,
                },
                ["scheme"] = withScheme
                    ? new Dictionary<string, object?> { ["demo"] = DemoMode }
                    : null,
            });
        }

        /// <summary>One history row, newest first — what the backend writes when
        /// a decision or an answer actually changes.</summary>
        private void Record(string label, string action, string? previousDecision = null,
            string? newDecision = null, string? previousAnswer = null,
            string? newAnswer = null, string? reason = null)
        {
            _history.Insert(0, new Dictionary<string, object?>
            {
                ["question_key"] = label,
                ["question_label"] = label,
                ["action"] = action,
                ["previous_decision"] = previousDecision,
                ["new_decision"] = newDecision,
                ["previous_answer"] = previousAnswer,
                ["new_answer"] = newAnswer,
                ["reason"] = reason ?? string.Empty,
                ["source"] = "HUMAN",
                ["actor"] = "teacher",
                ["at"] = 1793000100.0,
            });
        }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;
            var body = request.Content is null
                ? string.Empty
                : await request.Content.ReadAsStringAsync(cancellationToken);
            Requests.Add($"{request.Method} {path}");

            if (request.Method == HttpMethod.Get && path.EndsWith("/scheme/decisions"))
            {
                if (DecisionsUnread)
                {
                    return Error(HttpStatusCode.Conflict, "scheme_not_read",
                        "The mark scheme is attached but has not been read yet. "
                        + "MathChecker reads it while correcting the first paper for "
                        + "this assignment; its questions can be reviewed after that.");
                }

                return Json(Payload());
            }

            if (request.Method == HttpMethod.Post && path.EndsWith("/scheme/read"))
            {
                ReadCount++;
                if (!ReadSucceeds)
                {
                    return Json("""
                        {"coursework_id":"cw-1","read":false,"cached":false,"questions":0,
                         "reason":"mock mode: blueprint extraction skipped"}
                        """);
                }

                DecisionsUnread = false;
                return Json("""
                    {"coursework_id":"cw-1","read":true,"cached":false,"questions":5,
                     "total_marks":8}
                    """);
            }

            if (request.Method == HttpMethod.Get && path.EndsWith("/scheme/decision-history"))
            {
                return Json(JsonSerializer.Serialize(new Dictionary<string, object?>
                {
                    ["items"] = _history,
                    ["total"] = _history.Count,
                }));
            }

            if (request.Method == HttpMethod.Post && path.EndsWith("/scheme/decision"))
            {
                if (RefuseDecisions)
                {
                    return Error(HttpStatusCode.BadRequest, "bad_request",
                        "Unsupported decision 'X'. Use one of: AUTO, MANUAL, SKIP.");
                }

                var doc = JsonDocument.Parse(body).RootElement;
                var question = doc.GetProperty("question_number").GetString()!;
                var decision = doc.GetProperty("decision").GetString()!;
                Decisions.Add((question, decision));

                var row = _rows.First(r => (string)r["question_label"]! == question);
                Record(question, "DECISION", previousDecision: (string?)row["decision"],
                    newDecision: decision,
                    reason: doc.TryGetProperty("reason", out var why) ? why.GetString() : null);
                row["decision"] = decision;
                row["decided"] = true;
                row["skipped"] = decision == "SKIP";
                return Json(Payload(withScheme: false));
            }

            if (request.Method == HttpMethod.Post && path.EndsWith("/scheme/answer"))
            {
                var doc = JsonDocument.Parse(body).RootElement;
                var question = doc.GetProperty("question_number").GetString()!;
                var answer = doc.TryGetProperty("answer", out var a)
                             && a.ValueKind != JsonValueKind.Null
                    ? a.GetString()
                    : null;
                Answers.Add((question, answer));

                var row = _rows.First(r => (string)r["question_label"]! == question);
                var previous = row["answer_text"] as string
                               ?? ((string[])row["answers"]!)[0];
                Record(question, "ANSWER", previousAnswer: previous, newAnswer: answer);
                row["answer_text"] = answer;
                return Json(Payload(withScheme: false));
            }

            if (request.Method == HttpMethod.Post && path.EndsWith("/scheme/finalize"))
            {
                var doc = JsonDocument.Parse(body).RootElement;
                var acknowledged = doc.GetProperty("acknowledge_unresolved").GetBoolean();
                Finalised.Add(acknowledged);
                return Json(JsonSerializer.Serialize(new Dictionary<string, object?>
                {
                    ["total"] = 5,
                    ["automatic"] = 3,
                    ["manual"] = 2,
                    ["skipped"] = 0,
                    ["acknowledged_unresolved"] = acknowledged ? 5 : 0,
                }));
            }

            return Error(HttpStatusCode.NotFound, "unexpected", "No stub for " + path);
        }

        private static HttpResponseMessage Json(string body) =>
            new(HttpStatusCode.OK)
            {
                Content = new StringContent(body, Encoding.UTF8, "application/json"),
            };

        private static HttpResponseMessage Error(HttpStatusCode status, string code, string message) =>
            new(status)
            {
                Content = new StringContent(
                    JsonSerializer.Serialize(new { code, message }), Encoding.UTF8,
                    "application/json"),
            };
    }

    private sealed class RecordingDialogs : IDialogService
    {
        public bool Answer { get; set; }
        public bool Asked { get; private set; }

        public bool Confirm(string title, string message)
        {
            Asked = true;
            return Answer;
        }

        public void Info(string title, string message)
        {
        }

        public void Error(string title, string message)
        {
        }

        public string? PickOpenFile(string title, string filter) => null;
        public string? PickSaveFile(string title, string filter, string defaultName) => null;
        public string? AskText(string title, string message, string initial = "") => null;
        public void OpenExternal(string pathOrUrl)
        {
        }

        public void OpenFolder(string directory)
        {
        }
    }

    private sealed class SilentDialogs : IDialogService
    {
        public bool Confirm(string title, string message) => false;
        public void Info(string title, string message)
        {
        }

        public void Error(string title, string message)
        {
        }

        public string? PickOpenFile(string title, string filter) => null;
        public string? PickSaveFile(string title, string filter, string defaultName) => null;
        public string? AskText(string title, string message, string initial = "") => null;
        public void OpenExternal(string pathOrUrl)
        {
        }

        public void OpenFolder(string directory)
        {
        }
    }

    private sealed class AlwaysOurs : IHealthProbe
    {
        public Task<HealthResult> CheckAsync(int port, CancellationToken cancellationToken) =>
            Task.FromResult(new HealthResult(HealthKind.Ours, null));
    }

    private sealed class UnusedStarter : IProcessStarter
    {
        public IProcessHandle Start(BackendLaunchPlan plan) =>
            throw new InvalidOperationException("the test attaches to a backend, it never starts one");
    }
}
