using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.E2E;

/// <summary>
/// Drives the REAL "AI &amp; keys" page against a REAL backend: keys are stored
/// through the real endpoint, "Test all" runs over the real server-sent event
/// stream, and the result rows are the ones the page would show a teacher.
///
/// Why this exists next to the unit tests: those call the view model with a fake
/// HTTP handler, so they prove the page reads the wire format correctly but never
/// that the page and a running backend agree. This runs the real page, on a real
/// WPF dispatcher, against the real service.
/// </summary>
public static class ApiKeysScenario
{
    /// <summary>Deliberately non-functional test keys. Demo Mode mocks the
    /// provider, so no request leaves this machine; nothing here is a credential
    /// for anything, and the data folder is deleted at the end of the run.</summary>
    private static readonly string[] SyntheticKeys =
    {
        "AIzaSyE2ESyntheticKeyOne00000000000000",
        "AIzaSyE2ESyntheticKeyTwo00000000000000",
    };

    public static async Task<IReadOnlyList<CheckResult>> RunAsync(
        MathCheckerApiClient api, DesktopSettings settings, IFileLog log, BackendHost host,
        string? shotPath)
    {
        var results = new List<CheckResult>();
        void Check(string name, bool ok, string detail) => results.Add(new CheckResult(name, ok, detail));

        // ---------------------------------------------------------- the backend
        var added = await api.AddKeysAsync(SyntheticKeys);
        var addedKeys = Count(added, "keys");
        Check("the backend accepted the keys", addedKeys == SyntheticKeys.Length,
            $"added={addedKeys} of {SyntheticKeys.Length}");

        var keys = await api.KeysAsync();
        Check("the key list reports every stored key", keys.Total == SyntheticKeys.Length,
            $"total={keys.Total} healthy={keys.Healthy}");
        Check("demo keys are healthy, not rejected", keys.Healthy == SyntheticKeys.Length
                                                     && keys.Invalid == 0,
            $"healthy={keys.Healthy} invalid={keys.Invalid} cooldown={keys.Cooldown}");

        // Nothing the page is handed may carry key material.
        var leaked = keys.Keys
            .SelectMany(k => new[] { k.Label, k.Status, k.Fingerprint, k.ErrorKind, k.ErrorMessage })
            .Where(v => v is not null && v.Contains("AIza", StringComparison.OrdinalIgnoreCase))
            .ToArray();
        Check("no endpoint hands the UI the key itself", leaked.Length == 0,
            leaked.Length == 0 ? $"{keys.Keys.Count} keys checked" : string.Join(",", leaked));

        // ------------------------------------------------- the real page, on a real dispatcher
        var page = await OnUiThreadAsync(async () =>
        {
            if (Application.Current is not MathChecker.Desktop.App)
            {
                var app = new MathChecker.Desktop.App();
                app.InitializeComponent();
            }

            var session = new AppSession(settings, log, host, api, new NoDialogs());
            var viewModel = new AiKeysViewModel(session);
            await viewModel.LoadAsync();

            Check("the page loaded the keys", viewModel.Keys.Count == SyntheticKeys.Length,
                $"rows={viewModel.Keys.Count}");
            Check("the page's own summary counts them", viewModel.Summary.Contains("healthy",
                    StringComparison.OrdinalIgnoreCase) || viewModel.Summary.Contains("1", StringComparison.Ordinal),
                $"summary='{viewModel.Summary}'");

            // Test all, driven exactly as the button drives it.
            var sawRunning = false;
            viewModel.PropertyChanged += (_, e) =>
            {
                if (e.PropertyName == nameof(AiKeysViewModel.TestingAll) && viewModel.TestingAll)
                {
                    sawRunning = true;
                }
            };

            viewModel.TestAllCommand.Execute(null);
            Check("Test all starts", sawRunning || viewModel.TestingAll, $"testing={viewModel.TestingAll}");

            await WaitUntilAsync(() => !viewModel.TestingAll, TimeSpan.FromMinutes(3));
            await WaitUntilAsync(() => viewModel.HasResult, TimeSpan.FromSeconds(30));

            var note = viewModel.ResultNote;
            Check("Test all reports one row per key, not one per event",
                viewModel.ResultFacts.Count == viewModel.Keys.Count,
                $"rows={viewModel.ResultFacts.Count} keys={viewModel.Keys.Count} note='{note}'");
            Check("Test all names the number of keys it tested",
                note.Contains($"Tested {viewModel.Keys.Count} key", StringComparison.OrdinalIgnoreCase),
                note);
            Check("Test all says it finished rather than still testing",
                !note.Contains("Testing each key", StringComparison.OrdinalIgnoreCase)
                && viewModel.TestingAll == false,
                note);
            Check("Test all agrees with the key list it displays",
                viewModel.ResultFacts.All(f => f.Value.Length > 0),
                string.Join(" | ", viewModel.ResultFacts.Select(f => $"{f.Label}={f.Value}")));

            if (shotPath is not null)
            {
                Render(new Views.AiKeysView { DataContext = viewModel, Width = 1440, Height = 900 },
                    shotPath);
                Check("the AI & keys page renders to a PNG", File.Exists(shotPath),
                    Path.GetFullPath(shotPath));
            }

            return viewModel;
        });

        // ------------------------------------ the backend still agrees afterwards
        var after = await api.KeysAsync();
        Check("running Test all does not strike healthy keys off",
            after.Healthy == SyntheticKeys.Length && after.Invalid == 0,
            $"healthy={after.Healthy} invalid={after.Invalid} cooldown={after.Cooldown}");
        Check("the page and the backend report the same keys",
            after.Total == page.Keys.Count, $"backend={after.Total} page={page.Keys.Count}");

        // A single-key test through the same page, so the other button is covered
        // by the real surface too.
        var single = await api.TestKeyAsync(after.Keys[0].Fingerprint!);
        Check("testing one key on its own answers honestly",
            single.Fingerprint == after.Keys[0].Fingerprint && single.Message is not null,
            $"status={single.Status} auth_ok={single.AuthOk} generate_ok={single.GenerateOk}");

        return results;
    }

