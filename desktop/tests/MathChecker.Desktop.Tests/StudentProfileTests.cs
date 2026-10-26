using System.Net;
using System.Text;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Models;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The profile must show the backend's own figures, keep two students apart,
/// and filter with the submission flags the backend resolved.
/// </summary>
public class StudentProfileTests
{
    private const string ClassList = """{"classes":["10A","10B"]}""";

    private const string ProfileBody = """
        {
          "student_id":"s2","official_name":"Student B","email":"student.b@demo.invalid",
          "nickname":null,"class_name":null,"display_name":"Student B","class_label":"10A",
          "sources":{"identity":"Google Classroom","submission_status":"Google Classroom",
                     "scores":"Local correction database","correction_detail":"AI correction result"},
          "classroom":{"available":true,"assignments_total":3,"submitted":2,"not_submitted":1,"unknown":0,
            "assignments":[
              {"submission_id":"sub1","coursework_id":"w1","title":"Algebra Homework","status":"CORRECTED",
               "status_label":"Corrected","late":false,"submitted":true,"outstanding":false,"returned":false,
               "max_points":20,"due_date":"2026-10-10 20:00","update_time":"2026-10-04T11:00:00Z",
               "result":{"id":"r1","earned":18,"available":20,"percentage":90},
               "job":{"id":"j1","status":"COMPLETED"}},
              {"submission_id":"sub2","coursework_id":"w2","title":"Quadratics Paper","status":"CREATED",
               "status_label":"Draft","late":false,"submitted":false,"outstanding":true,"returned":false,
               "max_points":30,"update_time":"2026-10-05T09:00:00Z"},
              {"submission_id":"sub3","coursework_id":"w3","title":"Trigonometry HW","status":"CORRECTED",
               "status_label":"Corrected","late":true,"submitted":true,"outstanding":false,"returned":false,
               "max_points":15,"update_time":"2026-10-06T10:00:00Z",
               "result":{"id":"r2","earned":11,"available":15,"percentage":73.33333333333333},
               "job":{"id":"j2","status":"COMPLETED"}}
            ]},
          "performance":{"classroom_average":null,"classroom_highest":null,"classroom_lowest":null,
                         "classroom_graded":0,"classroom_not_graded":3,"classroom_zeros":0,
                         "mc_average":81.7,"mc_highest":90.0,"mc_lowest":73.33333333333333,"mc_zeros":0,
                         "total_earned":29,"total_available":35,"overall_percentage":82.9,
                         "missing":1,"late":1,"corrected":2},
          "behavior":{"on_time":1,"late":1,"missing":1,"draft":0,"turned_in":2,"returned":0},
          "corrections":{"total":2,"pending":0,"failed":0,"total_earned":29,"total_available":35,
            "overall_percentage":82.9,"needs_review":0,"last_corrected_at":1792662416.0,
            "recent":[
              {"id":"r1","coursework_id":"w1","submission_id":"sub1","earned":18,"available":20,
               "percentage":90.0,"provisional_grade":"9","created_at":1792662416.0,"needs_review":false,
               "status":"VALID","review_questions":[]},
              {"id":"r2","coursework_id":"w3","submission_id":"sub3","earned":11,"available":15,
               "percentage":73.33333333333333,"provisional_grade":"7","created_at":1792660000.0,
               "needs_review":false,"status":"VALID","review_questions":[]}
            ],
            "jobs":[]}
        }
        """;

    private const string ResultOne = """
        {"id":"r1","result_json":{"questions":[
          {"question_number":"Q1","max_marks":8,"awarded_marks":7,"topic":"Solving equations",
           "status":"VALID","loss_reason":"Sign error in the final line."},
          {"question_number":"Q2","max_marks":6,"awarded_marks":6,"topic":"Expanding brackets",
           "status":"VALID","loss_reason":"Full marks."},
          {"question_number":"Q3","max_marks":6,"awarded_marks":5,"topic":"Factorising",
           "status":"VALID","loss_reason":"One factor missing."}]},
         "verification_json":{"status":"VALID"}}
        """;

