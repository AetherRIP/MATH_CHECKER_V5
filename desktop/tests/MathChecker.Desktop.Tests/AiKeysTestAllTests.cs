using System.Net;
using System.Text;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The "Test all" button on the API page, driven the way the window drives it:
/// the real command, the real async streaming client, a real WPF dispatcher
/// pumping the continuations.
///
/// The SSE body below is the byte-for-byte event sequence the backend sends
/// (<c>backend/tests/test_keys_test_all_stream.py</c> pins the server side):
/// one <c>start</c>, per key a run of <c>key_stage</c> progress events and one
/// <c>key_result</c>, then a single <c>done</c> carrying the summary.
///
/// Before the fix the page treated EVERY event as a key result, so two keys
/// produced eleven rows — most of them reading "— · auth refused · generation
/// failed" — and the completion message reported "Finished testing 11 keys".
/// </summary>
[Collection(WpfCollection.Name)]
public sealed class AiKeysTestAllTests
{
    private const string TwoKeyStream =
        "data: {\"type\":\"start\",\"total\":2}\n\n"
        + "data: {\"type\":\"key_stage\",\"index\":1,\"fingerprint\":\"fp1\",\"stage\":\"connecting\",\"message\":\"Connecting…\"}\n\n"
        + "data: {\"type\":\"key_stage\",\"index\":1,\"fingerprint\":\"fp1\",\"stage\":\"auth\",\"message\":\"Authentication: PASS (12 models visible)\"}\n\n"
        + "data: {\"type\":\"key_stage\",\"index\":1,\"fingerprint\":\"fp1\",\"stage\":\"generate\",\"message\":\"Running minimal health probe (gemini-2.5-flash)…\"}\n\n"
        + "data: {\"type\":\"key_stage\",\"index\":1,\"fingerprint\":\"fp1\",\"stage\":\"done\",\"message\":\"Status: HEALTHY\"}\n\n"
        + "data: {\"type\":\"key_result\",\"index\":1,\"fingerprint\":\"fp1\",\"status\":\"HEALTHY\",\"auth_ok\":true,\"generate_ok\":true,\"response_time_ms\":123,\"error_kind\":null,\"error_message\":null,\"summary\":{\"total\":2,\"tested\":1,\"passed\":1,\"failed\":0,\"skipped\":0}}\n\n"
        + "data: {\"type\":\"key_stage\",\"index\":2,\"fingerprint\":\"fp2\",\"stage\":\"connecting\",\"message\":\"Connecting…\"}\n\n"
        + "data: {\"type\":\"key_stage\",\"index\":2,\"fingerprint\":\"fp2\",\"stage\":\"auth\",\"message\":\"Authentication: PASS (12 models visible)\"}\n\n"
        + "data: {\"type\":\"key_stage\",\"index\":2,\"fingerprint\":\"fp2\",\"stage\":\"done\",\"message\":\"Status: COOLDOWN\"}\n\n"
        + "data: {\"type\":\"key_result\",\"index\":2,\"fingerprint\":\"fp2\",\"status\":\"COOLDOWN\",\"auth_ok\":true,\"generate_ok\":false,\"response_time_ms\":null,\"error_kind\":\"rate_limited\",\"error_message\":\"Gemini rate/quota limit hit. The key was temporarily removed from rotation; work continues on another available key.\",\"summary\":{\"total\":2,\"tested\":2,\"passed\":1,\"failed\":1,\"skipped\":0}}\n\n"
        + "data: {\"type\":\"done\",\"summary\":{\"total\":2,\"tested\":2,\"passed\":1,\"failed\":1,\"skipped\":0,\"elapsed_s\":3.4}}\n\n";

    private static string KeysJson(int healthy, int cooldown) => $$"""
        {"total":2,"healthy":{{healthy}},"cooldown":{{cooldown}},"invalid":0,"disabled":0,
         "has_keys":true,
         "keys":[
           {"fingerprint":"fp1","label":"KEY_1","status":"HEALTHY"},
           {"fingerprint":"fp2","label":"KEY_2","status":"{{(cooldown > 0 ? "COOLDOWN" : "HEALTHY")}}"}]}
        """;

