using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Core.Workspace;
using MathChecker.Desktop.Models;
using MathChecker.Desktop.ViewModels;
using MathChecker.Desktop.Views;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// A question with subquestions must reach the teacher part by part.
///
/// The engine already stores one row per declared subquestion (with its own
/// marks and MARKED/UNMARKED state); these tests pin that the desktop reads
/// those rows as they are and renders them, instead of showing only the parent
/// total. An UNMARKED part is never presented as a confirmed zero.
/// </summary>
[Collection(WpfCollection.Name)]
public class SubquestionDisplayTests
{
    private const string PartsJson = """
        {
          "id":"r-sub","job_id":"j1","student_name":"TEST 2","display_name":"TEST 2",
          "class_label":"10C","earned":2,"available":5,"percentage":40.0,
          "provisional_grade":"4","confidence":"HIGH","review_required":0,"manual_override":0,
          "created_at":1792941724.0,
          "result_json":{"questions":[
            {"question_number":"7","max_marks":5,"awarded_marks":2,
             "topic":"Percentage Increase","status":"VALID",
             "student_answer":"16125 | (blank)",
             "loss_reason":"Part (b) was left blank.",
             "correct_method":"15000 * 1.075 = 16125",
             "subquestion_marks":2,
             "subquestions":[
               {"id":"7a","label":"7a","parent_id":"7","max_marks":2,"awarded_marks":2,
                "status":"MARKED","answers":["16125"],"mark_codes":["M1","A1"]},
               {"id":"7b","label":"7b","parent_id":"7","max_marks":3,"awarded_marks":0,
                "status":"UNMARKED","answers":[],"mark_codes":[]}
             ]}
          ]},
          "verification_json":{"verified":true,"status":"VALID",
            "coverage":{"expected":5,"marked":2,"requires_review":0,"unanswered":3}}
        }
        """;

    private const string DetailJson = """
        {
          "id":"r-sub","student_name":"TEST 2","display_name":"TEST 2",
          "class_label":"10C","earned":2,"available":8,"percentage":25.0,
          "provisional_grade":"3","confidence":"HIGH","created_at":1792941724.0,
          "result_json":{"questions":[
            {"question_number":"7","max_marks":5,"awarded_marks":2,"topic":"Percentages",
             "status":"VALID",
             "subquestions":[
               {"id":"7a","label":"7a","max_marks":2,"awarded_marks":2,"status":"MARKED"},
               {"id":"7b","label":"7b","max_marks":3,"awarded_marks":0,"status":"UNMARKED"}
             ]},
            {"question_number":"8","max_marks":3,"awarded_marks":3,"topic":"Angles",
             "status":"VALID"}
          ]},
          "verification_json":{},
          "created_at":1792941724.0
        }
        """;

    [Fact]
    public void The_dto_reads_each_part_with_its_own_mark_and_state()
    {
        var detail = JsonSerializer.Deserialize<ResultDetailDto>(PartsJson, Json.Options)!;
        var question = Assert.Single(detail.Questions());

        Assert.Equal(2, question.Subquestions.Count);

        var part = question.Subquestions[0];
        Assert.Equal("7a", part.LabelText);
        Assert.Equal("2 / 2", part.MarkText);
        Assert.Equal("Full marks", part.VerdictText);
        Assert.Equal("ok", part.Tone);
        Assert.True(part.HasAnswers);
        Assert.Equal("16125", part.AnswersText);
        Assert.Equal("M1, A1", part.MarkCodesText);

        var unmarked = question.Subquestions[1];
        Assert.True(unmarked.IsUnmarked);
        Assert.Equal("—", unmarked.MarkText);
        Assert.Equal("Unmarked", unmarked.VerdictText);
        // The scheme declared 3 marks; the marker returned nothing for it, so
        // the part is unknown rather than a confirmed zero.
        Assert.Equal(3d, unmarked.MaxMarks!.Value, 3);
        Assert.Equal("Not stated in the scheme", unmarked.AnswersText);
    }