    private const string ResultTwo = """
        {"id":"r2","result_json":{"questions":[
          {"question_number":"Q1","max_marks":8,"awarded_marks":7,"topic":"Solving equations",
           "status":"VALID","loss_reason":"Arithmetic slip."},
          {"question_number":"Q2","max_marks":7,"awarded_marks":4,"topic":"Sine rule",
           "status":"VALID","loss_reason":"Used the cosine rule."}]},
         "verification_json":{"status":"VALID"}}
        """;

    [Fact]
    public void Summary_figures_are_the_backend_figures_and_missing_is_not_zero()
    {
        var vm = NewProfile(ParseProfile());

        Assert.Equal("3", vm.AssignmentsText);
        Assert.Equal("2/3", vm.SubmittedText);
        Assert.Equal("1", vm.OutstandingText);
        Assert.Equal("81.7%", vm.McAverageText);
        Assert.Equal("29 / 35 (82.9%)", vm.OverallText);
        Assert.Equal("Not graded", vm.ClassroomAverageText);
        Assert.Equal("0 graded · 3 not graded", vm.ClassroomGradedText);
        Assert.Equal("2 stored corrections", vm.CorrectionCountText);
        Assert.False(vm.ShowTrackingWarning);
    }

    [Fact]
    public void Assignment_filters_use_the_flags_resolved_by_the_backend()
    {
        var vm = NewProfile(ParseProfile());
        Assert.Equal(3, vm.Assignments.Count);

        vm.AssignmentFilter = "Submitted";
        Assert.Equal(2, vm.Assignments.Count);
        Assert.All(vm.Assignments, a => Assert.True(a.Submitted));

        vm.AssignmentFilter = "Outstanding";
        var outstanding = Assert.Single(vm.Assignments);
        Assert.Equal("Quadratics Paper", outstanding.TitleText);
        Assert.True(outstanding.Outstanding);

        vm.AssignmentFilter = "Late";
        Assert.Single(vm.Assignments);

        vm.AssignmentFilter = "Corrected";
        Assert.Equal(2, vm.Assignments.Count);
        Assert.All(vm.Assignments, a => Assert.True(a.HasCorrection));

        vm.AssignmentFilter = "All";
        Assert.Equal("3 of 3 shown", vm.AssignmentCountText);
    }

    [Theory]
    [InlineData("All", 3, "3 of 3 shown")]
    [InlineData("Submitted", 2, "2 of 3 shown")]
    [InlineData("Outstanding", 1, "1 of 3 shown")]
    [InlineData("Late", 1, "1 of 3 shown")]
    [InlineData("Corrected", 2, "2 of 3 shown")]
    public void Every_filter_option_reports_its_own_rows_and_count(
        string filter, int expected, string expectedCount)
    {
        var vm = NewProfile(ParseProfile());

        vm.AssignmentFilter = filter;

        Assert.Equal(filter, vm.AssignmentFilter);
        Assert.Equal(expected, vm.Assignments.Count);
        Assert.Equal(expectedCount, vm.AssignmentCountText);
        Assert.Equal(expected == 0, vm.AssignmentsEmpty);
        Assert.Equal(3, vm.Profile.Classroom.Assignments.Count);
    }