    /// <summary>Runs an async body on a real STA thread with a pumping dispatcher.
    /// A WPF page cannot be exercised without one: await continuations and the
    /// view model's own UI posts both need the queue to be drained.</summary>
    private static async Task<T> OnUiThreadAsync<T>(Func<Task<T>> body)
    {
        var result = default(T);
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            var dispatcher = Dispatcher.CurrentDispatcher;
            var frame = new DispatcherFrame();
            _ = dispatcher.InvokeAsync(async () =>
            {
                try
                {
                    result = await body();
                }
                catch (Exception ex)
                {
                    failure = ex;
                }
                finally
                {
                    frame.Continue = false;
                }
            });
            Dispatcher.PushFrame(frame);
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();

        while (thread.IsAlive)
        {
            await Task.Delay(50);
        }

        if (failure is not null)
        {
            throw failure;
        }

        return result!;
    }

    private static async Task WaitUntilAsync(Func<bool> condition, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (condition())
            {
                return;
            }

            await Task.Delay(50);
        }
    }

    private static void Render(FrameworkElement view, string path)
    {
        view.Measure(new Size(view.Width, view.Height));
        view.Arrange(new Rect(0, 0, view.Width, view.Height));
        view.UpdateLayout();

        var bitmap = new RenderTargetBitmap((int)view.Width, (int)view.Height, 96, 96,
            PixelFormats.Pbgra32);
        bitmap.Render(view);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bitmap));

        var full = Path.GetFullPath(path);
        Directory.CreateDirectory(Path.GetDirectoryName(full)!);
        using var stream = File.Create(full);
        encoder.Save(stream);
    }

    private static int Count(JsonElement payload, string property) =>
        payload.ValueKind == JsonValueKind.Object
        && payload.TryGetProperty(property, out var value)
        && value.ValueKind == JsonValueKind.Array
            ? value.GetArrayLength()
            : 0;

    private sealed class NoDialogs : IDialogService
    {
        public bool Confirm(string title, string message) => false;

        public void Info(string title, string message)
        {
        }

        public void Error(string title, string message) => Console.Error.WriteLine(message);

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

/// <summary>One end-to-end assertion, reported by the caller that owns the counter.</summary>
public sealed record CheckResult(string Name, bool Ok, string Detail);