    private const string WorkersJson = """
        {"size":2,"active":0,"recommended":2}
        """;

    [Fact]
    public void Test_all_shows_one_row_per_key_and_the_backend_summary()
    {
        RunOnSta(async () =>
        {
            var handler = new KeysHandler(TwoKeyStream);
            var session = await OpenSessionAsync(handler);
            var page = new AiKeysViewModel(session);
            await page.LoadAsync();

            page.TestAllCommand.Execute(null);
            await WaitUntil(() => !page.TestingAll, "the key test to finish");

            // One row per KEY — not one per stream event.
            Assert.Equal(2, page.ResultFacts.Count);
            Assert.Equal(new[] { "KEY_1", "KEY_2" }, page.ResultFacts.Select(f => f.Label));

            Assert.Contains("Healthy", page.ResultFacts[0].Value);
            Assert.Contains("123 ms", page.ResultFacts[0].Value);
            Assert.Contains("Cooldown", page.ResultFacts[1].Value);
            // A failing key must say why it failed, not just that it did.
            Assert.Contains("Rate limit", page.ResultFacts[1].Value);

            // The completion text comes from the backend's summary: key counts,
            // the pass/fail split and the elapsed time.
            Assert.Contains("Tested 2 keys", page.ResultNote);
            Assert.Contains("1 healthy", page.ResultNote);
            Assert.Contains("1 failed", page.ResultNote);
            Assert.Contains("0 skipped", page.ResultNote);
            Assert.Contains("3.4", page.ResultNote);
            Assert.DoesNotContain("Testing each key", page.ResultNote);
            // The old page claimed nothing had been tested whenever its row
            // writes were dropped, which is the opposite of what happened.
            Assert.DoesNotContain("No keys were tested", page.ResultNote);
            Assert.False(page.TestingAll);
            Assert.True(page.HasResult);
        });
    }

    [Fact]
    public void Test_all_reports_a_cancelled_run_instead_of_staying_in_progress()
    {
        RunOnSta(async () =>
        {
            // A stream that opens, delivers the first events, and never ends:
            // exactly what a slow provider call looks like to the page.
            var handler = KeysHandler.Blocking();
            var session = await OpenSessionAsync(handler);
            var page = new AiKeysViewModel(session);
            await page.LoadAsync();

            page.TestAllCommand.Execute(null);
            await handler.StreamOpened.Task.WaitAsync(TimeSpan.FromSeconds(10));

            // While the run is in flight the page shows live progress for the
            // key being probed, and says it is testing that key.
            Assert.True(page.TestingAll);
            Assert.Contains("Connecting", Assert.Single(page.ResultFacts).Value);

            // What navigating away (or a window close) does.
            page.OnDeactivated();

            await WaitUntil(() => !page.TestingAll, "the cancelled run to stop");
            await WaitUntil(() => page.ResultNote.Contains("cancel", StringComparison.OrdinalIgnoreCase),
                "the page to say the run was cancelled");

            Assert.DoesNotContain("Testing each key against the live API", page.ResultNote);
            Assert.False(page.TestingAll);
            // Nothing was reported as finished that never finished.
            Assert.DoesNotContain("Finished", page.ResultNote);
        });
    }

    [Fact]
    public void Test_all_refreshes_the_key_list_after_the_run()
    {
        RunOnSta(async () =>
        {
            // The server's view changes as a result of the run: key 2 is now
            // cooling down. A page that does not reload keeps claiming it is
            // healthy, which is the stale-state defect this pins down.
            var handler = new KeysHandler(TwoKeyStream, keysPerCall: new[] { (2, 0), (1, 1) });
            var session = await OpenSessionAsync(handler);
            var page = new AiKeysViewModel(session);
            await page.LoadAsync();
            Assert.Contains("2 healthy", page.Summary);

            page.TestAllCommand.Execute(null);
            await WaitUntil(() => !page.TestingAll, "the key test to finish");
            await WaitUntil(() => handler.KeysRequests >= 2,
                "the page to re-read the key list after the run");

            Assert.Contains("1 healthy", page.Summary);
            Assert.Contains("1 cooling", page.Summary);
        });
    }