    [Fact]
    public async Task The_desk_question_carries_every_part_and_its_summary()
    {
        using var handler = new DeskHandler { Detail = PartsJson };
        var desk = await OpenDeskAsync(handler);

        var question = Assert.Single(desk.Questions);
        Assert.True(question.HasSubquestions);
        Assert.Equal(2, question.Subquestions.Count);
        Assert.Equal("2 parts · 1 marked, 1 unmarked", question.SubquestionSummary);
        Assert.Equal("2 / 5", question.MarkText);

        // The parent keeps its own mark; the parts do not replace it.
        Assert.Equal(2, question.Awarded);
        Assert.Equal(5, question.Max);
    }

    [Fact]
    public async Task A_question_without_parts_says_nothing_about_subquestions()
    {
        using var handler = new DeskHandler();
        var desk = await OpenDeskAsync(handler);

        Assert.All(desk.Questions, question => Assert.False(question.HasSubquestions));
        Assert.All(desk.Questions, question => Assert.Equal(string.Empty, question.SubquestionSummary));
    }

    [Fact]
    public void The_desk_renders_each_subquestion_beside_its_parent()
    {
        RunOnSta(() =>
        {
            using var handler = new DeskHandler { Detail = PartsJson };
            var desk = OpenDeskAsync(handler).GetAwaiter().GetResult();

            var view = new MarkingDeskView { DataContext = desk, Width = 1400, Height = 900 };
            view.Measure(new Size(1400, 900));
            view.Arrange(new Rect(0, 0, 1400, 900));
            view.UpdateLayout();

            var text = DescendantText(view);
            Assert.Contains("Subquestions", text);
            Assert.Contains("2 parts · 1 marked, 1 unmarked", text);
            Assert.Contains("7a", text);
            Assert.Contains("2 / 2", text);
            Assert.Contains("Full marks", text);
            Assert.Contains("Scheme answer: 16125", text);
            Assert.Contains("Marking points: M1, A1", text);
            Assert.Contains("7b", text);
            Assert.Contains("Unmarked", text);
            // The parent total is still shown, and still 2 / 5.
            Assert.Contains("2 / 5", text);
        });
    }

    [Fact]
    public void A_question_with_no_parts_renders_no_empty_section()
    {
        RunOnSta(() =>
        {
            using var handler = new DeskHandler();
            var desk = OpenDeskAsync(handler).GetAwaiter().GetResult();

            var view = new MarkingDeskView { DataContext = desk, Width = 1400, Height = 900 };
            view.Measure(new Size(1400, 900));
            view.Arrange(new Rect(0, 0, 1400, 900));
            view.UpdateLayout();

            Assert.DoesNotContain("Subquestions", DescendantText(view));
        });
    }

    [Fact]
    public async Task The_results_page_lists_the_parts_of_the_selected_question()
    {
        using var handler = new ResultsHandler();
        var page = await OpenResultsAsync(handler);

        page.SelectedQuestion = page.Questions.First(question => question.NumberText == "7");

        RunOnSta(() =>
        {
            var view = new ResultsView { DataContext = page, Width = 1400, Height = 900 };
            view.Measure(new Size(1400, 900));
            view.Arrange(new Rect(0, 0, 1400, 900));
            view.UpdateLayout();

            var text = DescendantText(view);
            Assert.Contains("Subquestions", text);
            Assert.Contains("7a", text);
            Assert.Contains("2 / 2", text);
            Assert.Contains("7b", text);
            Assert.Contains("Unmarked", text);
        });
    }

    [Fact]
    public async Task The_results_page_shows_no_parts_for_a_question_that_has_none()
    {
        using var handler = new ResultsHandler();
        var page = await OpenResultsAsync(handler);

        page.SelectedQuestion = page.Questions.First(question => question.NumberText == "8");

        RunOnSta(() =>
        {
            var view = new ResultsView { DataContext = page, Width = 1400, Height = 900 };
            view.Measure(new Size(1400, 900));
            view.Arrange(new Rect(0, 0, 1400, 900));
            view.UpdateLayout();

            Assert.DoesNotContain("Subquestions", DescendantText(view));
        });
    }

