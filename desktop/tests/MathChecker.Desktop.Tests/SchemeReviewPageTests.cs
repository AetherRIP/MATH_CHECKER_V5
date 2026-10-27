using System.Collections.Generic;
using System.Net;
using System.Text;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Core.Workspace;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The mark-scheme review screen must answer two questions a teacher actually
/// asks: "what exactly is wrong with the scheme here?" — answered from the real
/// uploaded page and the stored answer, never from prose this app made up — and
/// "can I just leave this question out?" — answered by a real exclusion that
/// reaches the marking prompt, the result and the total.
/// </summary>
public class SchemeReviewPageTests
{
    private const string Summary = """
        {"open_discrepancies":1,"confirmed":0,"awaiting_approval":0,"items":[]}
        """;

    private const string DiscrepancyList = """
        {"items":[
          {"id":"d1","question_number":"12","assignment_title":"Ch2L3 - Simplifying Expressions",
           "coursework_id":"cw-1","student_name":"Joudy Ehab","review_status":"AWAITING_REVIEW",
           "state":"POSSIBLE_DISCREPANCY","suspicion":"The scheme's answer looks wrong.",
           "scheme_page":4,"ignored":false}
        ],"total":1}
        """;

    private const string Proposals = """{"items":[],"total":0}""";

    /// <summary>The detail the backend really returns: the discrepancy row, the
    /// scheme context for its question, and the student's stored answer.</summary>
    private const string Detail = """
        {"discrepancy":{
            "id":"d1","coursework_id":"cw-1","course_id":"c1",
            "assignment_title":"Ch2L3 - Simplifying Expressions",
            "question_number":"12","discrepancy_type":"incorrect_answer",
            "state":"POSSIBLE_DISCREPANCY","review_status":"AWAITING_REVIEW",
            "scheme_answer":"11p + 6","verified_answer":"24x - 9",
            "verified_reasoning":"The printed worksheet asks to expand 3x(8x - 3), not to simplify 11p + 6.",
            "confidence":0.95,"max_marks":2,"awarded_marks":1,"justified_marks":1,
            "student_name":"Joudy Ehab","student_id":"s1","student_result_id":"res-1",
            "suspicion":"The reference answer belongs to a different question.",
            "evidence":{"scheme_answer":"11p + 6","verified_answer":"24x - 9",
                        "reasoning":"The printed worksheet asks to expand 3x(8x - 3).",
                        "confidence":0.95}},
         "scheme":{"question":"12","criteria":"M1 Any 3 terms correct, A1 cao 24x - 9",
                   "max_marks":2,"printed_label":"Jan 17/3H/Q5 b","page":4,
                   "page_image":"/api/assignments/cw-1/mark-scheme/page/4.png"},
         "answer":{"student_answer":"24x - 9","working":"3x(8x-3)","loss_reason":"differs",
                   "awarded":1,"max_marks":2},
         "ignored":false,"review_history":[],"proposals":[]}
        """;

    [Fact]
    public async Task The_finding_shows_the_real_page_and_both_answers()
    {
        using var handler = new SchemeHandler();
        var page = await LoadedAsync(handler);

        Assert.Contains("Q12", page.FindingTitle);
        Assert.Contains("mark scheme", page.FindingTitle);
        Assert.Contains("printed worksheet", page.FindingNote);

        // What the scheme says, from the scheme itself, and what the student wrote.
        Assert.Contains("11p + 6", page.SchemeSays);
        Assert.Contains("M1 Any 3 terms correct", page.SchemeSays);
        Assert.Contains("Jan 17/3H/Q5 b", page.SchemeSays);
        Assert.Equal("24x - 9", page.StudentSays);
        Assert.Contains("24x - 9", page.IndependentCheck);

        // Where it is in the uploaded document, and the page itself is fetched.
        Assert.Contains("Mark-scheme page 4", page.SchemeWhere);
        Assert.Contains("1 of 2 marks awarded", page.SchemeWhere);
        Assert.Equal(1, handler.ImageRequests);
        Assert.Contains("Page 4 of the uploaded mark scheme", page.SchemeImageCaption);
    }