    [Fact]
    public void A_filter_with_no_matches_is_empty_and_clears_the_hidden_selection()
    {
        const string body = """
            {
              "student_id":"s1","official_name":"Student One","display_name":"Student One",
              "class_label":"10A",
              "sources":{"identity":"Google Classroom","submission_status":"Google Classroom",
                         "scores":"Local correction database","correction_detail":"AI correction result"},
              "classroom":{"available":true,"assignments_total":2,"submitted":2,"not_submitted":0,
                "unknown":0,"assignments":[
                  {"submission_id":"s1","coursework_id":"w1","title":"On time work","status":"CORRECTED",
                   "status_label":"Corrected","late":false,"submitted":true,"outstanding":false,
                   "returned":false,"max_points":10,
                   "result":{"id":"r1","earned":8,"available":10,"percentage":80}},
                  {"submission_id":"s2","coursework_id":"w2","title":"Second work","status":"TURNED_IN",
                   "status_label":"Turned in","late":false,"submitted":true,"outstanding":false,
                   "returned":false,"max_points":10}
                ]},
              "performance":{"classroom_graded":1,"classroom_not_graded":1,"mc_average":80.0,
                             "total_earned":8,"total_available":10,"overall_percentage":80.0,
                             "missing":0,"late":0,"corrected":1},
              "behavior":{"on_time":2,"late":0,"missing":0,"draft":0,"turned_in":2,"returned":0},
              "corrections":{"total":1,"pending":0,"failed":0,"total_earned":8,"total_available":10,
                             "overall_percentage":80.0,"needs_review":0,"recent":[],"jobs":[]}
            }
            """;
        var vm = NewProfile(ParseProfile(body));
        vm.SelectedAssignment = vm.Assignments.Single(a => a.TitleText == "On time work");
        Assert.True(vm.HasSelectedAssignment);

        vm.AssignmentFilter = "Late";

        Assert.Equal("0 of 2 shown", vm.AssignmentCountText);
        Assert.True(vm.AssignmentsEmpty);
        Assert.Null(vm.SelectedAssignment);
        Assert.False(vm.HasSelectedAssignment);
        Assert.Equal("Select an assignment to open its correction.", vm.SelectedAssignmentHint);
    }

    [Fact]
    public void A_student_without_assignments_is_empty_for_every_filter()
    {
        var vm = NewProfile(ParseProfile(ProfileWithName("Student Zero", "s0")));

        foreach (var filter in vm.AssignmentFilters)
        {
            vm.AssignmentFilter = filter;
            Assert.Empty(vm.Assignments);
            Assert.True(vm.AssignmentsEmpty);
            Assert.Equal("No assignments tracked", vm.AssignmentCountText);
        }
    }

    [Fact]
    public void A_blank_or_unknown_filter_falls_back_to_all_instead_of_blank()
    {
        var vm = NewProfile(ParseProfile());
        vm.AssignmentFilter = "Outstanding";

        vm.AssignmentFilter = "Not a filter";
        Assert.Equal("All", vm.AssignmentFilter);
        Assert.Equal(3, vm.Assignments.Count);

        vm.AssignmentFilter = "   ";
        Assert.Equal("All", vm.AssignmentFilter);
        Assert.Equal(3, vm.Assignments.Count);

        Assert.Equal(new[] { "All", "Submitted", "Outstanding", "Late", "Corrected" },
            vm.AssignmentFilters);
    }

    [Fact]
    public async Task Selecting_a_student_opens_that_record_and_back_closes_it()
    {
        const string roster = """
            {"students":[
              {"student_id":"s1","official_name":"Student A","class_label":"10A"},
              {"student_id":"s2","official_name":"Student B","class_label":"10A"}
            ]}
            """;
        var handler = new RoutingHandler()
            .Map("students", roster)
            .Map("students/classes", ClassList)
            .Map("students/s1", ProfileWithName("Student A", "s1"))
            .Map("students/s2", ProfileBody)
            .Map("results/r1", ResultOne)
            .Map("results/r2", ResultTwo);

        using (handler)
        {
            var session = await SessionAsync(handler);
            var page = new StudentsViewModel(session);
            await page.LoadAsync();
            Assert.False(page.IsProfileOpen);

            await page.OpenProfileAsync(page.Items.Single(s => s.StudentId == "s2"));

            Assert.True(page.IsProfileOpen);
            Assert.Equal("Student B", page.Profile!.Profile.Name);
            Assert.Equal("Student B", page.HeaderTitle);
            Assert.Equal("10A", page.Profile.Profile.ClassText);
            Assert.Contains(handler.Requests, r => r.Path == "/api/students/s2");

            page.CloseProfile();
            Assert.False(page.IsProfileOpen);
            Assert.Null(page.Profile);
            Assert.Null(page.Selected);
            // The hub is called People; the header falls back to the page title.
            Assert.StartsWith("People", page.HeaderTitle);
        }
    }