    [Fact]
    public async Task The_parts_section_follows_the_selection_in_both_directions()
    {
        using var handler = new ResultsHandler();
        var page = await OpenResultsAsync(handler);
        var withParts = page.Questions.First(question => question.NumberText == "7");
        var withoutParts = page.Questions.First(question => question.NumberText == "8");

        RunOnSta(() =>
        {
            var view = new ResultsView { DataContext = page, Width = 1400, Height = 900 };
            Layout(view);

            page.SelectedQuestion = withParts;
            Layout(view);
            Assert.Contains("Subquestions", DescendantText(view));
            Assert.Contains("7b", DescendantText(view));

            // Moving to a question with no parts must take the section away
            // again, not leave the previous question's heading behind.
            page.SelectedQuestion = withoutParts;
            Layout(view);
            Assert.DoesNotContain("Subquestions", DescendantText(view));
        });
    }

    private static void Layout(FrameworkElement view)
    {
        view.Measure(new Size(view.Width, view.Height));
        view.Arrange(new Rect(0, 0, view.Width, view.Height));
        view.UpdateLayout();
    }

    // ---------------------------------------------------------------- helpers

    private static async Task<ResultsViewModel> OpenResultsAsync(ResultsHandler handler)
    {
        var page = new ResultsViewModel(await SessionAsync(handler));
        await page.LoadAsync();
        Assert.Null(page.Error);
        Assert.Single(page.Items);

        page.Selected = page.Items[0];
        // The detail is fetched when the row is chosen; wait for that load
        // rather than racing it.
        for (var attempt = 0; attempt < 100 && page.Questions.Count == 0; attempt++)
        {
            await Task.Delay(20);
        }

        Assert.NotEmpty(page.Questions);
        return page;
    }

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

    /// <summary>The three routes the Results page reads, by URL.</summary>
    private sealed class ResultsHandler : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;
            if (path.EndsWith("/results", StringComparison.Ordinal))
            {
                return Task.FromResult(PageHandler.Json("""
                    {"results":[{"id":"r-sub","student_name":"TEST 2","display_name":"TEST 2",
                      "earned":2,"available":5,"percentage":40.0,"created_at":1792941724.0}],
                     "total":1,"limit":300,"offset":0}
                    """));
            }

            if (path.EndsWith("/versions", StringComparison.Ordinal))
            {
                return Task.FromResult(PageHandler.Json("""{"versions":[]}"""));
            }

            if (path.Contains("/results/", StringComparison.Ordinal))
            {
                return Task.FromResult(PageHandler.Json(DetailJson));
            }

            return Task.FromResult(PageHandler.Error(System.Net.HttpStatusCode.NotFound,
                "unexpected_url", "No stub for " + path));
        }
    }

    private static async Task<MarkingDeskViewModel> OpenDeskAsync(DeskHandler handler)
    {
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        var session = new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs());
        var desk = new MarkingDeskViewModel(session, new FakeDecoder());
        await desk.OpenAsync("r-sub");
        Assert.Null(desk.Error);
        return desk;
    }

    /// <summary>
    /// Text a teacher can actually read: collapsed subtrees are skipped, and a
    /// TextBlock built from Runs ("Scheme answer: …") is read as its content,
    /// not its empty Text property.
    /// </summary>
    private static string DescendantText(DependencyObject root)
    {
        var texts = new List<string>();
        Walk(root, texts);
        return string.Join("\n", texts);

        static void Walk(DependencyObject node, List<string> output)
        {
            if (node is UIElement { Visibility: not Visibility.Visible })
            {
                return;
            }

            if (node is TextBlock block)
            {
                var text = block.Text;
                if (string.IsNullOrEmpty(text) && block.Inlines.Count > 0)
                {
                    text = new System.Windows.Documents.TextRange(
                        block.ContentStart, block.ContentEnd).Text.Trim();
                }

                // TextRange separates inline runs with a space; collapse the
                // doubling so assertions read what the eye reads.
                output.Add(System.Text.RegularExpressions.Regex.Replace(text, "[ \t]+", " ").Trim());
            }

            var count = VisualTreeHelper.GetChildrenCount(node);
            for (var i = 0; i < count; i++)
            {
                Walk(VisualTreeHelper.GetChild(node, i), output);
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

    private static void EnsureApplication()
    {
        if (Application.Current is MathChecker.Desktop.App)
        {
            return;
        }

        var app = new MathChecker.Desktop.App();
        app.InitializeComponent();
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