    [Fact]
    public async Task A_scheme_with_no_page_says_so_instead_of_drawing_a_blank_box()
    {
        using var handler = new SchemeHandler { Detail = Detail.Replace("\"page\":4", "\"page\":null") };
        var page = await LoadedAsync(handler);

        Assert.False(page.HasSchemeImage);
        Assert.Contains("did not say which page", page.SchemeImageNote);
        Assert.Equal(0, handler.ImageRequests);
    }

    [Fact]
    public async Task Skipping_a_question_posts_the_assignment_and_the_question()
    {
        using var handler = new SchemeHandler();
        var page = await LoadedAsync(handler);

        Assert.Equal(3, page.Questions.Count);
        var question12 = page.Questions.Single(q => q.Number == "12");
        Assert.False(question12.IsIgnored);
        Assert.Equal("Skip it", question12.ActionText);

        page.ToggleQuestionCommand.Execute(question12);
        await WaitUntilAsync(() => handler.IgnoreBodies.Count > 0);
        await WaitUntilAsync(() => page.Questions.Any(q => q.Number == "12" && q.IsIgnored));

        var body = handler.IgnoreBodies[0];
        Assert.Contains("\"coursework_id\":\"cw-1\"", body);
        Assert.Contains("\"question_number\":\"12\"", body);

        // The list reloads from the backend, and the skipped marks leave the total.
        var skipped = page.Questions.Single(q => q.Number == "12");
        Assert.Equal("Skipped", skipped.StateText);
        Assert.Equal("Mark it again", skipped.ActionText);
        Assert.Contains("1 skipped question", page.QuestionsNote);
        Assert.Contains("marked total 5 of 7", page.QuestionsNote);
        Assert.Contains("skipped", page.Status);

        // The finding's own button now offers to put it back.
        Assert.True(page.SelectedQuestionIgnored);
        Assert.Equal("Mark 12 again", page.IgnoreSelectedText);
    }

    [Fact]
    public async Task Restoring_a_question_removes_the_exclusion()
    {
        using var handler = new SchemeHandler { StartIgnored = true };
        var page = await LoadedAsync(handler);

        var row = page.Questions.Single(q => q.Number == "12");
        Assert.True(row.IsIgnored);
        Assert.Equal("Mark it again", row.ActionText);

        page.ToggleQuestionCommand.Execute(row);
        await WaitUntilAsync(() => handler.RestoreRequests > 0);
        await WaitUntilAsync(() => page.Questions.Any(q => q.Number == "12" && !q.IsIgnored));

        Assert.Contains("scheme/ignored/cw-1/12", handler.Requests.Last(r => r.Method == "DELETE").Url);
        Assert.Contains("marked again", page.Status);
        Assert.Contains("marked total 7 of 7", page.QuestionsNote);
    }

    [Fact]
    public async Task The_findings_button_skips_the_same_question_the_row_would()
    {
        using var handler = new SchemeHandler();
        var page = await LoadedAsync(handler);

        Assert.True(page.CanIgnoreSelected);
        Assert.Equal("Skip 12 from marking", page.IgnoreSelectedText);

        page.IgnoreSelectedCommand.Execute(null);
        await WaitUntilAsync(() => handler.IgnoreBodies.Count > 0);

        Assert.Contains("\"question_number\":\"12\"", handler.IgnoreBodies[0]);
    }

