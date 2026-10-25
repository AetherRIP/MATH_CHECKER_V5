using System.Net;
using System.Text;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The Auto-correct page must show the backend's own counts, refuse to start
/// while the backend's gate is closed, and report what was really queued.
/// The page is not allowed to compute a correction count of its own.
/// </summary>
public class AutoCorrectPageTests
{
    private const string AssignmentsBody = """
        {"assignments":[
          {"id":"cw-1","course_id":"c1","title":"Ch2L3 - Simplifying Expressions",
           "course_name":"WBIS - O Level Mathematics","course_section":"Set 1",
           "due_date":"2026-10-16 23:59","students_total":78,"submitted":9,"checked":5,
           "processing":1,"failed":0,"has_mark_scheme":true,"allowlisted":1},
          {"id":"cw-2","course_id":"c1","title":"Ch1L9 - Ratio",
           "course_name":"WBIS - O Level Mathematics","course_section":"Set 1",
           "students_total":78,"submitted":0,"checked":0,"processing":0,"failed":0,
           "has_mark_scheme":false,"allowlisted":1},
          {"id":"local-1","title":"My local paper","local":true,"students_total":0},
          {"id":"cw-3","course_id":"c1","title":"Deleted in Classroom","deleted_in_classroom":true,
           "students_total":3,"submitted":1,"has_mark_scheme":true,"allowlisted":1}
        ],"total":4}
        """;

    private const string JobsBody = """
        {"jobs":[
          {"id":"j1","status":"QUEUED","student_name":"Ada Lovelace"},
          {"id":"j2","status":"MARKING","student_name":"Ben Bray"},
          {"id":"j3","status":"COMPLETED","student_name":"Cleo Chan"},
          {"id":"j4","status":"FAILED","student_name":"Dev Doe"}
        ]}
        """;

    private const string StatusBody = """
        {"enabled":true,"worker_enabled":true,"interval_minutes":10,
         "review_mode":true,"draft_only":false,"attach_report":true,
         "submit_grade":true,"return_work":true,"report_detail":"detailed",
         "intervals":[5,10,15,30],"pending_jobs":0,"active_jobs":1,
         "last_check_text":"09:15","next_check_text":"09:25",
         "assignments_enabled":2,"counts":{}}
        """;

    private const string AutomationJobsBody = """
        {"jobs":[{"id":"a1","name":"Ada Lovelace","status":"WAITING_FOR_REVIEW",
                  "status_label":"Waiting for review","pipeline_label":"Marking","stage_index":3,
                  "created_text":"09:10"}]}
        """;

    private const string LogBody = """{"entries":[{"message":"Scan finished","level":"info"}]}""";

    private const string PreviewBody = """
        {"coursework_id":"cw-1","course_id":"c1",
         "assignment_title":"Ch2L3 - Simplifying Expressions",
         "counts":{"students":78,"submitted":9,"already_corrected":5,"needs_review":0,
                   "not_corrected":2,"processing":1,"invalid":1,"missing":68,"draft":0,"unknown":0},
         "eligible":[{"submission_id":"s1","student_id":"u1","student_name":"Ada Lovelace"},
                     {"submission_id":"s2","student_id":"u2","student_name":"Ben Bray"}],
         "already_corrected":[],"needs_review":[],
         "will_correct":2,"can_start":true,"blockers":[],
         "has_mark_scheme":true,"allowlisted":true,"sync_warning":null}
        """;

    private const string BlockedPreviewBody = """
        {"coursework_id":"cw-1","course_id":"c1","assignment_title":"Ch2L3",
         "counts":{"students":78,"submitted":9,"already_corrected":0,"needs_review":0,
                   "not_corrected":8,"processing":0,"invalid":0,"missing":69,"draft":1,"unknown":0},
         "eligible":[{"submission_id":"s1","student_name":"Ada Lovelace"}],
         "will_correct":1,"can_start":false,
         "blockers":["This assignment is not allowlisted.","No mark scheme has been uploaded for this assignment."],
         "has_mark_scheme":false,"allowlisted":false,"sync_warning":null}
        """;

    private const string EmptyPreviewBody = """
        {"coursework_id":"cw-1","course_id":"c1","assignment_title":"Ch2L3",
         "counts":{"students":78,"submitted":9,"already_corrected":9,"needs_review":0,
                   "not_corrected":0,"processing":0,"invalid":0,"missing":69,"draft":0,"unknown":0},
         "eligible":[],"will_correct":0,"can_start":true,"blockers":[],
         "has_mark_scheme":true,"allowlisted":true,"sync_warning":null}
        """;

