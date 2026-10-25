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
/// The Classroom card offered only an "Upload mark scheme…" button, so a
/// teacher could not tell an assignment that already had a scheme from one that
/// would refuse every correction with "no mark scheme". These pin the state the
/// card now reads from the backend: attached or not, which file, how many
/// pages, how many marks, and whether the scheme has been verified.
/// </summary>
public class ClassroomMarkSchemeTests
{
    private const string StatusBody = """
        {"connected":true,"email":"teacher@demo.invalid","client_configured":true,
         "write_draft_grades":false,"last_sync":1700000000.0,"connected_at":1699990000.0}
        """;

    private const string CoursesBody = """
        {"courses":[{"id":"c1","name":"10A Maths","section":"Set 1"}],"total":1}
        """;

    private const string AssignmentsBody = """
        {"assignments":[
          {"id":"a1","title":"Algebra Homework 5","max_points":75,"state":"PUBLISHED","due_date":"2026-10-30"},
          {"id":"a2","title":"Trigonometry Test","max_points":50,"state":"PUBLISHED","due_date":"2026-11-06"}
        ]}
        """;

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
        var handler = new ClassroomHandler(AttachedScheme);
        var (session, _) = await SessionAsync(handler);
        using (handler)
        {
            var page = await PageWithAssignmentAsync(session);

            Assert.True(page.SchemeAttached);
            Assert.False(page.SchemeMissing);
            Assert.Equal("Attached · verified", page.SchemeTitle);
            Assert.Contains("june-2017-paper-2-ms.pdf", page.SchemeDetail);
            Assert.Contains("26 pages", page.SchemeDetail);
            Assert.Contains("75 marks", page.SchemeDetail);
            Assert.Contains("31 marking points", page.SchemeDetail);
            Assert.Equal("Replace mark scheme…", page.UploadSchemeLabel);
        }
    }

    [Fact]
    public async Task A_missing_scheme_is_stated_before_the_teacher_uploads_one()
    {
        var handler = new ClassroomHandler(NoScheme);
        var (session, _) = await SessionAsync(handler);
        using (handler)
        {
            var page = await PageWithAssignmentAsync(session);

            Assert.False(page.SchemeAttached);
            Assert.True(page.SchemeMissing);
            Assert.Equal("Not attached", page.SchemeTitle);
            Assert.Contains("cannot start", page.SchemeDetail);
            Assert.Equal("Upload mark scheme…", page.UploadSchemeLabel);
        }
    }

    [Fact]
    public async Task An_attached_but_unread_scheme_says_it_has_not_been_analysed_yet()
    {
        var handler = new ClassroomHandler(UnprocessedScheme);
        var (session, _) = await SessionAsync(handler);
        using (handler)
        {
            var page = await PageWithAssignmentAsync(session);

            Assert.True(page.SchemeAttached);
            Assert.Equal("Attached · not analysed yet", page.SchemeTitle);
            Assert.Contains("untouched.pdf", page.SchemeDetail);
            Assert.Contains("first time work is corrected", page.SchemeDetail);
        }
    }

    [Fact]
    public async Task A_late_answer_for_the_previous_assignment_does_not_paint_the_new_one()
    {
        var handler = new ClassroomHandler(NoScheme) { SchemeGate = new TaskCompletionSource() };
        var (session, _) = await SessionAsync(handler);
        using (handler)
        {
            var page = await PageWithoutSchemeWaitAsync(session);

            // a1's scheme read is still held open when the teacher moves on.
            page.SelectedAssignment = page.Assignments[0];
            await WaitAsync(() => page.SchemeChecking, "the first assignment's scheme read");

            page.SelectedAssignment = page.Assignments[1];
            await WaitAsync(() => page.SchemeDetail.Contains("a2-ms.pdf", StringComparison.Ordinal),
                "the second assignment's scheme");

            handler.SchemeGate!.SetResult();
            await Task.Delay(120);

            Assert.Contains("a2-ms.pdf", page.SchemeDetail);
            Assert.DoesNotContain("Algebra Homework 5", page.SchemeTitle);
            Assert.True(page.SchemeAttached);
        }
    }

    private static async Task<ClassroomViewModel> PageWithAssignmentAsync(AppSession session)
    {
        var page = await PageWithoutSchemeWaitAsync(session);
        page.SelectedAssignment = page.Assignments[0];
        await WaitAsync(() => !page.SchemeChecking, "the mark scheme to be read");
        return page;
    }

    private static async Task<ClassroomViewModel> PageWithoutSchemeWaitAsync(AppSession session)
    {
        var page = new ClassroomViewModel(session);
        await page.LoadAsync();
        Assert.Null(page.Error);

        page.SelectedCourse = page.Courses[0];
        await WaitAsync(() => page.Assignments.Count == 2, "the assignment list");
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

    private static async Task<(AppSession Session, ClassroomHandler Handler)> SessionAsync(
        ClassroomHandler handler)
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

    /// <summary>Answers the Classroom page's routes by URL, because the page
    /// fires several reads (and a scheme read per selection) and call-order
    /// stubs would drift.</summary>
    private sealed class ClassroomHandler : HttpMessageHandler
    {
        public ClassroomHandler(string scheme)
        {
            SchemeBody = scheme;
        }

        public string SchemeBody { get; set; }

        public TaskCompletionSource? SchemeGate { get; set; }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;
            if (path.EndsWith("/classroom/status", StringComparison.Ordinal))
            {
                return Json(StatusBody);
            }

            if (path.EndsWith("/classroom/sync/progress", StringComparison.Ordinal))
            {
                return Json("""{"running":false,"summary":null}""");
            }

            if (path.EndsWith("/classroom/courses", StringComparison.Ordinal))
            {
                return Json(CoursesBody);
            }

            if (path.EndsWith("/assignments", StringComparison.Ordinal)
                && path.Contains("/classroom/courses/", StringComparison.Ordinal))
            {
                return Json(AssignmentsBody);
            }

            if (path.EndsWith("/submissions", StringComparison.Ordinal))
            {
                return Json("""{"submissions":[]}""");
            }

            if (path.EndsWith("/mark-scheme", StringComparison.Ordinal))
            {
                var isA1 = path.Contains("/assignments/a1/", StringComparison.Ordinal);
                if (!isA1)
                {
                    return Json("""
                        {"coursework_id":"a2","assignment_title":"Trigonometry Test",
                         "has_mark_scheme":true,"file_name":"a2-ms.pdf","pages":8,
                         "processed":true,"total_marks":50,"subquestion_count":12,
                         "verification_state":"VERIFIED","warnings":[]}
                        """);
                }

                if (SchemeGate is not null)
                {
                    await SchemeGate.Task.WaitAsync(cancellationToken);
                }

                return Json(SchemeBody);
            }

            return new HttpResponseMessage(HttpStatusCode.NotFound)
            {
                Content = new StringContent("{\"code\":\"unexpected\",\"message\":\"" + path + "\"}",
                    Encoding.UTF8, "application/json"),
            };
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
