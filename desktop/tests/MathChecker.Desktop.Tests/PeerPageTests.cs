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
/// The peer page is a view over the backend's supervisor, never a second copy of
/// its state. These tests pin the properties that make that honest: the page
/// loads OFF because the backend says so, opening it starts nothing, a state is
/// shown only after the backend reports it, and a failed start is displayed as a
/// failure rather than as success.
/// </summary>
public class PeerPageTests
{
    private const string OffBody = """
        {"enabled":false,"state":"DISABLED","label":"Peer networking off",
         "detail":"peer networking is off; nothing is started","corrupt":false,
         "supervised":false,"credentials_present":false,"service":null}
        """;

    private const string RunningBody = """
        {"enabled":true,"state":"RUNNING","label":"Peer service running",
         "detail":"peer service is running","corrupt":false,"supervised":true,
         "credentials_present":true,
         "service":{"pid":4242,"restarts":0,"encrypted_store":true,
                    "store_detail":"stored with DPAPI","started_at":1.0}}
        """;

    private const string NoCredentialsBody = """
        {"enabled":true,"state":"CREDENTIALS_UNAVAILABLE",
         "label":"Peer credentials unavailable",
         "detail":"this device has not been invited yet","corrupt":false,
         "supervised":true,"credentials_present":false,
         "requested":true,"confirmed":false,"service":null}
        """;

    [Fact]
    public async Task First_run_shows_the_backends_off_state()
    {
        var handler = new PeerHandler(_ => OffBody);
        var page = new PeerViewModel(await SessionAsync(handler));

        await page.LoadAsync();

        Assert.False(page.Enabled);
        Assert.Equal("Off", page.PreferenceText);
        Assert.Equal("DISABLED", page.State);
        Assert.Equal("Peer networking off", page.Label);
        Assert.False(page.Supervised);
        Assert.Null(page.Error);
    }

    [Fact]
    public async Task Opening_the_page_starts_nothing()
    {
        var handler = new PeerHandler(_ => OffBody);
        var page = new PeerViewModel(await SessionAsync(handler));

        await page.LoadAsync();

        var only = Assert.Single(handler.Requests);
        Assert.Equal("GET", only.Method);
        Assert.EndsWith("/api/peer/status", only.Url);
    }

    [Fact]
    public async Task Enabling_reports_running_only_after_the_backend_says_so()
    {
        // The switch POST answers with the observation, and the reload after it
        // reads the same state: nothing here is optimistic.
        var handler = new PeerHandler(_ => RunningBody);
        var page = new PeerViewModel(await SessionAsync(handler));
        await page.LoadAsync();

        page.EnableCommand.Execute(null);
        await WaitUntil(() => !page.IsBusy && page.State == "RUNNING", "the running state");

        var posted = Assert.Single(handler.Requests.Where(r => r.Method == "POST"));
        Assert.EndsWith("/api/peer/preference", posted.Url);
        Assert.NotNull(posted.Body);
        Assert.Contains("\"enabled\":true", posted.Body);
        Assert.True(page.Enabled);
        Assert.Equal("On", page.PreferenceText);
        Assert.Equal(4242, handler.LastPid);
    }

    [Fact]
    public async Task A_start_that_could_not_run_is_not_shown_as_started()
    {
        var handler = new PeerHandler(_ => NoCredentialsBody);
        var page = new PeerViewModel(await SessionAsync(handler));
        await page.LoadAsync();

        page.EnableCommand.Execute(null);
        await WaitUntil(() => !page.IsBusy && page.State == "CREDENTIALS_UNAVAILABLE",
            "the refused state");

        Assert.True(page.Enabled);                       // the preference was stored
        Assert.NotEqual("RUNNING", page.State);          // but nothing is running
        Assert.Equal("Peer credentials unavailable", page.Label);
        Assert.Contains("invited", page.Detail);
        Assert.Contains("invited", page.Status);         // not "Peer service started."
    }

