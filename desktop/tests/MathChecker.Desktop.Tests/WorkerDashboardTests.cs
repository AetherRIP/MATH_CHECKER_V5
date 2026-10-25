using System.Net;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Models;
using MathChecker.Desktop.ViewModels;
using MathChecker.Desktop.Views;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The Correction monitor is its own page: one card per real correction job.
/// These tests pin the two properties that make it honest — every value comes
/// from the backend row, and a job that is queued, finished or failed is never
/// shown as something it is not — plus the merge rules that keep cards updating
/// in place when updates arrive out of order.
/// </summary>
[Collection(WpfCollection.Name)]
public class WorkerDashboardTests
{
    private static JobDto Job(
        string id = "job-1",
        string status = "MARKING",
        string? worker = "W3",
        string? paper = "Mathematics Paper 2.pdf",
        int? pages = 15,
        double progress = 0.42,
        string stage = "Evaluating question 5(b)",
        string? name = "TEST 2",
        string? error = null,
        double? created = 1_792_931_800,
        double? started = 1_792_931_865,
        double? updated = 1_792_931_900,
        double? finished = null) => new()
    {
        Id = id,
        StudentName = name,
        Status = status,
        WorkerId = worker,
        PaperName = paper,
        PageCount = pages,
        Progress = progress,
        StageDetail = stage,
        Error = error,
        CreatedAt = created,
        StartedAt = started,
        UpdatedAt = updated,
        FinishedAt = finished,
        Attempts = 1,
        MaxAttempts = 3,
    };

    // ------------------------------------------------------------ card fields

    [Fact]
    public void A_card_shows_the_backend_row_verbatim()
    {
        var card = new WorkerCardViewModel(Job());

        Assert.Equal("TEST 2", card.StudentName);
        Assert.Equal("Worker 03", card.WorkerLabel);
        Assert.Equal("Mathematics Paper 2.pdf", card.PaperText);
        Assert.Equal("15 pages", card.PagesText);
        Assert.Equal("Marking", card.StatusText);
        Assert.Equal("Evaluating student answers", card.PhaseText);
        Assert.Equal("42%", card.ProgressText);
        Assert.False(card.IsIndeterminate);
        Assert.Equal("Question 5(b)", card.QuestionText);
        Assert.True(card.IsActive);
        Assert.Equal("active", card.Tone);
        Assert.StartsWith("JOB-1".ToLowerInvariant(), card.JobIdText);
    }

    [Fact]
    public void Unknown_details_are_reported_as_not_available_not_invented()
    {
        var card = new WorkerCardViewModel(Job(worker: null, paper: null, pages: null,
            status: "QUEUED", progress: 0, created: null, started: null, updated: null,
            stage: null));

        Assert.Equal("Not available", card.PaperText);
        Assert.Equal("Not available", card.PagesText);
        Assert.Equal("Not assigned", card.WorkerLabel);
        Assert.Equal("Not available", card.ElapsedText);
        Assert.Equal("TEST 2", card.StudentName);
    }

    [Fact]
    public void A_queued_job_says_it_is_waiting_and_shows_no_moving_bar()
    {
        var card = new WorkerCardViewModel(Job(status: "QUEUED", worker: null,
            progress: 0, stage: null));

        Assert.True(card.IsQueued);
        Assert.Equal("queued", card.Tone);
        Assert.Equal("Waiting", card.ProgressText);
        Assert.False(card.IsIndeterminate);
        Assert.Equal("Waiting for a worker", card.PhaseText);
    }

    [Fact]
    public void A_running_job_that_has_measured_nothing_shows_activity_not_zero()
    {
        var card = new WorkerCardViewModel(Job(status: "PREPARING", progress: 0,
            stage: "Preparing the PDFs"));

        Assert.True(card.IsIndeterminate);
        Assert.Equal("Working", card.ProgressText);
    }

    [Fact]
    public void A_finished_job_reports_completion_and_a_score_slot()
    {
        var card = new WorkerCardViewModel(Job(status: "COMPLETED", progress: 1,
            finished: 1_792_932_233, updated: 1_792_932_233));
        card.Job.ResultId = "abcdef123456";

        Assert.True(card.IsCompleted);
        Assert.Equal("done", card.Tone);
        Assert.Equal("100%", card.ProgressText);
        Assert.False(card.IsIndeterminate);
        Assert.StartsWith("Finished", card.FinishedText);
    }

