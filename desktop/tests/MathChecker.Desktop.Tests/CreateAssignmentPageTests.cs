using System.Net;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The assignments list only had a one-line title box, so no assignment could
/// be created with the grade it is out of or the mark scheme the corrector
/// needs. These pin the page that replaced it.
/// </summary>
public class CreateAssignmentPageTests
{
    private const string NotConnectedBody = """{"connected":false,"client_configured":true}""";
    private const string ConnectedBody = """
        {"connected":true,"email":"teacher@school.org","client_configured":true}
        """;
    private const string CoursesBody = """
        {"courses":[{"id":"c1","name":"10A Mathematics"}],"total":1}
        """;

    [Fact]
    public async Task A_local_assignment_is_created_with_its_grade_and_mark_scheme()
    {
        var scheme = TempPdf();
        var (session, handler, dialogs) = await SessionAsync(
            NotConnectedBody,
            """{"id":"local-abc123","course_id":"local-course","managed":false,"local":true}""",
            """{"coursework_id":"local-abc123","stored":true}""");
        using (handler)
        {
            var page = new CreateAssignmentViewModel(session);
            await page.LoadAsync();
            dialogs.OpenPath = scheme;

            page.AssignmentTitle = "Algebra test";
            page.MaxPoints = "20";
            page.DueDate = "2026-11-14";
            page.PickSchemeCommand.Execute(null);
            Assert.True(page.HasScheme);
            Assert.Equal(Path.GetFileName(scheme), page.SchemeName);

            page.CreateCommand.Execute(null);
            // Wait for the form to be cleared, not merely for the requests to be
            // recorded: the upload is still in flight when the third request is
            // logged, so asserting on the reset any earlier is a race.
            await WaitUntilAsync(() => handler.Requests.Count >= 3
                                       && page.AssignmentTitle.Length == 0
                                       && !page.HasScheme);

            var create = handler.Requests[1];
            Assert.Equal("POST", create.Method);
            Assert.Contains("/api/assignments/local", create.Url);
            using var body = JsonDocument.Parse(create.Body!);
            Assert.Equal("Algebra test", body.RootElement.GetProperty("title").GetString());
            Assert.Equal(20d, body.RootElement.GetProperty("max_points").GetDouble());
            Assert.Equal("2026-11-14", body.RootElement.GetProperty("due_date").GetString());

            var upload = handler.Requests[2];
            Assert.Equal("POST", upload.Method);
            Assert.Contains("/assignments/local-abc123/mark-scheme", upload.Url);

            // The form is ready for the next assignment, not left half-filled.
            Assert.Equal(string.Empty, page.AssignmentTitle);
            Assert.False(page.HasScheme);
        }

        File.Delete(scheme);
    }

    [Fact]
    public async Task A_bad_grade_is_refused_before_anything_is_created()
    {
        var (session, handler, _) = await SessionAsync(NotConnectedBody);
        using (handler)
        {
            var page = new CreateAssignmentViewModel(session);
            await page.LoadAsync();

            page.AssignmentTitle = "Algebra test";
            page.MaxPoints = "twenty";

            page.CreateCommand.Execute(null);
            await WaitUntilAsync(() => page.Error is not null);

            Assert.Contains("positive number", page.Error);
            Assert.Single(handler.Requests); // only the status read
        }
    }

    [Fact]
    public async Task A_blank_name_cannot_be_created()
    {
        var (session, handler, _) = await SessionAsync(NotConnectedBody);
        using (handler)
        {
            var page = new CreateAssignmentViewModel(session);
            await page.LoadAsync();

            Assert.False(page.CanCreate);
            page.AssignmentTitle = "   ";
            Assert.False(page.CanCreate);
            page.AssignmentTitle = "Vectors";
            Assert.True(page.CanCreate);
        }
    }

