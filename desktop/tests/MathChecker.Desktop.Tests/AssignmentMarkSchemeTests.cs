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
/// The mark scheme used to live on a separate Classroom page. It now belongs to
/// the assignment it marks, so these pin what the assignment page reads from the
/// backend: attached or not, which file, how many pages, how many marks, whether
/// the scheme has been verified — and, just as important, whether a
/// whole-assignment correction is allowed to start at all.
/// </summary>
public class AssignmentMarkSchemeTests
{
    private const string AttachedScheme = """
        {"coursework_id":"a1","assignment_title":"Algebra Homework 5","has_mark_scheme":true,
         "file_name":"june-2017-paper-2-ms.pdf","pages":26,"processed":true,"total_marks":75,
         "subquestion_count":31,"verification_state":"VERIFIED","warnings":[]}
        """;

    private const string UnprocessedScheme = """
        {"coursework_id":"a1","assignment_title":"Algebra Homework 5","has_mark_scheme":true,
         "file_name":"untouched.pdf","pages":12,"processed":false,
         "verification_state":"NOT_VERIFIED","warnings":[]}
        """;

    private const string NoScheme = """
        {"coursework_id":"a1","assignment_title":"Algebra Homework 5","has_mark_scheme":false,
         "file_name":null,"pages":null,"processed":false,
         "warnings":["No mark scheme has been uploaded for this assignment."]}
        """;

    [Fact]
    public async Task An_attached_scheme_names_the_file_pages_and_marks()
    {
        using var handler = new AssignmentHandler(AttachedScheme);
        var session = await SessionAsync(handler);
        var page = await PageWithAssignmentAsync(session, "a1");

        Assert.True(page.SchemeAttached);
        Assert.False(page.SchemeMissing);
        Assert.Equal("Attached · verified", page.SchemeTitle);
        Assert.Contains("june-2017-paper-2-ms.pdf", page.SchemeDetail);
        Assert.Contains("26 pages", page.SchemeDetail);
        Assert.Contains("75 marks", page.SchemeDetail);
        Assert.Contains("31 marking points", page.SchemeDetail);
        Assert.Equal("Replace mark scheme…", page.UploadSchemeLabel);
        Assert.True(page.CanCorrectAll);
    }

    [Fact]
    public async Task A_missing_scheme_is_stated_before_the_teacher_uploads_one()
    {
        using var handler = new AssignmentHandler(NoScheme);
        var session = await SessionAsync(handler);
        var page = await PageWithAssignmentAsync(session, "a1");

        Assert.False(page.SchemeAttached);
        Assert.True(page.SchemeMissing);
        Assert.Equal("Not attached", page.SchemeTitle);
        Assert.Contains("cannot start", page.SchemeDetail);
        Assert.Equal("Upload mark scheme…", page.UploadSchemeLabel);

        // No scheme means no whole-assignment run: the button is not offered as
        // a dead end that only fails once pressed.
        Assert.False(page.CanCorrectAll);
    }

    [Fact]
    public async Task An_attached_but_unread_scheme_says_it_has_not_been_analysed_yet()
    {
        using var handler = new AssignmentHandler(UnprocessedScheme);
        var session = await SessionAsync(handler);
        var page = await PageWithAssignmentAsync(session, "a1");

        Assert.True(page.SchemeAttached);
        Assert.Equal("Attached · not analysed yet", page.SchemeTitle);
        Assert.Contains("untouched.pdf", page.SchemeDetail);
        Assert.Contains("first time work is corrected", page.SchemeDetail);
    }

    [Fact]
    public async Task The_allowlist_state_comes_from_the_backend_not_from_the_scheme()
    {
        using var handler = new AssignmentHandler(AttachedScheme);
        var session = await SessionAsync(handler);

        var allowlisted = await PageWithAssignmentAsync(session, "a1");
        Assert.True(allowlisted.Allowlisted);
        Assert.False(allowlisted.CanAllowlist);
        Assert.Contains("Allow-listed", allowlisted.AllowlistText);

        var blocked = await PageWithAssignmentAsync(session, "a2");
        Assert.False(blocked.Allowlisted);
        Assert.True(blocked.CanAllowlist);
        Assert.Contains("Not allow-listed", blocked.AllowlistText);
    }