    [Fact]
    public void A_failed_job_carries_the_backends_error()
    {
        var card = new WorkerCardViewModel(Job(status: "FAILED", progress: 0.6,
            error: "All configured Gemini models failed: 503 UNAVAILABLE",
            finished: 1_792_932_233));

        Assert.True(card.IsFailed);
        Assert.Equal("error", card.Tone);
        Assert.True(card.HasError);
        Assert.Contains("503 UNAVAILABLE", card.ErrorText);
    }

    [Fact]
    public void Marks_are_only_reported_for_a_completed_job()
    {
        var running = Job(status: "MARKING");
        running.Result = JsonDocument.Parse("{\"earned\":75,\"available\":103,\"percentage\":72.8}")
            .RootElement.Clone();
        Assert.Equal(string.Empty, running.MarksText);

        var done = Job(status: "COMPLETED");
        done.Result = running.Result;
        Assert.Equal("75 / 103 · 72.8%", done.MarksText);
    }

    // ------------------------------------------------------------- live merge

    [Fact]
    public async Task Updates_change_the_same_card_instead_of_replacing_it()
    {
        var page = new WorkersViewModel(await SessionAsync());
        page.ApplyJobs(new[] { Job() });
        var card = Assert.Single(page.Cards);

        page.ApplyJobs(new[] { Job(progress: 0.75, stage: "Marking with gemini-3.6-flash",
            updated: 1_792_931_950) });

        Assert.Single(page.Cards);
        Assert.Same(card, page.Cards[0]);
        Assert.Equal(0.75, card.Progress);
        Assert.Equal("Marking with gemini-3.6-flash", card.StageText);
    }

    [Fact]
    public async Task An_older_update_cannot_rewind_a_card()
    {
        var page = new WorkersViewModel(await SessionAsync());
        page.ApplyJobs(new[] { Job(progress: 0.75, updated: 1_792_931_950) });
        var card = page.Cards[0];

        page.ApplyJobs(new[] { Job(progress: 0.10, updated: 1_792_931_900) });

        Assert.Equal(0.75, card.Progress);
    }

    [Fact]
    public async Task A_late_stage_event_cannot_reopen_a_finished_job()
    {
        var page = new WorkersViewModel(await SessionAsync());
        page.ApplyJobs(new[] { Job(status: "COMPLETED", progress: 1,
            finished: 1_792_932_233, updated: 1_792_932_233) });

        page.ApplyJobs(new[] { Job(status: "MARKING", progress: 0.5,
            updated: 1_792_932_500) });

        Assert.True(page.Cards[0].IsCompleted);
        Assert.Equal("100%", page.Cards[0].ProgressText);
    }

    [Fact]
    public async Task Two_jobs_make_two_cards_and_the_summary_counts_them()
    {
        var page = new WorkersViewModel(await SessionAsync());
        page.ApplyJobs(new[]
        {
            Job(id: "a", status: "MARKING", name: "TEST 2"),
            Job(id: "b", status: "QUEUED", name: "TEST 3", worker: null, progress: 0),
            Job(id: "c", status: "COMPLETED", name: "TEST 4", progress: 1,
                finished: 1_792_932_233, updated: 1_792_932_233),
        });

        Assert.Equal(3, page.Cards.Count);
        Assert.Equal(3, page.TotalCount);
        Assert.Equal(1, page.ProcessingCount);
        Assert.Equal(1, page.QueuedCount);
        Assert.Equal(1, page.CompletedCount);
        Assert.Equal(0, page.FailedCount);
    }

    [Fact]
    public async Task Filters_and_search_narrow_the_grid_without_losing_cards()
    {
        var page = new WorkersViewModel(await SessionAsync());
        page.ApplyJobs(new[]
        {
            Job(id: "a", status: "MARKING", name: "TEST 2", paper: "Algebra Paper 2.pdf"),
            Job(id: "b", status: "QUEUED", name: "TEST 3", paper: "Geometry Paper 1.pdf",
                worker: null, progress: 0),
            Job(id: "c", status: "FAILED", name: "TEST 4", paper: "Statistics Paper 3.pdf",
                error: "quota", progress: 0.2),
        });

        page.Filter = "queued";
        Assert.Equal("TEST 3", Assert.Single(page.Cards).StudentName);

        page.Filter = "all";
        page.SearchText = "paper 2";
        Assert.Equal("TEST 2", Assert.Single(page.Cards).StudentName);
        Assert.Equal(3, page.TotalCount);

        page.SearchText = string.Empty;
        Assert.Equal(3, page.Cards.Count);
    }

