using System.Net;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Core.Sync;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The People page's Sync now used to start the backend job, wait 1.5 s, read
/// ONE progress snapshot and reload — so the page always showed "Connecting…"
/// and the rows it had just fetched predated the sync. These tests pin the
/// replacement: wait for the backend's own final state, report it honestly,
/// never queue a second job, and retry only what failed.
/// </summary>
public class ClassroomSyncTests
{
    private const string StartRunning = """
        {"started":true,"snapshot":{"id":"full-1","kind":"full","target":{},"running":true,
         "stage":"connect","message":"Connecting…","progress":0.05,"error":null,"summary":null,
         "events":[{"type":"stage","stage":"connect","message":"Connecting…"}]}}
        """;

    private const string FinalCompleted = """
        {"id":"full-1","kind":"full","target":{},"running":false,"stage":"save",
         "message":"Sync complete","progress":1.0,"started_at":1700000000.0,
         "finished_at":1700000084.0,"elapsed_s":84.0,"error":null,
         "summary":{"courses":2,"assignments":5,"students":40,"submissions":300,
                    "errors":[],"unavailable":[],"partial":false,"synced_at":1700000084.0,
                    "elapsed_s":84.2}}
        """;

    private const string MidRun = """
        {"id":"full-1","kind":"full","target":{},"running":true,"stage":"submissions",
         "message":"Fetching submissions — Algebra Homework 5 (3/18)…","progress":0.62,
         "error":null,"summary":null}
        """;

    [Fact]
    public async Task Waits_for_the_backend_to_finish_and_reports_its_final_summary()
    {
        var (session, handler) = await SessionAsync(
            new StubHttpHandler()
                .Respond(HttpStatusCode.OK, StartRunning)
                .Respond(HttpStatusCode.OK, MidRun)
                .Respond(HttpStatusCode.OK, FinalCompleted));

        using (handler)
        {
            session.Sync.PollInterval = TimeSpan.Zero;

            var result = await session.Sync.SyncAsync();

            Assert.NotNull(result);
            Assert.Equal(SyncOutcome.Completed, result!.Outcome);
            Assert.Contains("2 courses", result.Counts);
            Assert.Contains("40 students", result.Counts);
            Assert.Contains("300 submissions", result.Counts);
            Assert.Equal("Sync complete in 84 s · 2 courses · 5 assignments · 40 students · 300 submissions",
                session.Sync.StatusText);
            Assert.False(session.Sync.IsRunning);
            Assert.False(session.Sync.CanRetry);

            // 1 POST to start + 2 polls: it did NOT stop at the first snapshot.
            Assert.Equal(3, handler.Requests.Count);
            Assert.Equal("POST", handler.Requests[0].Method);
            Assert.EndsWith("/api/classroom/sync/start", handler.Requests[0].Url);
            Assert.EndsWith("/api/classroom/sync/progress", handler.Requests[2].Url);
        }
    }

    [Fact]
    public async Task A_partial_run_is_not_reported_as_success()
    {
        const string partial = """
            {"id":"full-2","kind":"full","target":{},"running":false,"stage":"save",
             "message":"Sync complete","progress":1.0,"error":null,
             "summary":{"courses":2,"assignments":2,"students":6,"submissions":1,
                        "errors":[{"part":"submissions (Forces Homework)",
                                   "detail":"backend quota exceeded",
                                   "course_id":"c2","coursework_id":"w9"}],
                        "unavailable":[],"partial":true,"elapsed_s":9.5}}
            """;
        const string start = """
            {"started":true,"snapshot":{"id":"full-2","running":true,"progress":0.05,
             "message":"Connecting…","error":null,"summary":null}}
            """;

        var (session, handler) = await SessionAsync(
            new StubHttpHandler()
                .Respond(HttpStatusCode.OK, start)
                .Respond(HttpStatusCode.OK, partial));

        using (handler)
        {
            session.Sync.PollInterval = TimeSpan.Zero;

            var result = await session.Sync.SyncAsync();

            Assert.NotNull(result);
            Assert.Equal(SyncOutcome.Partial, result!.Outcome);
            Assert.True(session.Sync.IsPartial);
            Assert.True(session.Sync.CanRetry);
            Assert.Contains("1 problem", session.Sync.StatusText);
            var problem = Assert.Single(result.Problems);
            Assert.True(problem.Retryable);
            Assert.Equal("w9", problem.CourseworkId);
            Assert.Contains("Forces Homework", session.Sync.DetailText);
        }
    }

    [Fact]
    public async Task Two_rapid_clicks_share_one_run_and_one_job()
    {
        var (session, handler) = await SessionAsync(
            new StubHttpHandler()
                .Respond(HttpStatusCode.OK, StartRunning)
                .Respond(HttpStatusCode.OK, FinalCompleted));

        using (handler)
        {
            // A real poll interval, so the first run is genuinely in flight
            // while the second click arrives (the stub alone would finish the
            // whole run inside the first call).
            session.Sync.PollInterval = TimeSpan.FromMilliseconds(40);

            var first = session.Sync.SyncAsync();
            var second = session.Sync.SyncAsync();

            Assert.False(first.IsCompleted);
            Assert.Same(first, second);
            var result = await first;
            await second;
            Assert.Equal(SyncOutcome.Completed, result!.Outcome);

            var posts = handler.Requests.Count(r => r.Method == "POST");
            Assert.Equal(1, posts);
        }
    }