    [Fact]
    public void Test_all_ignores_a_second_click_while_running()
    {
        RunOnSta(async () =>
        {
            var handler = KeysHandler.Blocking();
            var session = await OpenSessionAsync(handler);
            var page = new AiKeysViewModel(session);
            await page.LoadAsync();

            var command = page.TestAllCommand;
            command.Execute(null);
            await handler.StreamOpened.Task.WaitAsync(TimeSpan.FromSeconds(10));

            Assert.False(command.CanExecute(null), "the command must refuse a re-entrant run");
            command.Execute(null);
            command.Execute(null);
            await Task.Delay(150);

            Assert.Equal(1, handler.StreamRequests);

            page.OnDeactivated();
            await WaitUntil(() => !page.TestingAll, "the run to stop");
        });
    }

    [Fact]
    public void The_commands_are_stable_instances()
    {
        // A page that builds a new command on every property read can be given
        // a second command mid-run by a re-bind, and two "Test all" streams
        // would then compete for this page's state. The rest of the app asserts
        // the same thing (CorrectionPageTests), so this page does too.
        RunOnSta(async () =>
        {
            var session = await OpenSessionAsync(new KeysHandler(TwoKeyStream));
            var page = new AiKeysViewModel(session);

            Assert.Same(page.TestAllCommand, page.TestAllCommand);
            Assert.Same(page.TestSelectedCommand, page.TestSelectedCommand);
            Assert.Same(page.AddKeysCommand, page.AddKeysCommand);
            Assert.Same(page.RemoveCommand, page.RemoveCommand);
            Assert.Same(page.DisableCommand, page.DisableCommand);
            Assert.Same(page.EnableCommand, page.EnableCommand);
            Assert.Same(page.ResizeWorkersCommand, page.ResizeWorkersCommand);
        });
    }

    // ---------------------------------------------------------------- harness

    private static async Task WaitUntil(Func<bool> condition, string what, int timeoutMs = 15000)
    {
        var deadline = Environment.TickCount64 + timeoutMs;
        while (Environment.TickCount64 < deadline)
        {
            if (condition())
            {
                return;
            }

            await Task.Delay(20);
        }

        throw new Xunit.Sdk.XunitException($"timed out waiting for {what}");
    }

    private static async Task<AppSession> OpenSessionAsync(KeysHandler handler)
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

    /// <summary>Runs the body on an STA thread with a real dispatcher pumping
    /// its continuations — the same threading a WPF command gets. No
    /// <see cref="System.Windows.Application"/> is created, so this class can
    /// run alongside the other WPF tests without owning the process-wide one.</summary>
    private static void RunOnSta(Func<Task> body)
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            var dispatcher = Dispatcher.CurrentDispatcher;
            SynchronizationContext.SetSynchronizationContext(
                new DispatcherSynchronizationContext(dispatcher));
            var frame = new DispatcherFrame();

            dispatcher.InvokeAsync(new Action(async () =>
            {
                try
                {
                    await body();
                }
                catch (Exception ex)
                {
                    failure = ex;
                }
                finally
                {
                    frame.Continue = false;
                }
            }));

            Dispatcher.PushFrame(frame);
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();