    [Fact]
    public async Task Each_subquestion_can_be_skipped_on_its_own()
    {
        using var handler = new SchemeHandler { Questions = """
            {"questions":[
              {"question_number":"4","max_marks":5,"page":2,"ignored":false,
               "criteria":"M1 M1 A1",
               "parts":[{"number":"4(a)","max_marks":2,"ignored":false},
                        {"number":"4(b)","max_marks":3,"ignored":false}]}
             ],"items":[],"total_marks":5,"marked_total":5}
            """ };
        var page = await LoadedAsync(handler);

        Assert.Equal(3, page.Questions.Count);
        var part = page.Questions.Single(q => q.Number == "4(b)");
        Assert.Contains("Subquestion of Q4", part.KindText);

        page.ToggleQuestionCommand.Execute(part);
        await WaitUntilAsync(() => handler.IgnoreBodies.Count > 0);

        Assert.Contains("\"question_number\":\"4(b)\"", handler.IgnoreBodies[0]);
    }

    // ------------------------------------------------------- recording a decision

    /// <summary>A decision has to remove the row from the list the screen calls
    /// open.
    ///
    /// The grid lists "open" discrepancies and its chip prints that count, while
    /// the summary beside it counts only rows awaiting review. Two separate faults
    /// kept a decided row there: the page asked for the WHOLE audit table rather
    /// than the open set, and each command's <c>await LoadAsync()</c> returned
    /// immediately because the command's own busy window was still open, so the
    /// list was never re-read at all.
    /// </summary>
    [Fact]
    public async Task Deciding_a_discrepancy_removes_it_from_the_open_list()
    {
        using var handler = new DecidingSchemeHandler();
        var page = await LoadedAsync(handler, dialogs: new ScriptedDialogs("1"));
        Assert.Equal(2, page.Discrepancies.Count);

        page.SelectedDiscrepancy = page.Discrepancies.Single(r => r.Id == "d1");
        page.DecideCommand.Execute(null);
        await WaitUntilAsync(() => page.Discrepancies.Count == 1,
            message: () => $"still listed: {string.Join(",", page.Discrepancies.Select(r => r.Id))}"
                         + $" urls={string.Join(" | ", handler.ListUrls)}");

        // The row really went, and the summary agrees with the list.
        Assert.DoesNotContain(page.Discrepancies, r => r.Id == "d1");
        Assert.Equal("d2", page.Discrepancies.Single().Id);
        Assert.Contains("1 discrepancy awaiting review", page.Summary);
        Assert.Contains("left the open list", page.Status);

        // The decision went to the backend, and the list was re-read from the
        // open set — not from the whole table.
        Assert.Contains("\"decision\":\"CONFIRM_SCHEME_CORRECT\"", handler.DecisionBodies.Single());
        Assert.True(handler.ListUrls.Count >= 2,
            "the list was never re-read after the decision: " + string.Join(" | ", handler.ListUrls));
        Assert.All(handler.ListUrls, url => Assert.Contains("review_status=AWAITING_REVIEW", url));
    }

    [Fact]
    public async Task A_row_asking_for_more_investigation_stays_open()
    {
        // Decision 6 is REQUEST_FURTHER_INVESTIGATION: the backend keeps the row
        // awaiting review, so the page must not tell the teacher it is settled.
        using var handler = new DecidingSchemeHandler { StaysOpen = true };
        var page = await LoadedAsync(handler, dialogs: new ScriptedDialogs("6"));

        page.SelectedDiscrepancy = page.Discrepancies.Single(r => r.Id == "d1");
        page.DecideCommand.Execute(null);
        await WaitUntilAsync(() => page.Status?.Contains("stays in the open list") == true
                                   && handler.ListUrls.Count >= 2,
            message: () => $"status='{page.Status}' count={page.Discrepancies.Count} "
                         + $"urls={string.Join(" | ", handler.ListUrls)}");

        Assert.Contains(page.Discrepancies, r => r.Id == "d1");
        Assert.Contains("REQUEST_FURTHER_INVESTIGATION", handler.DecisionBodies.Single());
        Assert.Contains("stays in the open list", page.Status);
        Assert.Contains("2 discrepancies awaiting review", page.Summary);
    }