    [Fact]
    public async Task Disabling_records_the_preference_and_shows_off()
    {
        var handler = new PeerHandler(request =>
            request.Method == HttpMethod.Post
                ? """
                  {"enabled":false,"state":"DISABLED","label":"Peer networking off",
                   "detail":"peer networking is off; nothing is started","corrupt":false,
                   "supervised":true,"credentials_present":true,"requested":false,
                   "confirmed":true,"service":null}
                  """
                : OffBody);
        var page = new PeerViewModel(await SessionAsync(handler));
        await page.LoadAsync();

        page.DisableCommand.Execute(null);
        await WaitUntil(() => !page.IsBusy && !page.Enabled, "the off state");

        var posted = Assert.Single(handler.Requests.Where(r => r.Method == "POST"));
        Assert.NotNull(posted.Body);
        Assert.Contains("\"enabled\":false", posted.Body);
        Assert.Equal("DISABLED", page.State);
        Assert.Equal("Off", page.PreferenceText);
    }

    [Fact]
    public async Task A_refresh_replaces_a_previous_running_state_instead_of_keeping_it()
    {
        var queue = new Queue<string>(new[] { RunningBody, OffBody });
        var handler = new PeerHandler(_ => queue.Dequeue());
        var page = new PeerViewModel(await SessionAsync(handler));

        await page.LoadAsync();
        Assert.Equal("RUNNING", page.State);

        // The supervisor died between the two reads: the page must follow it.
        await page.LoadAsync();

        Assert.Equal("DISABLED", page.State);
        Assert.False(page.Enabled);
    }

    [Fact]
    public async Task A_backend_failure_is_shown_and_does_not_invent_a_state()
    {
        var handler = new PeerHandler(_ => OffBody);
        handler.PostResponse = new HttpResponseMessage(HttpStatusCode.InternalServerError)
        {
            Content = new StringContent(
                """{"code":"state_unwritable","message":"The peer switch could not be saved."}""",
                Encoding.UTF8, "application/json"),
        };
        var page = new PeerViewModel(await SessionAsync(handler));
        await page.LoadAsync();

        page.EnableCommand.Execute(null);
        await WaitUntil(() => !page.IsBusy && page.HasError, "the error");

        Assert.Contains("could not be saved", page.Error);
        Assert.Equal("DISABLED", page.State);
        Assert.False(page.Enabled);
    }

    [Fact]
    public async Task The_diagnostics_never_carry_key_material()
    {
        var handler = new PeerHandler(_ => RunningBody);
        var page = new PeerViewModel(await SessionAsync(handler));

        await page.LoadAsync();

        var text = string.Join(" | ", page.Facts.Select(f => f.Label + "=" + f.Value)).ToLowerInvariant();
        foreach (var forbidden in new[] { "private_key", "privatekey", "secret", "token" })
        {
            Assert.DoesNotContain(forbidden, text);
        }

        Assert.Contains("Encrypted with Windows DPAPI", page.StoreText);
        Assert.Contains("pid 4242", page.ProcessText);
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
        return new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs());
    }

    private static async Task WaitUntil(Func<bool> condition, string what, int timeoutMs = 10000)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        while (!condition())
        {
            if (DateTime.UtcNow > deadline)
            {
                throw new TimeoutException("Timed out waiting for " + what + ".");
            }

            await Task.Delay(10);
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

    /// <summary>Answers by method and path, so a retry or a reload cannot consume
    /// the wrong queued response.</summary>
    private sealed class PeerHandler : HttpMessageHandler
    {
        private readonly Func<HttpRequestMessage, string> _get;

        public PeerHandler(Func<HttpRequestMessage, string> get) => _get = get;

        public List<RecordedRequest> Requests { get; } = new();

        public HttpResponseMessage? PostResponse { get; set; }

        public int? LastPid { get; private set; }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var body = request.Content is null
                ? null
                : await request.Content.ReadAsStringAsync(cancellationToken);
            Requests.Add(new RecordedRequest(request.Method.Method,
                request.RequestUri!.AbsoluteUri, body, new Dictionary<string, string>()));

            if (request.Method == HttpMethod.Post && PostResponse is not null)
            {
                return PostResponse;
            }

            var json = _get(request);
            if (json.Contains("4242"))
            {
                LastPid = 4242;
            }

            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(json, Encoding.UTF8, "application/json"),
                RequestMessage = request,
            };
        }
    }
}