    [Fact]
    public async Task Selecting_a_card_shows_the_stage_history_the_backend_recorded()
    {
        var body = """
        {
          "id": "job-1", "student_name": "TEST 2", "status": "MARKING",
          "stage_detail": "Evaluating question 5(b)", "progress": 0.42,
          "worker_id": "W3", "paper_name": "Mathematics Paper 2.pdf", "page_count": 15,
          "attempts": 1, "max_attempts": 3, "created_at": 1792931800,
          "started_at": 1792931865, "updated_at": 1792931900,
          "stage_history": [
            {"status": "QUEUED", "stage": "Queued", "progress": 0, "at": 1792931800},
            {"status": "MARKING", "stage": "Evaluating question 5(b)", "progress": 0.42, "at": 1792931900}
          ]
        }
        """;
        var page = new WorkersViewModel(await SessionAsync(body));
        page.ApplyJobs(new[] { Job() });

        await page.SelectAsync(page.Cards[0]);

        Assert.True(page.HasSelection);
        Assert.Contains(page.DetailFacts, f => f.Label == "Worker" && f.Value == "Worker 03");
        Assert.Contains(page.DetailFacts, f => f.Label == "Paper" && f.Value == "Mathematics Paper 2.pdf");
        Assert.Equal(2, page.DetailStages.Count);
        Assert.Equal("Queued", page.DetailStages[0].StageText);
        Assert.Equal("Evaluating question 5(b)", page.DetailStages[1].StageText);
        Assert.True(page.HasStages);
    }

    [Fact]
    public void A_provider_cooldown_is_reported_rather_than_hiding_the_queue()
    {
        var state = new WorkerStateDto
        {
            Requested = 6,
            Running = 6,
            Provider = JsonDocument.Parse(
                "{\"available\":false,\"cooldown_remaining\":42.4}").RootElement.Clone(),
        };

        Assert.True(state.ProviderPaused);
        // The backend reports 42.4 s remaining; the chip rounds UP so it never
        // promises the queue will move before the cooldown really ends.
        Assert.Contains("43s", state.ProviderText);
        Assert.Contains("paused", state.ProviderText);

        var healthy = new WorkerStateDto
        {
            Provider = JsonDocument.Parse("{\"available\":true}").RootElement.Clone(),
        };
        Assert.False(healthy.ProviderPaused);
        Assert.Equal(string.Empty, healthy.ProviderText);
    }

    // ------------------------------------------------------------ rendering

    [Fact]
    public void The_dashboard_renders_live_cards_and_the_details_panel()
    {
        RunOnSta(() =>
        {
            var page = new WorkersViewModel(SessionAsync().GetAwaiter().GetResult());
            page.ApplyJobs(new[]
            {
                Job(id: "a", name: "TEST 2"),
                Job(id: "b", status: "QUEUED", name: "TEST 3", paper: "Geometry Paper 1.pdf",
                    worker: null, progress: 0),
                Job(id: "c", status: "FAILED", name: "TEST 4", paper: "Statistics Paper 3.pdf",
                    error: "503 UNAVAILABLE", progress: 0.2),
            });
            page.SelectAsync(page.Cards[0]).GetAwaiter().GetResult();

            var view = new WorkersView { DataContext = page, Width = 1280, Height = 800 };
            view.Measure(new Size(1280, 800));
            view.Arrange(new Rect(0, 0, 1280, 800));
            view.UpdateLayout();

            var text = DescendantText(view);
            Assert.Contains("TEST 2", text);
            Assert.Contains("Worker 03", text);
            Assert.Contains("Mathematics Paper 2.pdf", text);
            Assert.Contains("TEST 3", text);
            Assert.Contains("Waiting for a worker", text);
            Assert.Contains("503 UNAVAILABLE", text);
            Assert.Contains("WORKER DETAILS", text);
        });
    }

    [Theory]
    [InlineData(1200, 12, 3)]
    [InlineData(1013, 12, 3)]
    [InlineData(700, 12, 2)]
    [InlineData(660, 12, 2)]
    [InlineData(650, 12, 1)]
    [InlineData(500, 12, 1)]
    public void The_grid_fits_as_many_cards_per_row_as_the_width_allows(double width, int cards, int columns)
    {
        var (fitted, itemWidth) = Core.UI.ResponsiveGridPanel.Fit(width, cards, 320, 14);

        Assert.Equal(columns, fitted);
        Assert.True(itemWidth >= 320, $"a card was squeezed to {itemWidth}");
    }

    [Fact]
    public void The_grid_never_invents_columns_it_does_not_have_cards_for()
    {
        var (columns, itemWidth) = Core.UI.ResponsiveGridPanel.Fit(1440, 2, 320, 14);

        Assert.Equal(2, columns);
        Assert.Equal(713, itemWidth);
    }