    [Fact]
    public async Task Moving_to_another_assignment_replaces_the_previous_scheme()
    {
        using var handler = new AssignmentHandler(AttachedScheme);
        var session = await SessionAsync(handler);

        var first = await PageWithAssignmentAsync(session, "a1");
        Assert.Contains("june-2017-paper-2-ms.pdf", first.SchemeDetail);

        // A different assignment must not keep the previous file, pages or
        // allow-list state: that is how the old shared page mixed two
        // assignments up.
        var second = await PageWithAssignmentAsync(session, "a2");
        Assert.Contains("a2-ms.pdf", second.SchemeDetail);
        Assert.DoesNotContain("june-2017-paper-2-ms.pdf", second.SchemeDetail);
        Assert.False(second.Allowlisted);
    }

    private static async Task<AssignmentDetailViewModel> PageWithAssignmentAsync(
        AppSession session, string courseworkId)
    {
        session.OpenAssignment(courseworkId);
        var page = new AssignmentDetailViewModel(session);
        await page.LoadAsync();
        Assert.Null(page.Error);
        await WaitAsync(() => !page.SchemeChecking, "the mark scheme to be read");
        return page;
    }

    private static async Task WaitAsync(Func<bool> done, string what)
    {
        for (var attempt = 0; attempt < 150; attempt++)
        {
            if (done())
            {
                return;
            }

            await Task.Delay(20);
        }

        throw new TimeoutException("Timed out waiting for " + what + ".");
    }

    private static async Task<AppSession> SessionAsync(AssignmentHandler handler)
    {
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        return new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs());
    }

    /// <summary>Answers the assignment page's routes by URL, because the page
    /// fires a roster read and then a scheme read.</summary>
    private sealed class AssignmentHandler : HttpMessageHandler
    {
        public AssignmentHandler(string scheme)
        {
            SchemeBody = scheme;
        }

        public string SchemeBody { get; }

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;
            var isA1 = path.Contains("/assignments/a1/", StringComparison.Ordinal);

            if (path.EndsWith("/students", StringComparison.Ordinal))
            {
                return Task.FromResult(Json(StudentsBody(isA1 ? "a1" : "a2")));
            }

            if (path.EndsWith("/mark-scheme", StringComparison.Ordinal))
            {
                if (!isA1)
                {
                    return Task.FromResult(Json("""
                        {"coursework_id":"a2","assignment_title":"Trigonometry Test",
                         "has_mark_scheme":true,"file_name":"a2-ms.pdf","pages":8,
                         "processed":true,"total_marks":50,"subquestion_count":12,
                         "verification_state":"VERIFIED","warnings":[]}
                        """));
                }

                return Task.FromResult(Json(SchemeBody));
            }

            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound)
            {
                Content = new StringContent("{\"code\":\"unexpected\",\"message\":\"" + path + "\"}",
                    Encoding.UTF8, "application/json"),
            });
        }

        private static string StudentsBody(string courseworkId)
        {
            var a1 = courseworkId == "a1";
            var title = a1 ? "Algebra Homework 5" : "Trigonometry Test";
            var allowlisted = a1 ? 1 : 0;
            return $$"""
                {"assignment":{"id":"{{courseworkId}}","course_id":"c1","title":"{{title}}",
                   "max_points":75,"allowlisted":{{allowlisted}},"has_mark_scheme":true,
                   "course_name":"10A Maths","course_section":"Set 1"},
                 "students":[],
                 "summary":{"expected":0,"completed":0,"not_completed":0,"missing":0,
                            "unknown":0,"submitted":0,"corrected":0},
                 "sync":{"last_sync":1700000000.0,"roster_known":true},
                 "warnings":[]}
                """;
        }

        private static HttpResponseMessage Json(string body) =>
            new(HttpStatusCode.OK)
            {
                Content = new StringContent(body, Encoding.UTF8, "application/json"),
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