    private const string CorrectAllBody = """
        {"enqueued":2,"jobs":["j9","j10"],"duplicates":0,"already_corrected":[],
         "needs_review":[],"processing":[],"skipped_missing":["x"],"skipped_draft":[],
         "invalid":[],"unknown":[],"enqueue_failed":[],"total_students":78,
         "sync_warning":null,"refreshed":true,"recheck":false}
        """;

    [Fact]
    public async Task Only_classroom_assignments_are_listed_with_the_backend_counts()
    {
        var handler = new RouteHandler();
        using (handler)
        {
            var (session, _) = await SessionAsync(handler);
            var page = new AutomationViewModel(session);

            await page.LoadAsync();

            Assert.Null(page.Error);
            Assert.Equal(2, page.Items.Count);
            Assert.DoesNotContain(page.Items, a => a.Id == "local-1");
            Assert.DoesNotContain(page.Items, a => a.Id == "cw-3");

            var ratio = page.Items.Single(a => a.Id == "cw-1");
            Assert.Equal("Ch2L3 - Simplifying Expressions", ratio.Title);
            Assert.Equal("78 students", ratio.RosterText);
            Assert.Equal("9 handed in", ratio.SubmittedText);
            // await = submitted - corrected - processing (all backend numbers)
            Assert.Equal("3 to correct", ratio.AwaitingText);
            Assert.Equal("1 in progress", ratio.ProcessingText);
            Assert.Equal("5 corrected", ratio.CorrectedText);
            Assert.True(ratio.IsReady);
            Assert.Contains("papers ready to correct", ratio.ReadyText);
        }
    }

    [Fact]
    public async Task Selecting_an_assignment_shows_the_backend_preview_before_anything_starts()
    {
        var handler = new RouteHandler();
        using (handler)
        {
            var (session, _) = await SessionAsync(handler);
            var page = new AutomationViewModel(session);
            await page.LoadAsync();

            page.Selected = page.Items.Single(a => a.Id == "cw-1");
            await WaitUntilAsync(() => page.HasPreview);

            Assert.True(page.HasPreview);
            Assert.Equal(2, page.PreviewPapers.Count);
            Assert.Contains("Ada Lovelace", page.PreviewPapers);
            Assert.Contains("2 of 9", page.PreviewSummary);
            Assert.False(page.HasBlockers);
            Assert.True(page.CanStart);
            Assert.Equal("Start correcting 2 papers", page.StartText);
            // Nothing was queued by merely looking.
            Assert.DoesNotContain(handler.Requests, r => r.Url.Contains("correct-all") && !r.Url.Contains("preview"));
        }
    }

    [Fact]
    public async Task A_blocked_assignment_explains_where_to_fix_it_and_cannot_start()
    {
        var handler = new RouteHandler { Preview = BlockedPreviewBody };
        using (handler)
        {
            var (session, _) = await SessionAsync(handler);
            var page = new AutomationViewModel(session);
            await page.LoadAsync();

            page.Selected = page.Items.Single(a => a.Id == "cw-1");
            await WaitUntilAsync(() => page.HasPreview);

            Assert.True(page.HasBlockers);
            Assert.Contains(page.Blockers, b => b.Contains("not allowlisted", StringComparison.OrdinalIgnoreCase));
            Assert.Contains(page.Blockers, b => b.Contains("Allow-list assignment"));
            Assert.Contains(page.Blockers, b => b.Contains("mark scheme"));
            Assert.False(page.CanStart);
        }
    }

    [Fact]
    public async Task Nothing_to_correct_offers_no_start_button()
    {
        var handler = new RouteHandler { Preview = EmptyPreviewBody };
        using (handler)
        {
            var (session, _) = await SessionAsync(handler);
            var page = new AutomationViewModel(session);
            await page.LoadAsync();

            page.Selected = page.Items.Single(a => a.Id == "cw-1");
            await WaitUntilAsync(() => page.HasPreview);

            Assert.False(page.CanStart);
            Assert.Equal("Start correcting", page.StartText);
            Assert.Contains("already corrected", page.PreviewSummary);
        }
    }

    [Fact]
    public async Task Starting_posts_the_real_correction_and_reports_what_was_queued()
    {
        var handler = new RouteHandler();
        using (handler)
        {
            var (session, _) = await SessionAsync(handler);
            var dialogs = (ConfirmingDialogs)session.Dialogs;
            var page = new AutomationViewModel(session);
            await page.LoadAsync();

            page.Selected = page.Items.Single(a => a.Id == "cw-1");
            await WaitUntilAsync(() => page.HasPreview);

            page.StartCommand.Execute(null);
            await WaitUntilAsync(() => dialogs.Asked.Count > 0);
            await WaitUntilAsync(() => page.HasResult);

            // One confirmation, and it names the assignment and the real count.
            var question = Assert.Single(dialogs.Asked);
            Assert.Contains("Ch2L3 - Simplifying Expressions", question);
            Assert.Contains("Papers to correct: 2", question);
            Assert.Contains("never stops the rest", question);

            var post = handler.Requests.Last(r => r.Method == "POST");
            Assert.Contains("classroom/courses/c1/assignments/cw-1/correct-all", post.Url);
            Assert.Contains("\"refresh\":true", post.Body ?? string.Empty);

            // The result repeats the backend's numbers: two queued, and the
            // papers it deliberately skipped are named as skipped.
            Assert.Contains("Queued 2 papers.", page.ResultText);
            Assert.Contains("skipped because nothing was handed in", page.ResultText);
            Assert.Contains("1 waiting", page.QueueSummary);
        }
    }