    [Fact]
    public async Task Deciding_the_last_discrepancy_clears_the_evidence_it_was_showing()
    {
        using var handler = new DecidingSchemeHandler { OpenRows = 1 };
        var page = await LoadedAsync(handler, dialogs: new ScriptedDialogs("1"));

        // The only open row is selected, so its evidence is on screen.
        await WaitUntilAsync(() => page.FindingTitle.Contains("Q12"),
            message: () => $"title='{page.FindingTitle}'");

        page.DecideCommand.Execute(null);
        await WaitUntilAsync(() => page.Discrepancies.Count == 0,
            message: () => $"left listed: {string.Join(",", page.Discrepancies.Select(r => r.Id))}");

        // Nothing is open, so nothing may still describe the decided row.
        Assert.Contains("0 discrepancies awaiting review", page.Summary);
        Assert.Contains("Nothing is awaiting review", page.FindingTitle);
        Assert.DoesNotContain("Q12", page.FindingTitle);
        Assert.Empty(page.Questions);
        Assert.False(page.HasSchemeSays);
    }

    [Fact]
    public async Task A_decision_the_backend_refuses_is_reported_and_changes_nothing()
    {
        using var handler = new DecidingSchemeHandler { DecisionStatus = HttpStatusCode.BadRequest };
        var page = await LoadedAsync(handler, dialogs: new ScriptedDialogs("1"));

        page.SelectedDiscrepancy = page.Discrepancies.Single(r => r.Id == "d1");
        page.DecideCommand.Execute(null);
        await WaitUntilAsync(() => page.HasError,
            message: () => $"error='{page.Error}' status='{page.Status}' "
                         + $"bodies={handler.DecisionBodies.Count} urls={Traffic(handler)}");

        // The refusal is shown, and the row it refused to decide is still open.
        Assert.Contains("Unknown decision 'NOPE'", page.Error);
        Assert.Equal(2, page.Discrepancies.Count);
    }

    [Fact]
    public async Task An_unreadable_discrepancy_says_so_rather_than_showing_an_empty_card()
    {
        using var handler = new SchemeHandler { DetailStatus = HttpStatusCode.NotFound };
        var page = await LoadedAsync(handler, ready: p => p.FindingTitle.Contains("could not be read"));

        Assert.Contains("could not be read", page.FindingTitle);
        Assert.False(page.HasSchemeSays);
        Assert.False(page.HasStudentSays);
        Assert.Empty(page.Questions);
        Assert.Contains("Nothing to show", page.QuestionsNote);
    }

    // ------------------------------------------------------------------ setup

    private static async Task<SchemeReviewViewModel> LoadedAsync(HttpMessageHandler handler,
        Func<SchemeReviewViewModel, bool>? ready = null, IDialogService? dialogs = null)
    {
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        var session = new AppSession(settings, new MemoryLog(), host, api,
            dialogs ?? new SilentDialogs());

        var page = new SchemeReviewViewModel(session, new FakeDecoder());
        await page.LoadAsync();
        Assert.Null(page.Error);
        await WaitUntilAsync(() => ready is null ? page.Questions.Count > 0 : ready(page),
            message: () => $"note='{page.QuestionsNote}' title='{page.FindingTitle}' "
                         + $"error='{page.Error}' urls={Traffic(handler)}");
        return page;
    }

    /// <summary>What the page actually asked for, whichever handler answered — a
    /// failing wait has to be able to print the real traffic, not a guess.</summary>
    private static string Traffic(HttpMessageHandler handler) => handler switch
    {
        SchemeHandler standard => string.Join(" | ",
            standard.Requests.Select(r => r.Method + " " + r.Url)),
        DecidingSchemeHandler deciding => string.Join(" | ", deciding.ListUrls),
        _ => "(not recorded)",
    };

    private static async Task WaitUntilAsync(Func<bool> condition, int attempts = 200,
        Func<string>? message = null)
    {
        for (var i = 0; i < attempts; i++)
        {
            if (condition())
            {
                return;
            }

            await Task.Delay(20);
        }

        Assert.Fail("The expected state was never reached. " + (message?.Invoke() ?? string.Empty));
    }

