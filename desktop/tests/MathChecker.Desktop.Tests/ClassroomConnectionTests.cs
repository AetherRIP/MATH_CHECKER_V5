using System.Net;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The People page had no way to connect Google Classroom at all: the API
/// client could start the OAuth flow but no button ever called it. These pin the
/// card's three states and the buttons that change them.
/// </summary>
public class ClassroomConnectionTests
{
    private const string StudentsBody = """{"students":[]}""";

    private const string ConnectedBody = """
        {"connected":true,"email":"teacher@school.org","client_configured":true}
        """;

    private const string NotConnectedBody = """
        {"connected":false,"email":null,"client_configured":true}
        """;

    private const string NoClientBody = """
        {"connected":false,"email":null,"client_configured":false,
         "problem":"Google OAuth client is not configured."}
        """;

    [Fact]
    public async Task A_connected_account_names_itself_and_offers_reconnect()
    {
        var (session, handler, _) = await SessionAsync(ConnectedBody, StudentsBody);
        using (handler)
        {
            var page = new StudentsViewModel(session);

            await page.LoadAsync();

            Assert.True(page.ClassroomConnected);
            Assert.Equal("Connected as teacher@school.org", page.ClassroomStateText);
            Assert.Equal("Reconnect", page.ClassroomActionText);
            Assert.True(page.CanConnect);
            Assert.False(page.HasClassroomProblem);
        }
    }

    [Fact]
    public async Task A_fresh_install_offers_the_connect_button_by_name()
    {
        var (session, handler, _) = await SessionAsync(NotConnectedBody, StudentsBody);
        using (handler)
        {
            var page = new StudentsViewModel(session);

            await page.LoadAsync();

            Assert.False(page.ClassroomConnected);
            Assert.Equal("Connect Google Classroom", page.ClassroomActionText);
            Assert.True(page.CanConnect);
            Assert.Contains("Connect a teacher account", page.ClassroomHint);
        }
    }

    [Fact]
    public async Task A_missing_oauth_client_disables_connect_and_says_why()
    {
        var (session, handler, _) = await SessionAsync(NoClientBody, StudentsBody);
        using (handler)
        {
            var page = new StudentsViewModel(session);

            await page.LoadAsync();

            Assert.False(page.CanConnect);
            Assert.True(page.HasClassroomProblem);
            Assert.Contains("client file is missing", page.ClassroomHint);
            // The backend's own explanation is shown, not hidden behind ours.
            Assert.Contains("not configured", page.ClassroomProblem);
        }
    }

    [Fact]
    public async Task Connecting_runs_the_oauth_flow_and_reloads_the_state()
    {
        var (session, handler, _) = await SessionAsync(
            NotConnectedBody, StudentsBody,          // first load
            """{"connected":true,"email":"teacher@school.org"}""", // POST classroom/connect
            ConnectedBody, StudentsBody);            // reload after connecting
        using (handler)
        {
            var page = new StudentsViewModel(session);
            await page.LoadAsync();
            Assert.False(page.ClassroomConnected);

            page.ConnectClassroomCommand.Execute(null);
            await WaitUntilAsync(() => page.ClassroomConnected);

            Assert.Contains(handler.Requests, r =>
                r.Method == "POST" && r.Url.EndsWith("/api/classroom/connect", StringComparison.Ordinal));
            Assert.Equal("Connected as teacher@school.org", page.ClassroomStateText);
        }
    }

    [Fact]
    public async Task Disconnecting_asks_first_and_says_local_data_is_kept()
    {
        var (session, handler, dialogs) = await SessionAsync(ConnectedBody, StudentsBody,
            """{"connected":false}""", NotConnectedBody, StudentsBody);
        using (handler)
        {
            var page = new StudentsViewModel(session);
            await page.LoadAsync();

            // The stub declines once: nothing may be sent.
            page.DisconnectClassroomCommand.Execute(null);
            await Task.Delay(50);
            Assert.DoesNotContain(handler.Requests, r =>
                r.Url.EndsWith("/api/classroom/disconnect", StringComparison.Ordinal));

            dialogs.ConfirmAnswer = true;
            page.DisconnectClassroomCommand.Execute(null);
            await WaitUntilAsync(() => !page.ClassroomConnected);

            Assert.Contains(handler.Requests, r =>
                r.Method == "POST" && r.Url.EndsWith("/api/classroom/disconnect", StringComparison.Ordinal));
            Assert.Contains("kept", page.Status);
        }
    }

    private static async Task<(AppSession Session, StubHttpHandler Handler, RecordingDialogs Dialogs)>
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
        var dialogs = new RecordingDialogs();
        return (new AppSession(settings, new MemoryLog(), host, api, dialogs), handler, dialogs);
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

    private sealed class RecordingDialogs : IDialogService
    {
        public bool ConfirmAnswer { get; set; }

        public bool Confirm(string title, string message) => ConfirmAnswer;
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