    [Fact]
    public void The_monitor_is_a_page_of_its_own()
    {
        var page = new WorkersViewModel(SessionAsync().GetAwaiter().GetResult());

        Assert.Equal("workers", page.Key);
        Assert.Equal("Correction monitor", page.Title);
        Assert.Equal("Correct", page.Group);
    }

    [Fact]
    public void The_correction_page_no_longer_embeds_the_worker_dashboard()
    {
        RunOnSta(() =>
        {
            var page = new CorrectionViewModel(SessionAsync().GetAwaiter().GetResult());

            var view = new CorrectionView { DataContext = page, Width = 1280, Height = 800 };
            view.Measure(new Size(1280, 800));
            view.Arrange(new Rect(0, 0, 1280, 800));
            view.UpdateLayout();

            var text = DescendantText(view);
            Assert.Contains("NEW CORRECTION", text);
            Assert.Contains("FOLLOW THE CORRECTION", text);
            Assert.DoesNotContain("WORKER DETAILS", text);
            Assert.DoesNotContain("PROCESSING", text);
        });
    }

    [Fact]
    public void The_monitor_uses_the_window_width_for_several_cards_per_row()
    {
        RunOnSta(() =>
        {
            var page = new WorkersViewModel(SessionAsync().GetAwaiter().GetResult());
            page.ApplyJobs(new[]
            {
                Job(id: "a", name: "TEST 2"),
                Job(id: "b", name: "TEST 3"),
                Job(id: "c", status: "QUEUED", name: "TEST 4", worker: null, progress: 0),
            });

            var view = new WorkersView { DataContext = page, Width = 1440, Height = 900 };
            view.Measure(new Size(1440, 900));
            view.Arrange(new Rect(0, 0, 1440, 900));
            view.UpdateLayout();

            var cards = CardPositions(view);
            Assert.Equal(3, cards.Count);

            // Same top edge and different left edges: the cards really are
            // side by side, not stacked in one narrow column.
            var firstRow = cards.Where(c => Math.Abs(c.Y - cards[0].Y) < 1).ToList();
            Assert.True(firstRow.Count >= 2, "the monitor stacked every card in one column");
            Assert.True(cards.Max(c => c.X) > 360, "the grid did not use the window width");
        });
    }

    private static List<(double X, double Y)> CardPositions(DependencyObject root)
    {
        var positions = new List<(double X, double Y)>();
        Walk(root);
        return positions;

        void Walk(DependencyObject node)
        {
            if (node is Button { DataContext: WorkerCardViewModel }
                && node is Visual visual
                && root is Visual origin)
            {
                var point = visual.TransformToAncestor(origin).Transform(new Point(0, 0));
                positions.Add((point.X, point.Y));
            }

            var count = System.Windows.Media.VisualTreeHelper.GetChildrenCount(node);
            for (var i = 0; i < count; i++)
            {
                Walk(System.Windows.Media.VisualTreeHelper.GetChild(node, i));
            }
        }
    }

    private static string DescendantText(DependencyObject root)
    {
        var texts = new List<string>();
        Walk(root, texts);
        return string.Join("\n", texts);

        static void Walk(DependencyObject node, List<string> output)
        {
            if (node is TextBlock block)
            {
                output.Add(block.Text);
            }

            var count = System.Windows.Media.VisualTreeHelper.GetChildrenCount(node);
            for (var i = 0; i < count; i++)
            {
                Walk(System.Windows.Media.VisualTreeHelper.GetChild(node, i), output);
            }
        }
    }

    private static void RunOnSta(Action action)
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            try
            {
                EnsureApplication();
                action();
            }
            catch (Exception ex)
            {
                failure = ex;
            }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();

        if (failure is not null)
        {
            throw new Xunit.Sdk.XunitException(failure.ToString());
        }
    }

    /// <summary>Reuses the real App.xaml resources so the render test exercises
    /// the same brushes, templates and converters the application loads.</summary>
    private static void EnsureApplication()
    {
        if (Application.Current is MathChecker.Desktop.App)
        {
            return;
        }

        var app = new MathChecker.Desktop.App();
        app.InitializeComponent();
    }

    // ---------------------------------------------------------------- helpers

    private static async Task<AppSession> SessionAsync(string? jobDetailJson = null)
    {
        var handler = new StubHttpHandler();
        if (jobDetailJson is not null)
        {
            handler.Respond(HttpStatusCode.OK, jobDetailJson);
        }

        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        await host.StartAsync();

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        return new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs());
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