    [Fact]
    public async Task Retry_re_syncs_only_the_failed_assignment()
    {
        const string partial = """
            {"id":"full-3","running":false,"progress":1.0,"message":"Sync complete",
             "error":null,
             "summary":{"courses":2,"assignments":2,"students":6,"submissions":1,
                        "errors":[{"part":"submissions (Forces Homework)",
                                   "detail":"backend quota exceeded",
                                   "course_id":"c2","coursework_id":"w9"}],
                        "unavailable":[],"partial":true,"elapsed_s":9.5}}
            """;
        const string startRetry = """
            {"started":true,"snapshot":{"id":"assignment-9","kind":"assignment",
             "target":{"course_id":"c2","coursework_id":"w9"},"running":true,
             "stage":"roster","message":"Fetching class roster…","progress":0.10,
             "error":null,"summary":null}}
            """;
        const string retryDone = """
            {"id":"assignment-9","kind":"assignment","running":false,"progress":1.0,
             "message":"Sync complete","error":null,
             "summary":{"course_id":"c2","coursework_id":"w9","submissions":12,
                        "roster":30,"transitions":0,"synced_at":1700000200.0}}
            """;

        var (session, handler) = await SessionAsync(
            new StubHttpHandler()
                .Respond(HttpStatusCode.OK, """{"started":true,"snapshot":{"id":"full-3","running":true,"progress":0.05,"message":"Connecting…","error":null,"summary":null}}""")
                .Respond(HttpStatusCode.OK, partial)
                .Respond(HttpStatusCode.OK, startRetry)
                .Respond(HttpStatusCode.OK, retryDone));

        using (handler)
        {
            session.Sync.PollInterval = TimeSpan.Zero;

            await session.Sync.SyncAsync();
            Assert.True(session.Sync.CanRetry);

            var retry = await session.Sync.RetryFailedAsync();

            Assert.NotNull(retry);
            Assert.Equal(SyncOutcome.Completed, retry!.Outcome);
            Assert.Equal("Re-synced 1 failed part", retry.Headline);
            Assert.True(retry.RetryRun);

            var retryStart = handler.Requests[2];
            Assert.Equal("POST", retryStart.Method);
            Assert.Contains("course_id=c2", retryStart.Url);
            Assert.Contains("coursework_id=w9", retryStart.Url);
            Assert.Equal(4, handler.Requests.Count);   // no second full sync
        }
    }

    [Fact]
    public async Task A_refused_start_is_an_honest_failure()
    {
        var (session, handler) = await SessionAsync(
            new StubHttpHandler().Respond(HttpStatusCode.BadRequest,
                """{"code":"classroom_not_connected","message":"Connect Google Classroom first."}"""));

        using (handler)
        {
            var result = await session.Sync.SyncAsync();

            Assert.NotNull(result);
            Assert.Equal(SyncOutcome.Failed, result!.Outcome);
            Assert.True(session.Sync.IsFailed);
            Assert.Contains("Connect Google Classroom", result.Error);
            Assert.Contains("Sync failed", session.Sync.StatusText);
        }
    }

    [Fact]
    public async Task Sync_pages_share_one_service_and_other_pages_have_none()
    {
        var (session, handler) = await SessionAsync(
            new StubHttpHandler().Respond(HttpStatusCode.OK, """{"students":[]}"""));

        using (handler)
        {
            var students = new StudentsViewModel(session);
            var classes = new ClassesViewModel(session);
            var assignments = new AssignmentsViewModel(session);
            var dashboard = new DashboardViewModel(session);

            Assert.True(students.SyncsFromClassroom);
            Assert.True(classes.SyncsFromClassroom);
            Assert.True(assignments.SyncsFromClassroom);
            Assert.True(dashboard.SyncsFromClassroom);
            Assert.Same(session.Sync, students.SyncService);
            Assert.Same(session.Sync, classes.SyncService);
            Assert.Same(session.Sync, assignments.SyncService);
            Assert.Same(session.Sync, dashboard.SyncService);

            // A page with nothing synchronized from Classroom shows no button.
            var settings = new SettingsViewModel(session);
            Assert.False(settings.SyncsFromClassroom);
            Assert.Null(settings.SyncService);
        }
    }

    [Fact]
    public async Task Completion_is_announced_once_with_the_verified_result()
    {
        var (session, handler) = await SessionAsync(
            new StubHttpHandler()
                .Respond(HttpStatusCode.OK, StartRunning)
                .Respond(HttpStatusCode.OK, FinalCompleted));

        using (handler)
        {
            session.Sync.PollInterval = TimeSpan.Zero;
            var seen = new List<SyncResult>();
            session.Sync.Completed += seen.Add;

            await session.Sync.SyncAsync();

            var result = Assert.Single(seen);
            Assert.Equal(SyncOutcome.Completed, result.Outcome);
        }
    }

    private static async Task<(AppSession Session, StubHttpHandler Handler)> SessionAsync(
        StubHttpHandler handler)
    {
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        return (new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs()), handler);
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