    /// <summary>A decoder stub: the review page must not depend on WPF imaging to
    /// report what the scheme says.</summary>
    private sealed class FakeDecoder : IPageImageDecoder
    {
        public Task<DecodedPage> DecodeAsync(byte[] bytes,
            CancellationToken cancellationToken = default) =>
            Task.FromResult(new DecodedPage(1000, 1414, null));
    }

    /// <summary>Answers by URL, so a retry or a refresh cannot consume the wrong
    /// queued body, and records what the page actually asked for.</summary>
    private sealed class SchemeHandler : HttpMessageHandler
    {
        public string Detail { get; set; } = SchemeReviewPageTests.Detail;

        public string Questions { get; set; } = """
            {"questions":[
              {"question_number":"11","max_marks":2,"page":3,"ignored":false,"criteria":"B1 B1","parts":[]},
              {"question_number":"12","max_marks":2,"page":4,"ignored":false,
               "criteria":"M1 Any 3 terms correct, A1 cao 11p + 6",
               "printed_label":"Jan 17/3H/Q5 b","parts":[]},
              {"question_number":"13","max_marks":3,"page":5,"ignored":false,"criteria":"M1 M1 A1","parts":[]}
             ],"items":[],"total_marks":7,"marked_total":7}
            """;

        public bool StartIgnored { get; set; }

        /// <summary>Whether question 12 is currently excluded, after whatever
        /// the page has asked for so far.</summary>
        private bool Ignored => _restored ? false : StartIgnored || _toggled;

        private bool _toggled;
        private bool _restored;

        public HttpStatusCode DetailStatus { get; set; } = HttpStatusCode.OK;

        public List<RecordedRequest> Requests { get; } = new();

        public List<string> IgnoreBodies { get; } = new();

        public int ImageRequests { get; private set; }

        public int RestoreRequests { get; private set; }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var body = request.Content is null
                ? null
                : await request.Content.ReadAsStringAsync(cancellationToken);
            var url = request.RequestUri!.AbsoluteUri;
            Requests.Add(new RecordedRequest(request.Method.Method, url, body,
                request.Headers.ToDictionary(h => h.Key, h => string.Join(",", h.Value))));

            var path = request.RequestUri.AbsolutePath;

            if (path.EndsWith("/scheme/summary", StringComparison.Ordinal))
            {
                return Json(Summary);
            }

            if (path.EndsWith("/scheme/proposals", StringComparison.Ordinal))
            {
                return Json(Proposals);
            }

            if (path.EndsWith("/scheme/discrepancies", StringComparison.Ordinal))
            {
                return Json(DiscrepancyList);
            }

            if (path.EndsWith("/scheme/discrepancies/d1", StringComparison.Ordinal))
            {
                return DetailStatus == HttpStatusCode.OK
                    ? Json(DetailForCurrentState())
                    : Error(DetailStatus);
            }

            if (path.EndsWith("/scheme/questions", StringComparison.Ordinal))
            {
                return Json(QuestionsForCurrentState());
            }

            if (path.Contains("/mark-scheme/page/", StringComparison.Ordinal))
            {
                ImageRequests++;
                var content = new ByteArrayContent(Encoding.UTF8.GetBytes("PAGE4"));
                content.Headers.ContentType = new("image/png");
                return new HttpResponseMessage(HttpStatusCode.OK) { Content = content };
            }

            if (path.EndsWith("/scheme/ignore", StringComparison.Ordinal)
                && request.Method == HttpMethod.Post)
            {
                IgnoreBodies.Add(body ?? string.Empty);
                _toggled = true;
                _restored = false;
                return Json("""{"ignored":{"question_number":"12"},"excluded":[],"marked_total":5}""");
            }