        if (failure is not null)
        {
            throw new Xunit.Sdk.XunitException(failure.ToString());
        }
    }

    /// <summary>Answers the three routes the page uses, by URL. The stream can
    /// be a finished response or one that stays open until cancelled.</summary>
    private sealed class KeysHandler : HttpMessageHandler
    {
        private readonly string? _stream;
        private readonly Queue<(int Healthy, int Cooldown)> _keysPerCall;
        private int _keysRequests;
        private int _streamRequests;

        public KeysHandler(string stream, params (int Healthy, int Cooldown)[] keysPerCall)
        {
            _stream = stream;
            _keysPerCall = new Queue<(int, int)>(keysPerCall.Length > 0
                ? keysPerCall
                : new[] { (2, 0) });
        }

        private KeysHandler()
        {
            _stream = null;
            _keysPerCall = new Queue<(int, int)>();
        }

        public static KeysHandler Blocking() => new();

        public TaskCompletionSource StreamOpened { get; } =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public int KeysRequests => Volatile.Read(ref _keysRequests);

        public int StreamRequests => Volatile.Read(ref _streamRequests);

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;

            if (path.EndsWith("/keys/test/stream", StringComparison.Ordinal))
            {
                Interlocked.Increment(ref _streamRequests);
                if (_stream is null)
                {
                    StreamOpened.TrySetResult();
                    // First chunk, then nothing: the run stays in flight until
                    // the page cancels it.
                    var content = new StreamContent(new OpenEndedStream(
                        "data: {\"type\":\"start\",\"total\":2}\n\n"
                        + "data: {\"type\":\"key_stage\",\"index\":1,\"fingerprint\":\"fp1\","
                        + "\"stage\":\"connecting\",\"message\":\"Connecting…\"}\n\n"));
                    return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
                    {
                        Content = content,
                        RequestMessage = request,
                    });
                }

                return Task.FromResult(Json(_stream, "text/event-stream", request));
            }

            if (path.EndsWith("/keys", StringComparison.Ordinal))
            {
                Interlocked.Increment(ref _keysRequests);
                var (healthy, cooldown) = _keysPerCall.Count > 0 ? _keysPerCall.Dequeue() : (2, 0);
                if (_keysPerCall.Count == 0)
                {
                    _keysPerCall.Enqueue((healthy, cooldown));   // repeat the last answer
                }

                return Task.FromResult(Json(KeysJson(healthy, cooldown), "application/json", request));
            }

            if (path.EndsWith("/workers", StringComparison.Ordinal))
            {
                return Task.FromResult(Json(WorkersJson, "application/json", request));
            }

            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.NotFound)
            {
                Content = new StringContent($$"""{"code":"no_stub","message":"{{path}}"}""",
                    Encoding.UTF8, "application/json"),
                RequestMessage = request,
            });
        }

        private static HttpResponseMessage Json(string body, string contentType,
            HttpRequestMessage request) =>
            new(HttpStatusCode.OK)
            {
                Content = new StringContent(body, Encoding.UTF8, contentType),
                RequestMessage = request,
            };
    }

    /// <summary>A response body that yields one chunk and then never ends.</summary>
    private sealed class OpenEndedStream : Stream
    {
        private readonly byte[] _first;
        private bool _sent;

        public OpenEndedStream(string first) => _first = Encoding.UTF8.GetBytes(first);

        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();

        public override long Position
        {
            get => throw new NotSupportedException();
            set => throw new NotSupportedException();
        }

        public override async ValueTask<int> ReadAsync(Memory<byte> buffer,
            CancellationToken cancellationToken = default)
        {
            if (!_sent)
            {
                _sent = true;
                var count = Math.Min(buffer.Length, _first.Length);
                _first.AsMemory(0, count).CopyTo(buffer);
                return count;
            }

            await Task.Delay(Timeout.Infinite, cancellationToken);
            return 0;
        }

        public override int Read(byte[] buffer, int offset, int count) =>
            throw new NotSupportedException("this test stream is read asynchronously");

        public override void Flush()
        {
        }

        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();

        public override void SetLength(long value) => throw new NotSupportedException();

        public override void Write(byte[] buffer, int offset, int count) =>
            throw new NotSupportedException();
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