    [Fact]
    public async Task Two_students_never_share_profile_data()
    {
        var handler = new RoutingHandler()
            .Map("students/classes", ClassList)
            .Map("students/s1", ProfileWithName("Student A", "s1"))
            .Map("students/s2", ProfileBody)
            .Map("results/r1", ResultOne)
            .Map("results/r2", ResultTwo);

        using (handler)
        {
            var session = await SessionAsync(handler);
            var first = NewProfile(ParseProfile(ProfileWithName("Student A", "s1")), session);
            var second = NewProfile(ParseProfile(), session);

            Assert.Equal("Student A", first.Profile.Name);
            Assert.Equal("s1", first.Profile.StudentId);
            Assert.Equal("Student B", second.Profile.Name);
            Assert.Equal("s2", second.Profile.StudentId);
            Assert.Equal(0, first.Profile.Corrections.Total);
            Assert.Equal(2, second.Profile.Corrections.Total);
        }
    }

    [Fact]
    public async Task Details_save_sends_only_local_metadata_and_applies_the_response()
    {
        var updated = ProfileBody
            .Replace("\"nickname\":null", "\"nickname\":\"Bee\"")
            .Replace("\"class_name\":null", "\"class_name\":\"10B\"");
        var handler = new RoutingHandler()
            .Map("students/s2/details", updated, "PUT")
            .Map("students/classes", ClassList)
            .Map("results/r1", ResultOne)
            .Map("results/r2", ResultTwo);

        using (handler)
        {
            var session = await SessionAsync(handler);
            var vm = NewProfile(ParseProfile(), session);
            var saved = false;
            vm.ProfileSaved += () => saved = true;

            vm.BeginEditCommand.Execute(null);
            vm.NicknameDraft = "  Bee  ";
            vm.ClassDraft = "10B";
            await vm.SaveDetailsAsync();

            Assert.Null(vm.Error);
            Assert.False(vm.IsEditingDetails);
            Assert.Equal("Bee", vm.Profile.Nickname);
            Assert.Equal("10B", vm.Profile.ClassName);
            Assert.True(saved);

            var put = Assert.Single(handler.Requests, r => r.Method == "PUT");
            using var body = JsonDocument.Parse(put.Body!);
            Assert.Equal("Bee", body.RootElement.GetProperty("nickname").GetString());
            Assert.Equal("10B", body.RootElement.GetProperty("class_name").GetString());
            Assert.Equal(1, handler.Requests.Count(r => r.Path == "/api/students/s2/details"));
        }
    }

    [Fact]
    public async Task Correction_detail_and_topic_marks_come_from_stored_questions()
    {
        var handler = new RoutingHandler()
            .Map("students/classes", ClassList)
            .Map("results/r1", ResultOne)
            .Map("results/r2", ResultTwo);

        using (handler)
        {
            var session = await SessionAsync(handler);
            var vm = NewProfile(ParseProfile(), session);

            await vm.EnsureDetailsAsync();

            Assert.Equal(4, vm.Topics.Count);
            var solving = vm.Topics.Single(t => t.Topic == "Solving equations");
            Assert.Equal(14, solving.Earned);
            Assert.Equal(16, solving.Available);
            Assert.Equal(87.5, solving.Percentage, 1);
            Assert.Equal(2, solving.Questions);
            Assert.Contains(vm.Topics, t => t.Topic == "Expanding brackets" && t.Percentage == 100);

            Assert.Equal(2, vm.Trend.Count);
            Assert.True(vm.HasTrend);
            Assert.Equal("73.3%", vm.Trend[0].ValueText);
            Assert.Equal("90%", vm.Trend[1].ValueText);

            Assert.False(vm.HasRepeatedDifficulties);
            Assert.Contains("Expanding brackets", vm.StrengthText);
            Assert.Contains("Sine rule", vm.FocusText);

            // The most recent correction is selected and its questions are shown.
            Assert.Equal("r1", vm.SelectedCorrection!.Id);
            Assert.Equal(3, vm.Questions.Count);
            Assert.False(vm.QuestionsEmpty);
            Assert.Contains("Solving equations", vm.Questions[0].Topic);
            Assert.Equal("7 / 8", vm.Questions[0].MarkText);
        }
    }

