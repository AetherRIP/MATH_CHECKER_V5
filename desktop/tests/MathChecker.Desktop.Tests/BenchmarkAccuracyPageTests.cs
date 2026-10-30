using System.Net;
using System.Text;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The desktop benchmark page is a view over the existing API, not a second
/// benchmark. These tests pin the properties that make that safe: no
/// submission on load, an explicit confirmation, one POST per click, the
/// backend's eligibility after a reload, preserved input after a refusal, and
/// cancellation of a stale detail read.
/// </summary>
public class BenchmarkAccuracyPageTests
{
    [Fact]
    public async Task The_page_reports_unavailable_without_inventing_a_score()
    {
        var handler = new BenchmarkHandler();
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(handler));

        await page.LoadAsync();

        Assert.False(page.CertifiedAvailable);
        Assert.False(page.ShowCertifiedMetrics);
        Assert.Contains("Certified accuracy unavailable. Not 0%.",
            page.CertifiedStatusText);
        Assert.Contains("has not yet been measured against teacher-verified references",
            page.CertifiedStatusText);
        Assert.Equal(0, page.VerifiedCount);
        Assert.Equal(1, page.UnverifiedCount);
        Assert.Equal(0, page.DisputedCount);
        Assert.Equal(0, page.HoldoutCount);
        Assert.Equal("v1", page.DatasetVersionText);
        Assert.Contains("1 papers", page.DatasetCompositionText);
        Assert.True(page.HasWarnings);
        Assert.Single(page.Warnings);
        Assert.Equal(0, handler.VerifyCount);
    }

    [Fact]
    public async Task Opening_a_reference_shows_three_distinct_marks_and_submits_nothing()
    {
        var handler = new BenchmarkHandler();
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(handler));

        await page.LoadAsync();
        await WaitUntil(() => page.Detail is not null, "the first reference detail");

        Assert.NotNull(page.Selected);
        Assert.Contains("Synthetic Algebra", page.DetailPaper);
        Assert.Contains("Cambridge", page.DetailBoard);
        Assert.Equal("2 / 2", page.DetailSchemeMarkText);
        Assert.Equal("1 / 2 · under by 1", page.AiMarkText);
        Assert.Equal("Not confirmed", page.DetailTeacherMarkText);
        Assert.Contains("Not eligible", page.DetailEligibility);
        Assert.Equal(3, page.SchemeLines.Count);
        Assert.Single(page.AnswerLines);
        Assert.Equal(0, handler.VerifyCount);
    }

    [Fact]
    public async Task A_mark_requires_explicit_confirmation_and_an_assessor()
    {
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(new BenchmarkHandler()));
        await page.LoadAsync();
        await WaitUntil(() => page.Detail is not null, "the reference detail");

        page.MarkText = "2";
        Assert.False(page.CanSubmit);

        page.Assessor = "A. Teacher";
        Assert.False(page.CanSubmit);

        page.IsConfirmed = true;
        Assert.True(page.CanSubmit);
        Assert.True(page.SubmitCommand.CanExecute(null));

        page.IsConfirmed = false;
        Assert.False(page.CanSubmit);
    }

    [Fact]
    public async Task Recording_posts_confirm_true_once_and_the_page_reloads_the_backend_status()
    {
        var handler = new BenchmarkHandler();
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(handler));
        await page.LoadAsync();
        await WaitUntil(() => page.Detail is not null, "the reference detail");
        Fill(page, "2", "A. Teacher", confirmed: true);

        var command = page.SubmitCommand;
        command.Execute(null);
        await WaitUntil(() => !command.IsRunning, "the assessment to finish");
        await WaitUntil(() => page.DetailIsEligible, "the refreshed eligible state");

        Assert.Equal(1, handler.VerifyCount);
        Assert.True(handler.LastConfirm);
        Assert.Equal(1, page.VerifiedCount);
        Assert.Equal(0, page.UnverifiedCount);
        Assert.Equal("2 / 2", page.DetailTeacherMarkText);
        Assert.True(page.DetailIsEligible);
        Assert.Contains("Assessment recorded", page.Status);
        Assert.False(page.IsConfirmed);
    }

    [Fact]
    public async Task A_double_click_cannot_append_two_assessments()
    {
        var handler = new BenchmarkHandler { VerifyGate = new TaskCompletionSource() };
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(handler));
        await page.LoadAsync();
        await WaitUntil(() => page.Detail is not null, "the reference detail");
        Fill(page, "2", "A. Teacher", confirmed: true);

        var command = page.SubmitCommand;
        command.Execute(null);
        await WaitUntil(() => handler.VerifyCount == 1, "the first request to start");
        command.Execute(null);
        await Task.Delay(100);
        Assert.Equal(1, handler.VerifyCount);
        Assert.False(command.CanExecute(null));

        handler.VerifyGate.SetResult();
        await WaitUntil(() => !command.IsRunning, "the assessment to finish");
        Assert.Equal(1, handler.VerifyCount);
    }

    [Fact]
    public async Task A_refused_assessment_preserves_the_teachers_input_and_records_nothing()
    {
        var handler = new BenchmarkHandler
        {
            VerifyFailure = (HttpStatusCode.BadRequest,
                """{"detail":{"code":"confirmation_required","message":"A benchmark mark must be explicitly confirmed."}}""")
        };
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(handler));
        await page.LoadAsync();
        await WaitUntil(() => page.Detail is not null, "the reference detail");
        Fill(page, "2", "A. Teacher", confirmed: true, note: "Keep this note");

        var command = page.SubmitCommand;
        command.Execute(null);
        await WaitUntil(() => !command.IsRunning, "the refused assessment");

        Assert.Equal(1, handler.VerifyCount);
        Assert.Contains("explicitly confirmed", page.Error);
        Assert.Equal("2", page.MarkText);
        Assert.Equal("A. Teacher", page.Assessor);
        Assert.Equal("Keep this note", page.Note);
        Assert.True(page.IsConfirmed);
        Assert.Equal(string.Empty, page.WorkflowStatus);
        Assert.Equal(0, page.VerifiedCount);
    }

    [Fact]
    public async Task A_dispute_requires_an_explanation_and_stays_ineligible()
    {
        var handler = new BenchmarkHandler();
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(handler));
        await page.LoadAsync();
        await WaitUntil(() => page.Detail is not null, "the reference detail");

        page.Decision = page.DecisionOptions.Single(d => d.Code == "disputed");
        page.Assessor = "A. Teacher";
        page.IsConfirmed = true;
        Assert.False(page.CanSubmit);

        page.Note = "The worked line cannot be read from the scan.";
        Assert.True(page.CanSubmit);

        var command = page.SubmitCommand;
        command.Execute(null);
        await WaitUntil(() => !command.IsRunning, "the dispute to finish");
        await WaitUntil(() => page.DisputedCount == 1, "the disputed status to reload");

        Assert.True(page.DetailIsDisputed);
        Assert.False(page.DetailIsEligible);
        Assert.True(page.HasConflict);
        Assert.Contains("preserved", page.DetailConflict);
        Assert.Equal("disputed", handler.LastStatus);
    }

    [Fact]
    public async Task An_out_of_range_mark_is_refused_by_the_page()
    {
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(new BenchmarkHandler()));
        await page.LoadAsync();
        await WaitUntil(() => page.Detail is not null, "the reference detail");

        page.Assessor = "A. Teacher";
        page.IsConfirmed = true;
        page.MarkText = "3";
        Assert.False(page.CanSubmit);
        Assert.Contains("0 to 2", page.ValidationHint);

        page.MarkText = "2.5";
        Assert.False(page.CanSubmit);
    }

    [Fact]
    public async Task A_slow_older_detail_cannot_overwrite_a_newer_selection()
    {
        var handler = new BenchmarkHandler { Ids = { "synthetic.q1", "synthetic.q2" } };
        handler.Gates["synthetic.q1"] = new TaskCompletionSource();
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(handler));

        await page.LoadAsync();
        await WaitUntil(() => page.Selected?.ReferenceId == "synthetic.q1", "the first selection");
        page.Selected = page.References.Single(r => r.ReferenceId == "synthetic.q2");
        await WaitUntil(() => page.Detail?.ReferenceId == "synthetic.q2", "the second detail");

        handler.Gates["synthetic.q1"].SetResult();
        await Task.Delay(150);
        Assert.Equal("synthetic.q2", page.Detail?.ReferenceId);
    }

    [Fact]
    public async Task Recompute_replays_the_recorded_run_without_creating_a_new_one()
    {
        var handler = new BenchmarkHandler { LatestRunJson = RunJson(reproduced: true) };
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(handler));
        await page.LoadAsync();

        Assert.True(page.HasLatestRun);
        Assert.True(page.CanReproduce);

        var command = page.ReproduceCommand;
        command.Execute(null);
        await WaitUntil(() => !command.IsRunning, "the reproduction");

        Assert.Equal(1, handler.ReproduceCount);
        Assert.Contains("PASS", page.LatestRunReproduction);
        Assert.False(page.CanReproduce);
        Assert.Equal(0, handler.VerifyCount);
    }

    [Fact]
    public async Task Metrics_are_shown_only_when_the_backend_certifies_the_run()
    {
        var handler = new BenchmarkHandler
        {
            Certified = true,
            LatestRunJson = RunJson(reproduced: true, certified: true),
            CertifiedAccuracyJson = """
                {"items":{"exact":1,"compared":1,"rate":1.0,"ci95":[0.2,1.0],
                          "denominator":"matched reference items with a valid AI mark"},
                 "questions":{"exact":1,"compared":1,"rate":1.0,"ci95":[0.2,1.0]},
                 "subquestions":{"exact":0,"compared":0,"rate":null,"ci95":null},
                 "total":{"exact":1,"compared":1,"rate":1.0,"ci95":[0.2,1.0],"scripts":1},
                 "sample":{"references_expected":1,"papers_expected":1,"marks_available":2},
                 "run_id":"run-1","finished_at":"2026-10-29T12:00:00+00:00"}
                """,
        };
        var page = new BenchmarkAccuracyViewModel(await SessionAsync(handler));
        await page.LoadAsync();

        Assert.True(page.CertifiedAvailable);
        Assert.True(page.ShowCertifiedMetrics);
        Assert.Equal("1/1 exact · 100.0% · 95% CI 20.0–100.0%", page.CertifiedItemsText);
        Assert.Contains("1/1 scripts exact", page.CertifiedTotalText);

        var uncertified = new BenchmarkHandler { LatestRunJson = RunJson(reproduced: true) };
        var other = new BenchmarkAccuracyViewModel(await SessionAsync(uncertified));
        await other.LoadAsync();
        Assert.False(other.CertifiedAvailable);
        Assert.False(other.ShowCertifiedMetrics);
    }

    // ------------------------------------------------------------------ harness

    private static void Fill(BenchmarkAccuracyViewModel page, string mark, string assessor,
        bool confirmed, string note = "")
    {
        page.MarkText = mark;
        page.Assessor = assessor;
        page.Note = note;
        page.IsConfirmed = confirmed;
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

    private static string RunJson(bool reproduced, bool certified = false) => """
        {"run_id":"run-1","status":"complete","certified":__CERT__,
         "certification":{"basis":"teacher_verified","eligible":__CERT__,
                          "reason":null},
         "started_at":"2026-10-29T11:00:00+00:00","finished_at":"2026-10-29T12:00:00+00:00",
         "mode":"stub","model":"gemini-3.6-flash","dataset_version":"1",
         "dataset_matches":true,"verification_matches":true,
         "sample":{"references_expected":1,"papers_expected":1,"marks_available":2},
         "agreement":{"items":{"exact":1,"compared":1,"rate":1.0,"ci95":[0.2,1.0]}},
         "error":{"mean_absolute_error":0.0,"normalized_mae":0.0,
                  "overmarking":{"count":0,"rate":0.0},"undermarking":{"count":0,"rate":0.0}},
         "coverage":{"items_processed":1,"item_coverage":1.0,"missing_mark_count":0},
         "review":{"review_flag_rate":0.0,"human_review_rate":0.0},
         "grades":{"compared":0,"available":false},
         "limitations":["Small sample."],
         "reproduction":{"run_id":"run-1","reproduced":__REPRODUCED__,
                         "differences":[]}}
        """.Replace("__CERT__", certified ? "true" : "false")
        .Replace("__REPRODUCED__", reproduced ? "true" : "false");

    private sealed class BenchmarkHandler : HttpMessageHandler
    {
        private readonly object _gate = new();
        private readonly List<string> _history = new();
        private bool _recorded;
        private bool _disputed;

        public List<string> Ids { get; } = new() { "synthetic.q1" };
        public Dictionary<string, TaskCompletionSource> Gates { get; } = new();
        public TaskCompletionSource? VerifyGate { get; set; }
        public (HttpStatusCode Status, string Body)? VerifyFailure { get; set; }
        public string? LatestRunJson { get; set; }
        public string? CertifiedAccuracyJson { get; set; }
        public bool Certified { get; set; }

        public int VerifyCount { get; private set; }
        public int ReproduceCount { get; private set; }
        public bool LastConfirm { get; private set; }
        public string LastStatus { get; private set; } = string.Empty;
        public List<(string Method, string Path, string? Body)> Requests { get; } = new();

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var method = request.Method.Method;
            var path = request.RequestUri!.AbsolutePath;
            var body = request.Content is null
                ? null
                : await request.Content.ReadAsStringAsync(cancellationToken);
            lock (_gate)
            {
                Requests.Add((method, path, body));
            }

            if (path.EndsWith("/benchmark/overview", StringComparison.Ordinal))
            {
                return Json(Overview());
            }

            if (path.EndsWith("/benchmark/references", StringComparison.Ordinal))
            {
                return Json(References());
            }

            if (path.Contains("/benchmark/runs/", StringComparison.Ordinal)
                && path.EndsWith("/reproduce", StringComparison.Ordinal))
            {
                ReproduceCount++;
                return Json("""{"run_id":"run-1","reproduced":true,"differences":[]}""");
            }

            if (method == "POST" && path.EndsWith("/verify", StringComparison.Ordinal))
            {
                using var document = JsonDocument.Parse(body ?? "{}");
                var root = document.RootElement;
                LastConfirm = root.TryGetProperty("confirm", out var confirm)
                              && confirm.ValueKind == JsonValueKind.True;
                LastStatus = root.TryGetProperty("status", out var status)
                    ? status.GetString() ?? string.Empty
                    : string.Empty;
                VerifyCount++;
                if (VerifyGate is not null)
                {
                    await VerifyGate.Task.WaitAsync(cancellationToken);
                }

                if (VerifyFailure is { } failure)
                {
                    return new HttpResponseMessage(failure.Status)
                    {
                        Content = new StringContent(failure.Body, Encoding.UTF8, "application/json"),
                    };
                }

                lock (_gate)
                {
                    _recorded = true;
                    _disputed = LastStatus == "disputed";
                    var awarded = root.TryGetProperty("awarded", out var mark)
                                  && mark.ValueKind == JsonValueKind.Number
                        ? mark.GetInt32()
                        : (int?)null;
                    _history.Add(JsonSerializer.Serialize(new
                    {
                        seq = _history.Count + 1,
                        reference_id = "synthetic.q1",
                        awarded,
                        status = LastStatus,
                        assessor = "A. Teacher",
                        role = "",
                        note = "Recorded by the test.",
                        alternative_methods = Array.Empty<string>(),
                        created_at = "2026-10-29T12:00:00+00:00",
                        dataset_version = "1",
                    }));
                }

                return Json("""
                    {"reference_id":"synthetic.q1","reference":{},"verification":{},
                     "production_db_modified":false,"classroom_writes":0}
                    """);
            }

            const string marker = "/benchmark/references/";
            var index = path.IndexOf(marker, StringComparison.Ordinal);
            if (method == "GET" && index >= 0)
            {
                var id = Uri.UnescapeDataString(path[(index + marker.Length)..]);
                if (Gates.TryGetValue(id, out var gate))
                {
                    await gate.Task.WaitAsync(cancellationToken);
                }

                return Json(ReferenceDetail(id));
            }

            return new HttpResponseMessage(HttpStatusCode.NotFound)
            {
                Content = new StringContent("""{"code":"not_found","message":"No stub"}""",
                    Encoding.UTF8, "application/json"),
            };
        }

        private string Overview()
        {
            var status = _recorded ? (_disputed ? "disputed" : "verified") : "unverified";
            var eligible = _recorded && !_disputed;
            var accuracy = CertifiedAccuracyJson ?? "null";
            var run = LatestRunJson ?? "null";
            return """
                {"dataset":{"version":"1","sha256":"0123456789abcdef0123456789abcdef",
                            "paper_count":1,"item_count":1,"question_count":1,"subquestion_count":0,
                            "marks_available":2,"scheme_marks":2,
                            "partitions":{"development":1,"holdout":0},
                            "provenance":{"teacher_verified":0,"official_reference":1,
                                          "ai_generated":0,"unverified":0},
                            "warnings":["no_holdout_partition: no independent estimate"]},
                 "verification":{"total_references":1,
                                 "counts":{"__STATUS__":1},
                                 "eligible":__ELIGIBLE__,
                                 "eligible_questions":__ELIGIBLE__,
                                 "eligible_subquestions":0,
                                 "eligible_marks":__MARKS__,
                                 "teacher_marks":__MARKS__,
                                 "unresolved":[],"excluded":[],
                                 "provenance":{"teacher_verified":0,"official_reference":1,
                                               "ai_generated":0,"unverified":0}},
                 "certified_available":__CERT__,
                 "certified_accuracy":__ACCURACY__,
                 "certification_reason":__REASON__,
                 "latest_run":__RUN__}
                """
                .Replace("__STATUS__", status)
                .Replace("__ELIGIBLE__", eligible ? "1" : "0")
                .Replace("__MARKS__", eligible ? "2" : "0")
                .Replace("__CERT__", Certified ? "true" : "false")
                .Replace("__ACCURACY__", accuracy)
                .Replace("__REASON__", JsonString(_recorded
                    ? "No benchmark run has measured this assessment."
                    : "Certified accuracy is unavailable: no reference item has an explicit teacher confirmation."))
                .Replace("__RUN__", run);
        }

        private string References() => """
            {"items":[__ITEMS__],
             "total":__TOTAL__,"dataset_version":"1",
             "dataset_sha256":"0123456789abcdef",
             "statuses":["verified","disputed","incomplete"],
             "certified_available":false}
            """
            .Replace("__ITEMS__", string.Join(",", Ids.Select(ReferenceJson)))
            .Replace("__TOTAL__", Ids.Count.ToString(System.Globalization.CultureInfo.InvariantCulture));

        private string ReferenceJson(string id)
        {
            var status = _recorded ? (_disputed ? "disputed" : "verified") : "unverified";
            var eligible = _recorded && !_disputed;
            var reason = _recorded
                ? (_disputed ? "teacher_dispute" : "")
                : "no_teacher_assessment";
            return """
                {"reference_id":"__ID__","paper_id":"synthetic-paper",
                 "paper_title":"Synthetic Algebra","board":"Cambridge",
                 "qualification":"O Level","topic":"Algebra","partition":"development",
                 "question":"1","subquestion":null,"level":"question","max_marks":2,
                 "scheme_mark":2,"provenance":"official_reference",
                 "prompt":"Solve x + 3 = 5.","scheme":["M1: subtract 3","A1: x = 2"],
                 "student_answer":["x + 3 = 5","x = 2"],
                 "categories":["fully-correct"],"ruling":"2 of 2: subtraction and answer.",
                 "basis":"both clauses evidenced",
                 "reference":{"reference_id":"__ID__","status":"__STATUS__",
                              "teacher_mark":__MARK__,"reason":__REASON__,
                              "eligible":__ELIGIBLE__,
                              "assessments":[]},
                 "ai":{"run_id":"run-1","mark":1,"max":2,"difference":-1,
                       "direction":"under","matched_by":"exact_question",
                       "review_required":false}}
                """
                .Replace("__ID__", id)
                .Replace("__STATUS__", status)
                .Replace("__MARK__", eligible ? "2" : "null")
                .Replace("__REASON__", JsonString(reason))
                .Replace("__ELIGIBLE__", eligible ? "true" : "false");
        }

        private string ReferenceDetail(string id)
        {
            var status = _recorded ? (_disputed ? "disputed" : "verified") : "unverified";
            var eligible = _recorded && !_disputed;
            var reason = _recorded
                ? (_disputed ? "teacher_dispute" : "")
                : "no_teacher_assessment";
            return """
                {"reference_id":"__ID__","paper_id":"synthetic-paper",
                 "paper_title":"Synthetic Algebra","board":"Cambridge",
                 "qualification":"O Level","topic":"Algebra","partition":"development",
                 "question":"1","subquestion":null,"level":"question","max_marks":2,
                 "scheme_mark":2,"provenance":"official_reference",
                 "prompt":"Solve x + 3 = 5.",
                 "scheme":["M1: subtract 3","M1: state x = 2","A1: answer 2"],
                 "student_answer":["x = 2"],
                 "categories":["fully-correct","method-and-accuracy"],
                 "ruling":"2 of 2: subtraction and answer.","basis":"both clauses evidenced",
                 "reference":{"reference_id":"__ID__","status":"__STATUS__",
                              "teacher_mark":__MARK__,"reason":__REASON__,
                              "eligible":__ELIGIBLE__,
                              "assessments":[]},
                 "history":[__HISTORY__],
                 "ai":{"run_id":"run-1","mark":1,"max":2,"difference":-1,"direction":"under",
                       "matched_by":"exact_question","review_required":false}}
                """
                .Replace("__ID__", id)
                .Replace("__STATUS__", status)
                .Replace("__MARK__", eligible ? "2" : "null")
                .Replace("__REASON__", JsonString(reason))
                .Replace("__ELIGIBLE__", eligible ? "true" : "false")
                .Replace("__HISTORY__", string.Join(",", _history));
        }

        private static string JsonString(string value) => JsonSerializer.Serialize(value);

        private static HttpResponseMessage Json(string body) => new(HttpStatusCode.OK)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json"),
        };
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
