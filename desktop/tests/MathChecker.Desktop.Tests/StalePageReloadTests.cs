using System.Net;
using System.Text;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// A page that changes backend state must show the new state.
///
/// Every one of these commands used to end with <c>await LoadAsync()</c> INSIDE
/// its own action, and that reload is a no-op: <c>RunAsync</c> holds
/// <c>IsBusy</c> for the whole action, and <c>LoadAsync</c> returns immediately
/// while a page is busy. The backend changed, the screen did not, and the only
/// way to see the truth was to press Refresh. These tests fail if a page goes
/// back to reloading itself from inside its own busy window.
/// </summary>
public class StalePageReloadTests
{
    [Fact]
    public async Task Accepting_a_result_removes_it_from_the_queue_without_a_manual_refresh()
    {
        var state = new ReviewState();
        state.Items.Add("""{"id":"r1","student_name":"Ada","earned":7,"available":10}""");
        var handler = new RouteHandler();
        handler.Get("/api/review", () => $$"""{"items":[{{string.Join(",", state.Items)}}],"total":{{state.Items.Count}}}""");
        handler.Post("/api/results/r1/override", () =>
        {
            state.Items.Clear();          // the backend now has nothing to review
            return "{}";
        });

        var page = new ReviewViewModel(await SessionAsync(handler));
        await page.LoadAsync();
        Assert.Single(page.Items);
        page.Selected = page.Items[0];

        // The command properties build a fresh instance per read, so the test
        // must hold the one it executed — otherwise it waits on a command that
        // never ran and asserts before the reload happens.
        var accept = page.AcceptCommand;
        accept.Execute(null);
        await WaitUntil(() => !accept.IsRunning, "the accept to finish");

        Assert.Empty(page.Items);                       // without the reload this is still 1
        Assert.Equal("Result accepted.", page.Status);  // and the confirmation survives it
        Assert.Equal(2, handler.Count("GET", "/api/review"));
    }

    [Fact]
    public async Task Cancelling_a_job_shows_the_cancelled_row_without_a_manual_refresh()
    {
        var state = new JobState { Status = "RUNNING" };
        var handler = new RouteHandler();
        handler.Get("/api/jobs", () => $$"""
            {"jobs":[{"id":"j1","student_name":"Ada","status":"{{state.Status}}"}]}
            """);
        handler.Post("/api/jobs/j1/cancel", () =>
        {
            state.Status = "CANCELLED";
            return "{}";
        });

        var page = new JobsViewModel(await SessionAsync(handler), "jobs", "Job history",
            showWorkers: false);
        await page.LoadAsync();
        Assert.Equal("RUNNING", page.Items[0].Status);
        page.Selected = page.Items[0];

        var cancel = page.CancelJobCommand;
        cancel.Execute(null);
        await WaitUntil(() => !cancel.IsRunning, "the cancel to finish");

        Assert.Equal("CANCELLED", page.Items[0].Status);  // stale without the reload
        Assert.Equal(2, handler.Count("GET", "/api/jobs"));
    }

    [Fact]
    public async Task A_reload_inside_a_command_body_is_a_no_op_and_the_helper_is_not()
    {
        var page = new CountingPage(await SessionAsync(new RouteHandler()));

        await page.LoadAsync();
        Assert.Equal(1, page.Loads);

        var trap = page.TrapCommand;
        trap.Execute(null);
        await WaitUntil(() => !trap.IsRunning, "the trapped command");
        Assert.Equal(1, page.Loads);          // documents WHY the helper exists

        var reloading = page.ReloadingCommand;
        reloading.Execute(null);
        await WaitUntil(() => !reloading.IsRunning, "the reloading command");
        Assert.Equal(2, page.Loads);
        Assert.Equal("Done.", page.Status);
    }

    // ------------------------------------------------------------------ harness

    private static async Task<AppSession> SessionAsync(HttpMessageHandler handler)
    {
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        await host.StartAsync();

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        return new AppSession(settings, new MemoryLog(), host, api, new ConfirmingDialogs());
    }

    private static async Task WaitUntil(Func<bool> condition, string what, int timeoutMs = 15000)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        while (!condition())
        {
            if (DateTime.UtcNow > deadline)
            {
                throw new TimeoutException("Timed out waiting for " + what + ".");
            }

            await Task.Delay(20);
        }
    }

    private sealed class ReviewState
    {
        public List<string> Items { get; } = new();
    }

    private sealed class JobState
    {
        public string Status { get; set; } = "RUNNING";
    }

    /// <summary>Answers by method + path, so a page's reload is served fresh
    /// state instead of a queued response consumed by the wrong call.</summary>
    private sealed class RouteHandler : HttpMessageHandler
    {
        private readonly List<(string Method, string Path, Func<string> Body)> _routes = new();
        private readonly List<(string Method, string Path)> _seen = new();

        public void Get(string path, Func<string> body) => _routes.Add(("GET", path, body));

        public void Post(string path, Func<string> body) => _routes.Add(("POST", path, body));

        public int Count(string method, string path) =>
            _seen.Count(s => s.Method == method && s.Path == path);

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var method = request.Method.Method;
            var path = request.RequestUri!.AbsolutePath;
            lock (_seen)
            {
                _seen.Add((method, path));
            }

            var route = _routes.FirstOrDefault(r => r.Method == method && r.Path == path);
            if (route.Body is null)
            {
                return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound)
                {
                    Content = new StringContent("""{"code":"not_found","message":"no stub"}""",
                        Encoding.UTF8, "application/json"),
                    RequestMessage = request,
                });
            }

            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(route.Body(), Encoding.UTF8, "application/json"),
                RequestMessage = request,
            });
        }
    }

    /// <summary>A page small enough to state the rule directly.</summary>
    private sealed class CountingPage : PageViewModel
    {
        public CountingPage(AppSession session)
            : base(session, "counting", "Counting")
        {
        }

        public int Loads { get; private set; }

        protected override Task LoadCoreAsync()
        {
            Loads++;
            return Task.CompletedTask;
        }

        /// <summary>The pattern that looks right and does nothing.</summary>
        public AsyncCommand TrapCommand => Command(async () =>
        {
            await Task.Yield();
            await LoadAsync();
        });

        public AsyncCommand ReloadingCommand => CommandWithReload(async () =>
        {
            await Task.Yield();
        }, "Done.");
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
        public bool Confirm(string title, string message) => true;
        public void Info(string title, string message)
        {
        }

        public void Error(string title, string message)
        {
        }

        public string? PickOpenFile(string title, string filter) => null;
        public string? PickSaveFile(string title, string filter, string defaultName) => null;
        public string? AskText(string title, string message, string initial = "") => initial;
        public void OpenExternal(string pathOrUrl)
        {
        }

        public void OpenFolder(string directory)
        {
        }
    }
}