    [Fact]
    public async Task A_correction_without_a_breakdown_says_so_instead_of_inventing_one()
    {
        var handler = new RoutingHandler()
            .Map("students/classes", ClassList)
            .Map("results/r1", """{"id":"r1","result_json":{}}""")
            .Map("results/r2", ResultTwo);

        using (handler)
        {
            var session = await SessionAsync(handler);
            var vm = NewProfile(ParseProfile(), session);

            await vm.EnsureDetailsAsync();

            Assert.True(vm.QuestionsEmpty);
            Assert.Contains("without a question breakdown", vm.CorrectionDetailNote);
        }
    }

    [Fact]
    public void History_lists_every_correction_while_the_trend_stays_bounded()
    {
        var profile = ParseProfile();
        profile.Corrections.Total = 13;
        profile.Corrections.History = Enumerable.Range(0, 13)
            .Select(i => new StudentCorrectionDto
            {
                Id = "r" + i,
                Earned = 10,
                Available = 10,
                Percentage = 90 - i,
                CreatedAt = 1792662416.0 - i * 60,
            })
            .ToList();

        var vm = NewProfile(profile);

        Assert.Equal(13, vm.Corrections.Count);
        Assert.Equal("13 stored corrections", vm.CorrectionCountText);
        Assert.Equal(12, vm.Trend.Count);
        Assert.Contains("12 most recent of 13", vm.TrendNote);
        // Newest is still first in the list and last in the oldest-first chart.
        Assert.Equal("r0", vm.Corrections[0].Id);
        Assert.Equal(90, vm.Trend[^1].Percentage, 1);
    }

    [Fact]
    public void One_correction_is_not_enough_for_a_trend()
    {
        var profile = ParseProfile();
        profile.Corrections.Recent = profile.Corrections.Recent.Take(1).ToList();
        profile.Corrections.Total = 1;

        var vm = NewProfile(profile);

        Assert.False(vm.HasTrend);
        Assert.Contains("at least two", vm.TrendNote);
        Assert.False(vm.HasTopics);
        Assert.Equal("Not enough topic data yet.", vm.StrengthText);
    }

    [Fact]
    public async Task Repeated_difficulty_is_reported_only_from_repeated_low_topic_marks()
    {
        const string lowOne = """
            {"id":"r1","result_json":{"questions":[
              {"question_number":"Q1","max_marks":5,"awarded_marks":2,"topic":"Fractions"}]}}
            """;
        const string lowTwo = """
            {"id":"r2","result_json":{"questions":[
              {"question_number":"Q1","max_marks":5,"awarded_marks":3,"topic":"Fractions"}]}}
            """;
        var handler = new RoutingHandler()
            .Map("students/classes", ClassList)
            .Map("results/r1", lowOne)
            .Map("results/r2", lowTwo);

        using (handler)
        {
            var session = await SessionAsync(handler);
            var vm = NewProfile(ParseProfile(), session);

            await vm.EnsureDetailsAsync();

            Assert.True(vm.HasRepeatedDifficulties);
            Assert.Contains("Fractions", vm.RepeatedText);
            var fractions = Assert.Single(vm.Topics);
            Assert.Equal(2, fractions.Questions);
            Assert.Equal(50, fractions.Percentage, 1);
        }
    }

    [Fact]
    public async Task A_failed_report_export_surfaces_the_backend_message()
    {
        // No report route is mapped, so the backend answers 404 and the page
        // must say so instead of leaving the button silently dead.
        var handler = new RoutingHandler().Map("students/s2", ProfileBody);
        using (handler)
        {
            var session = await SessionAsync(handler);
            var vm = NewProfile(ParseProfile(), session);

            vm.ReportCommand.Execute(null);
            await WaitUntilAsync(() => vm.Error is not null);

            Assert.NotNull(vm.Error);
            Assert.Contains("not found", vm.Error, StringComparison.OrdinalIgnoreCase);
            Assert.Contains(handler.Requests, r => r.Path == "/api/students/s2/report");
        }
    }