    [Fact]
    public async Task The_background_scheduler_settings_survive_in_advanced_settings()
    {
        var handler = new RouteHandler();
        using (handler)
        {
            var (session, _) = await SessionAsync(handler);
            var page = new AutomationViewModel(session);

            await page.LoadAsync();

            Assert.True(page.Enabled);
            Assert.True(page.ReviewMode);
            Assert.Equal(10, page.IntervalMinutes);
            Assert.Equal("detailed", page.ReportDetail);
            Assert.Contains("checks every 10 min", page.WorkerText);
            Assert.Single(page.AutomationJobs);
            Assert.True(page.HasAutomationJobs);
            Assert.Contains(page.LogLines, line => line.Contains("Scan finished"));
        }
    }

    [Fact]
    public async Task Searching_and_filtering_never_touch_the_queue_or_the_records()
    {
        var handler = new RouteHandler();
        using (handler)
        {
            var (session, _) = await SessionAsync(handler);
            var page = new AutomationViewModel(session);
            await page.LoadAsync();

            page.Search = "ratio";
            Assert.Single(page.Items);
            Assert.Equal("Ch1L9 - Ratio", page.Items[0].Title);

            page.Search = string.Empty;
            Assert.Equal(2, page.Items.Count);

            // cw-2 has no mark scheme: the backend gate is what disables the
            // button, and the card says why.
            var ratio2 = page.Items.Single(a => a.Id == "cw-2");
            Assert.False(ratio2.IsReady);
            Assert.Contains("mark scheme", ratio2.ReadyText);

            page.ClassFilter = null!;              // a rebuilt combo writes null
            Assert.Equal("All classes", page.ClassFilter);
            Assert.Equal(2, page.Items.Count);
        }
    }

    private static async Task<(AppSession Session, RouteHandler Handler)> SessionAsync(RouteHandler handler)
    {
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        return (new AppSession(settings, new MemoryLog(), host, api, new ConfirmingDialogs()), handler);
    }

    private static async Task WaitUntilAsync(Func<bool> condition, int timeoutMs = 5000)
    {
        var started = Environment.TickCount64;
        while (!condition())
        {
            if (Environment.TickCount64 - started > timeoutMs)
            {
                throw new TimeoutException("the page never reached the expected state");
            }

            await Task.Delay(20);
        }
    }

    /// <summary>Answers by path so an extra poll cannot consume the wrong
    /// queued response — the page polls jobs and status independently.</summary>
    private sealed class RouteHandler : HttpMessageHandler
    {
        public string Preview { get; set; } = PreviewBody;

        public List<RecordedRequest> Requests { get; } = new();

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var body = request.Content is null
                ? null
                : await request.Content.ReadAsStringAsync(cancellationToken);
            var url = request.RequestUri!.AbsoluteUri;
            Requests.Add(new RecordedRequest(request.Method.Method, url, body,
                request.Headers.ToDictionary(h => h.Key, h => string.Join(",", h.Value),
                    StringComparer.OrdinalIgnoreCase)));

            var path = request.RequestUri.AbsolutePath;
            var json =
                path.EndsWith("/correct-all/preview", StringComparison.Ordinal) ? Preview
                : path.EndsWith("/correct-all", StringComparison.Ordinal) ? CorrectAllBody
                : path.EndsWith("/automation/status", StringComparison.Ordinal) ? StatusBody
                : path.EndsWith("/automation/jobs", StringComparison.Ordinal) ? AutomationJobsBody
                : path.EndsWith("/automation/log", StringComparison.Ordinal) ? LogBody
                : path.EndsWith("/assignments", StringComparison.Ordinal) ? AssignmentsBody
                : path.EndsWith("/jobs", StringComparison.Ordinal) ? JobsBody
                : """{"ok":true}""";

            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(json, Encoding.UTF8, "application/json"),
            };
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

    private sealed class ConfirmingDialogs : IDialogService
    {
        public List<string> Asked { get; } = new();

        public bool Confirm(string title, string message)
        {
            Asked.Add(message);
            return true;
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
}