    [Fact]
    public async Task A_badly_written_due_date_is_explained_not_stored()
    {
        var (session, handler, _) = await SessionAsync(NotConnectedBody);
        using (handler)
        {
            var page = new CreateAssignmentViewModel(session);
            await page.LoadAsync();

            page.AssignmentTitle = "Vectors";
            page.DueDate = "next friday";

            page.CreateCommand.Execute(null);
            await WaitUntilAsync(() => page.Error is not null);

            Assert.Contains("YYYY-MM-DD", page.Error);
            Assert.Single(handler.Requests);
        }
    }

    [Fact]
    public async Task A_connected_account_offers_its_classes_as_destinations()
    {
        var (session, handler, _) = await SessionAsync(ConnectedBody, CoursesBody);
        using (handler)
        {
            var page = new CreateAssignmentViewModel(session);
            await page.LoadAsync();

            Assert.True(page.ClassroomConnected);
            Assert.Equal(CreateAssignmentViewModel.LocalDestination, page.Destination);
            Assert.Contains(page.Destinations, d => d.Value == "c1");
            Assert.False(page.CreatesInClassroom);
        }
    }

    [Fact]
    public async Task Choosing_a_class_creates_the_assignment_in_google_classroom()
    {
        var (session, handler, _) = await SessionAsync(
            ConnectedBody, CoursesBody,
            """{"coursework":{"id":"cw-9"},"course_id":"c1"}""");
        using (handler)
        {
            var page = new CreateAssignmentViewModel(session);
            await page.LoadAsync();
            page.AssignmentTitle = "Trigonometry";
            page.MaxPoints = "30";
            page.Destination = "c1";

            page.CreateCommand.Execute(null);
            await WaitUntilAsync(() => handler.Requests.Count >= 3);

            var create = handler.Requests[2];
            Assert.Contains("/api/classroom/courses/c1/assignments/create", create.Url);
            using var body = JsonDocument.Parse(create.Body!);
            Assert.Equal("Trigonometry", body.RootElement.GetProperty("title").GetString());
            Assert.Equal(30d, body.RootElement.GetProperty("max_points").GetDouble());
        }
    }

    [Fact]
    public void The_created_id_is_read_from_either_shape_the_backend_returns()
    {
        using var local = JsonDocument.Parse("""{"id":"local-1","course_id":"x"}""");
        using var classroom = JsonDocument.Parse("""{"coursework":{"id":"cw-2"}}""");
        using var nothing = JsonDocument.Parse("""{"ok":true}""");

        Assert.Equal("local-1", CreateAssignmentViewModel.CreatedId(local.RootElement));
        Assert.Equal("cw-2", CreateAssignmentViewModel.CreatedId(classroom.RootElement));
        Assert.Null(CreateAssignmentViewModel.CreatedId(nothing.RootElement));
    }

    private static string TempPdf()
    {
        var path = Path.Combine(Path.GetTempPath(), "mc-scheme-" + Guid.NewGuid().ToString("N") + ".pdf");
        File.WriteAllBytes(path, "%PDF-1.4 test"u8.ToArray());
        return path;
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

            await Task.Delay(20);
        }

        Assert.Fail("The condition was still false after " + timeoutMs + " ms.");
    }

    private static async Task<(AppSession Session, StubHttpHandler Handler, PickDialogs Dialogs)>
        SessionAsync(params string[] bodies)
    {
        var handler = new StubHttpHandler();
        foreach (var body in bodies)
        {
            handler.Respond(HttpStatusCode.OK, body);
        }

        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        var dialogs = new PickDialogs();
        return (new AppSession(settings, new MemoryLog(), host, api, dialogs), handler, dialogs);
    }

    private sealed class PickDialogs : IDialogService
    {
        public string? OpenPath { get; set; }

        public bool Confirm(string title, string message) => true;
        public void Info(string title, string message)
        {
        }

        public void Error(string title, string message)
        {
        }

        public string? PickOpenFile(string title, string filter) => OpenPath;
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