    [Fact]
    public async Task A_404_keeps_the_error_and_does_not_open_a_profile()
    {
        var handler = new RoutingHandler();
        using (handler)
        {
            var session = await SessionAsync(handler);
            var page = new StudentsViewModel(session);

            await page.OpenProfileAsync(new StudentDto { StudentId = "missing", OfficialName = "Nobody" });

            Assert.False(page.IsProfileOpen);
            Assert.NotNull(page.Error);
            Assert.Contains("Student not found", page.Error);
        }
    }

    // ---------------------------------------------------------------- helpers

    private static StudentProfileViewModel NewProfile(StudentProfileDto profile, AppSession? session = null) =>
        new(session ?? OfflineSession(), profile);

    /// <summary>A session with a stopped backend: the view model's formatting
    /// tests never touch HTTP, so no host has to be started for them.</summary>
    private static AppSession OfflineSession()
    {
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        return new AppSession(settings, new MemoryLog(),
            new BackendHost(settings, new MemoryLog()),
            new MathCheckerApiClient(new RoutingHandler()), new SilentDialogs());
    }

    private static StudentProfileDto ParseProfile(string? body = null) =>
        JsonSerializer.Deserialize<StudentProfileDto>(body ?? ProfileBody, Json.Options)!;

    private static string ProfileWithName(string name, string id) => $$"""
        {
          "student_id":"{{id}}","official_name":"{{name}}","display_name":"{{name}}",
          "class_label":"10A",
          "sources":{"identity":"Google Classroom","submission_status":"Google Classroom",
                     "scores":"No corrections yet","correction_detail":"No corrections yet"},
          "classroom":{"available":false,"assignments_total":0,"submitted":0,"not_submitted":0,
                       "unknown":0,"assignments":[]},
          "performance":{"total_earned":0,"total_available":0,"missing":0,"late":0,"corrected":0},
          "behavior":{},
          "corrections":{"total":0,"pending":0,"failed":0,"total_earned":0,"total_available":0,
                         "needs_review":0,"recent":[],"jobs":[]}
        }
        """;

    private static async Task WaitUntilAsync(Func<bool> condition)
    {
        for (var attempt = 0; attempt < 150 && !condition(); attempt++)
        {
            await Task.Delay(20);
        }
    }

    private static async Task<AppSession> SessionAsync(RoutingHandler handler)
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

    private sealed record Recorded(string Method, string Path, string? Body);

    /// <summary>Routes by path (and optionally method) so a prefetch cannot
    /// consume a response meant for another call.</summary>
    private sealed class RoutingHandler : HttpMessageHandler
    {
        private readonly Dictionary<string, string> _responses = new(StringComparer.OrdinalIgnoreCase);
        private readonly HashSet<string> _methods = new(StringComparer.OrdinalIgnoreCase);

        public List<Recorded> Requests { get; } = new();

        public RoutingHandler Map(string path, string body, string method = "GET")
        {
            _responses[path] = body;
            _methods.Add(method + " " + path);
            return this;
        }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;
            var key = path.StartsWith("/api/", StringComparison.Ordinal) ? path[5..] : path.TrimStart('/');
            var body = request.Content is null
                ? null
                : await request.Content.ReadAsStringAsync(cancellationToken);
            Requests.Add(new Recorded(request.Method.Method, path, body));

            if (!_methods.Contains(request.Method.Method + " " + key)
                && request.Method != HttpMethod.Get)
            {
                return Error(HttpStatusCode.MethodNotAllowed, "method_not_allowed",
                    "Unexpected " + request.Method + " " + key);
            }

            if (_responses.TryGetValue(key, out var payload))
            {
                return new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new StringContent(payload, Encoding.UTF8, "application/json"),
                };
            }

            return Error(HttpStatusCode.NotFound, "student_not_found", "Student not found. Run a Classroom sync first.");
        }

        private static HttpResponseMessage Error(HttpStatusCode status, string code, string message) =>
            new(status)
            {
                Content = new StringContent(JsonSerializer.Serialize(new { code, message }),
                    Encoding.UTF8, "application/json"),
            };
    }
}