            if (path.Contains("/scheme/ignored/", StringComparison.Ordinal)
                && request.Method == HttpMethod.Delete)
            {
                RestoreRequests++;
                _restored = true;
                return Json("""{"restored":"12","still_excluded":[],"marked_total":7}""");
            }

            return Error(HttpStatusCode.NotFound);
        }

        private string DetailForCurrentState() =>
            Detail.Replace("\"ignored\":false", Ignored ? "\"ignored\":true" : "\"ignored\":false");

        private string QuestionsForCurrentState()
        {
            if (!Ignored)
            {
                return Questions;
            }

            // What the backend returns once 12 is excluded: the question stays in
            // the list, marked as skipped, and the marked total drops by its marks.
            var total = 7 - 2;
            return Questions
                .Replace("\"question_number\":\"12\",\"max_marks\":2,\"page\":4,\"ignored\":false",
                    "\"question_number\":\"12\",\"max_marks\":2,\"page\":4,\"ignored\":true")
                .Replace("\"marked_total\":7", $"\"marked_total\":{total}");
        }

        private static HttpResponseMessage Json(string json) =>
            new(HttpStatusCode.OK)
            {
                Content = new StringContent(json, Encoding.UTF8, "application/json"),
            };

        private static HttpResponseMessage Error(HttpStatusCode status) =>
            new(status)
            {
                Content = new StringContent(
                    """{"code":"not_found","message":"That discrepancy does not exist."}""",
                    Encoding.UTF8, "application/json"),
            };
    }

    /// <summary>Answers the decision dialog the way a teacher would.</summary>
    private sealed class ScriptedDialogs : IDialogService
    {
        private readonly string _answer;

        public ScriptedDialogs(string answer) => _answer = answer;

        public bool Confirm(string title, string message) => true;

        public void Info(string title, string message)
        {
        }

        public void Error(string title, string message)
        {
        }

        public string? PickOpenFile(string title, string filter) => null;

        public string? PickSaveFile(string title, string filter, string defaultName) => null;

        /// <summary>The decision menu: the reply is the number of a listed
        /// decision, exactly as the dialog promises.</summary>
        public string? AskText(string title, string message, string initial = "") => _answer;

        public void OpenExternal(string pathOrUrl)
        {
        }

        public void OpenFolder(string directory)
        {
        }
    }

    /// <summary>A backend that really applies the decision: the decided row leaves
    /// the open set, exactly as <c>/scheme/discrepancies?review_status=AWAITING_REVIEW</c>
    /// behaves, while the unfiltered listing keeps the audit row. It records the
    /// listing URLs so a test can prove the page asked for the open set — and that
    /// it asked again at all.</summary>
    private sealed class DecidingSchemeHandler : HttpMessageHandler
    {
        /// <summary>The questions the selected row's assignment declares. The
        /// screen refuses to call itself loaded with none, so they are real.
        /// </summary>
        private const string Questions = """
            {"questions":[
              {"question_number":"12","max_marks":2,"page":4,"ignored":false,
               "criteria":"M1 Any 3 terms correct, A1 cao 11p + 6","parts":[]}
             ],"items":[],"total_marks":2,"marked_total":2}
            """;

        private const string Proposals = "{\"items\":[],\"total\":0}";

        /// <summary>How many rows the open set holds to begin with.</summary>
        public int OpenRows { get; set; } = 2;

        /// <summary>REQUEST_FURTHER_INVESTIGATION leaves the row awaiting review.</summary>
        public bool StaysOpen { get; set; }

        public HttpStatusCode DecisionStatus { get; set; } = HttpStatusCode.OK;

        public List<string> ListUrls { get; } = new();
        public List<string> DecisionBodies { get; } = new();

        private bool _decided;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var body = request.Content is null
                ? null
                : await request.Content.ReadAsStringAsync(cancellationToken);
            var path = request.RequestUri!.AbsolutePath;
            var closes = !StaysOpen;

            if (path.EndsWith("/scheme/discrepancies/d1/decision", StringComparison.Ordinal))
            {
                DecisionBodies.Add(body ?? string.Empty);
                if (DecisionStatus != HttpStatusCode.OK)
                {
                    return new HttpResponseMessage(DecisionStatus)
                    {
                        Content = new StringContent(
                            """{"code":"bad_request","message":"Unknown decision 'NOPE'."}""",
                            Encoding.UTF8, "application/json"),
                    };
                }

                _decided = true;
                var status = closes ? "REVIEW_COMPLETED" : "AWAITING_REVIEW";
                return Json($"{{\"discrepancy\":{{\"id\":\"d1\",\"review_status\":\"{status}\"}}}}");
            }

            if (path.EndsWith("/scheme/summary", StringComparison.Ordinal))
            {
                var open = OpenRows - (_decided && closes ? 1 : 0);
                return Json($"{{\"open_discrepancies\":{open},\"confirmed\":0,"
                            + $"\"awaiting_approval\":0,\"items\":[]}}");
            }

            if (path.EndsWith("/scheme/discrepancies", StringComparison.Ordinal))
            {
                var url = request.RequestUri.PathAndQuery;
                ListUrls.Add(url);
                return Json(OpenList(url, closes));
            }

            if (path.EndsWith("/scheme/proposals", StringComparison.Ordinal))
            {
                return Json(Proposals);
            }

            if (path.EndsWith("/scheme/discrepancies/d2", StringComparison.Ordinal))
            {
                return Json(Detail.Replace("\"id\":\"d1\"", "\"id\":\"d2\""));
            }

            if (path.EndsWith("/scheme/discrepancies/d1", StringComparison.Ordinal))
            {
                return Json(Detail);
            }

            if (path.EndsWith("/scheme/questions", StringComparison.Ordinal))
            {
                return Json(Questions);
            }

            return new HttpResponseMessage(HttpStatusCode.NotFound)
            {
                Content = new StringContent("""{"code":"not_found","message":"no"}""",
                    Encoding.UTF8, "application/json"),
            };
        }

        /// <summary>The rows at <c>/scheme/discrepancies</c>: the open set when the
        /// caller asked for it, the whole audit table when it did not.</summary>
        private string OpenList(string url, bool closes)
        {
            var openOnly = url.Contains("review_status=AWAITING_REVIEW", StringComparison.Ordinal);
            var openRow = ListRow("d1", "AWAITING_REVIEW", "POSSIBLE_DISCREPANCY");
            var second = ListRow("d2", "AWAITING_REVIEW", "POSSIBLE_DISCREPANCY");
            var settled = ListRow("d1", "REVIEW_COMPLETED", "HUMAN_REVIEWED");

            if (OpenRows == 1)
            {
                return _decided && closes
                    ? """{"items":[],"total":0}"""
                    : $"{{\"items\":[{openRow}],\"total\":1}}";
            }

            if (!_decided || !closes)
            {
                return $"{{\"items\":[{openRow},{second}],\"total\":2}}";
            }

            // Decided: the row is gone from the open set. The unfiltered listing
            // still carries it, which is what the page must not be asking for.
            return openOnly
                ? $"{{\"items\":[{second}],\"total\":1}}"
                : $"{{\"items\":[{settled},{second}],\"total\":2}}";
        }

        private static string ListRow(string id, string status, string state) =>
            $"{{\"id\":\"{id}\",\"question_number\":\"12\",\"coursework_id\":\"cw-1\","
            + $"\"assignment_title\":\"Ch2L3 - Simplifying Expressions\","
            + $"\"student_name\":\"Joudy Ehab\",\"review_status\":\"{status}\","
            + $"\"state\":\"{state}\",\"scheme_page\":4,\"ignored\":false}}";

        private static HttpResponseMessage Json(string json) =>
            new(HttpStatusCode.OK)
            {
                Content = new StringContent(json, Encoding.UTF8, "application/json"),
            };
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
}
