using System.Net;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The Correct page is the local (no-Classroom) AI correction entry point.
/// These tests pin the plumbing that made its "Start correction" button stay
/// disabled after both files were chosen: the bound command instance must be
/// the one that is told to re-evaluate.
/// </summary>
public class CorrectionPageTests
{
    [Fact]
    public async Task The_submit_command_is_one_stable_instance()
    {
        var page = new CorrectionViewModel(await SessionAsync());

        Assert.Same(page.SubmitCommand, page.SubmitCommand);
    }

    [Fact]
    public async Task Choosing_both_files_enables_submit_and_notifies_the_binding()
    {
        using var paper = new TempFile(".pdf", "%PDF-1.4 synthetic");
        using var scheme = new TempFile(".pdf", "%PDF-1.4 synthetic");
        var page = new CorrectionViewModel(await SessionAsync());
        var notified = 0;
        page.SubmitCommand.CanExecuteChanged += (_, _) => notified++;

        Assert.False(page.SubmitCommand.CanExecute(null));

        page.SetSubmission(paper.Path);
        Assert.False(page.SubmitCommand.CanExecute(null));

        page.SetMarkScheme(scheme.Path);

        Assert.True(page.CanSubmit);
        Assert.True(page.SubmitCommand.CanExecute(null));
        Assert.True(notified > 0, "the bound command was never told its CanExecute changed");
    }

    [Fact]
    public async Task An_unsupported_file_is_refused_before_an_upload_happens()
    {
        using var paper = new TempFile(".exe", "not a paper");
        using var scheme = new TempFile(".pdf", "%PDF-1.4 synthetic");
        var page = new CorrectionViewModel(await SessionAsync());
        page.SetSubmission(paper.Path);
        page.SetMarkScheme(scheme.Path);

        // CanSubmit is deliberately permissive (file checks run on execute), so
        // the command reports the real problem instead of uploading it.
        page.SubmitCommand.Execute(null);

        Assert.NotNull(page.Error);
        Assert.Contains("Unsupported file type", page.Error);
    }

    private static async Task<AppSession> SessionAsync()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, "{}");
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        await host.StartAsync();

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        return new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs());
    }

    private sealed class TempFile : IDisposable
    {
        public TempFile(string extension, string content)
        {
            Path = System.IO.Path.Combine(
                System.IO.Path.GetTempPath(), $"mc5-test-{Guid.NewGuid():N}{extension}");
            File.WriteAllText(Path, content);
        }

        public string Path { get; }

        public void Dispose()
        {
            try
            {
                File.Delete(Path);
            }
            catch (IOException)
            {
                // Temp cleanup is best effort.
            }
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
}
