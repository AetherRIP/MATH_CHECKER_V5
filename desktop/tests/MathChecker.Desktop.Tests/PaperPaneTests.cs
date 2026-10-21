using System.Net;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Core.Workspace;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

public class PaperPaneTests
{
    [Fact]
    public async Task A_missing_paper_shows_the_backends_reason_and_offers_settings()
    {
        const string message =
            "Submission retention is OFF, so the original is deleted when a correction finishes.";
        var handler = new PageHandler
        {
            Metadata = PageHandler.Missing("retention_disabled", message),
        };
        var (session, _) = await SessionAsync(handler);
        var paper = Paper(session);
        string? navigated = null;
        session.Navigate = key => navigated = key;

        await paper.OpenAsync("r1");

        Assert.True(paper.IsMissing);
        Assert.False(paper.IsAvailable);
        Assert.Equal("retention_disabled", paper.Reason);
        Assert.Equal("The student's work is not stored here", paper.MissingTitle);
        Assert.Equal(message, paper.MissingBody);
        Assert.Equal("Retention settings", paper.MissingActionLabel);
        Assert.True(paper.HasMissingAction);
        Assert.Equal(0, handler.ImageRequests);

        paper.MissingActionCommand.Execute(null);
        Assert.Equal("privacy", navigated);
    }

    [Theory]
    [InlineData("retention_disabled", "Retention settings")]
    [InlineData("file_missing", "Re-check storage")]
    [InlineData("file_unreadable", "Re-correct paper")]
    [InlineData("processing", "Check again")]
    public async Task Each_reason_offers_its_own_action(string reason, string label)
    {
        var handler = new PageHandler
        {
            Metadata = PageHandler.Missing(reason, "The backend explains this itself."),
        };
        var (session, _) = await SessionAsync(handler);
        var paper = Paper(session);

        await paper.OpenAsync("r1");

        Assert.True(paper.IsMissing);
        Assert.Equal(label, paper.MissingActionLabel);
        Assert.Equal("The backend explains this itself.", paper.MissingBody);
    }

    [Fact]
    public async Task A_paper_without_a_stored_submission_offers_no_action()
    {
        var handler = new PageHandler
        {
            Metadata = PageHandler.Missing("no_link", "This result is not linked to a stored correction job."),
        };
        var (session, _) = await SessionAsync(handler);
        var paper = Paper(session);

        await paper.OpenAsync("r1");

        Assert.True(paper.IsMissing);
        Assert.Null(paper.MissingActionLabel);
        Assert.False(paper.HasMissingAction);
        Assert.Equal("This result has no stored submission", paper.MissingTitle);
    }

    [Fact]
    public async Task An_available_paper_loads_page_one_and_builds_the_strip()
    {
        var handler = new PageHandler { Metadata = PageHandler.Available(3, "answers.pdf") };
        var (session, _) = await SessionAsync(handler);
        var paper = Paper(session);

        await paper.OpenAsync("r1");

        Assert.Equal(PaperPaneState.Available, paper.State);
        Assert.True(paper.IsAvailable);
        Assert.Equal(3, paper.PageCount);
        Assert.Equal(1, paper.PageIndex);
        Assert.Equal("answers.pdf · 3 pages", paper.HeaderText);
        Assert.Equal("Page 1 of 3", paper.PageLabel);
        Assert.Equal(3, paper.PageChips.Count);
        Assert.True(paper.PageChips[0].IsCurrent);
        Assert.False(paper.IsPageLoading);
        Assert.Equal(1000, paper.ImagePixelWidth);
        Assert.Equal(150, paper.RequestedDpi);
        Assert.True(paper.ShowPager);

        await paper.WaitForPrefetchAsync();
        Assert.Equal(new[] { 1, 2 }, handler.ImagePages);
    }

    [Fact]
    public async Task A_failed_page_stays_inline_and_retries_the_page_not_the_paper()
    {
        var attempts = 0;
        var handler = new PageHandler
        {
            Metadata = PageHandler.Available(3),
            Image = page =>
            {
                attempts++;
                return attempts == 1
                    ? PageHandler.Error(HttpStatusCode.InternalServerError, "page_render_failed",
                        "The page could not be rendered.")
                    : PageHandler.Png(page);
            },
        };
        var (session, _) = await SessionAsync(handler);
        var paper = Paper(session);

        await paper.OpenAsync("r1");

        Assert.True(paper.IsPageFailed);
        Assert.Contains("page_render_failed", paper.PageError);
        Assert.Equal(3, paper.PageCount);
        Assert.True(paper.ShowPager);

        paper.RetryPageCommand.Execute(null);
        await paper.PendingLoad;

        Assert.True(paper.IsAvailable);
        Assert.Null(paper.PageError);
        Assert.Equal(1000, paper.ImagePixelWidth);
        Assert.Equal(1, paper.PageIndex);
    }

    [Fact]
    public async Task A_metadata_failure_is_retryable()
    {
        var handler = new PageHandler
        {
            MetadataResponse = () => PageHandler.Error(HttpStatusCode.InternalServerError,
                "backend_error", "The marking backend is unavailable."),
        };
        var (session, _) = await SessionAsync(handler);
        var paper = Paper(session);

        await paper.OpenAsync("r1");

        Assert.True(paper.IsFailed);
        Assert.Contains("backend_error", paper.FailureMessage);

        handler.MetadataResponse = null;
        handler.Metadata = PageHandler.Available(1);
        await paper.CheckAgainAsync();

        Assert.True(paper.IsAvailable);
        Assert.Null(paper.FailureMessage);
        Assert.Equal(1, paper.PageCount);
    }

    [Fact]
    public async Task Zoom_fits_the_pane_and_fetches_a_sharper_render_only_when_needed()
    {
        var handler = new PageHandler { Metadata = PageHandler.Available(3) };
        var (session, _) = await SessionAsync(handler);
        var paper = Paper(session);
        await paper.OpenAsync("r1");

        // 1000 px at 150 dpi is 640 DIP; an 800 DIP pane fits at 125%.
        paper.SetViewportWidth(800);
        Assert.True(paper.IsFitWidth);
        Assert.Equal(125, paper.ZoomPercent);

        paper.ZoomInCommand.Execute(null);
        Assert.False(paper.IsFitWidth);
        Assert.Equal(150, paper.ZoomPercent);
        Assert.Equal(150, paper.RequestedDpi);

        paper.ZoomInCommand.Execute(null);
        Assert.Equal(175, paper.ZoomPercent);
        await paper.PendingLoad;
        Assert.Equal(220, paper.RequestedDpi);

        for (var step = 0; step < 6; step++)
        {
            paper.ZoomInCommand.Execute(null);
        }

        Assert.Equal(300, paper.ZoomPercent);
        await paper.PendingLoad;
        Assert.Equal(300, paper.RequestedDpi);

        for (var step = 0; step < 20; step++)
        {
            paper.ZoomOutCommand.Execute(null);
        }

        Assert.Equal(50, paper.ZoomPercent);
        await paper.PendingLoad;
        Assert.Equal(150, paper.RequestedDpi);
    }

    [Fact]
    public async Task Page_navigation_updates_the_strip_and_clamps_to_the_paper()
    {
        var handler = new PageHandler { Metadata = PageHandler.Available(3) };
        var (session, _) = await SessionAsync(handler);
        var paper = Paper(session);
        await paper.OpenAsync("r1");
        await paper.WaitForPrefetchAsync();

        paper.NextPageCommand.Execute(null);
        await paper.PendingLoad;

        Assert.Equal(2, paper.PageIndex);
        Assert.Equal("Page 2 of 3", paper.PageLabel);
        Assert.True(paper.PageChips[1].IsCurrent);
        Assert.False(paper.PageChips[0].IsCurrent);

        paper.GoToPageCommand.Execute(3);
        await paper.PendingLoad;

        Assert.Equal(3, paper.PageIndex);
        Assert.True(paper.PageChips[2].IsCurrent);
        Assert.Contains(3, handler.ImagePages);

        paper.GoToPageCommand.Execute(99);
        await paper.PendingLoad;

        Assert.Equal(3, paper.PageIndex);
        Assert.Equal(3, paper.PageChips.Count);
    }

    private static PaperPaneViewModel Paper(AppSession session) =>
        new(session, decoder: new FakeDecoder())
        {
            // Tests must not leave a five-second background poll running.
            AutoRefreshProcessing = false,
        };

    private static async Task<(AppSession Session, PageHandler Handler)> SessionAsync(PageHandler handler)
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

    private sealed class FakeDecoder : IPageImageDecoder
    {
        public Task<DecodedPage> DecodeAsync(byte[] bytes,
            CancellationToken cancellationToken = default) =>
            Task.FromResult(new DecodedPage(1000, 1414, null));
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
