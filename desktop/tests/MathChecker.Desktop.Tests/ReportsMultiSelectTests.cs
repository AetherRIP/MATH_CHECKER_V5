using System.Net;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The Reports page must let a teacher choose several assignments, send every
/// chosen id to the backend, and show the backend's combined mark fraction —
/// never an average of per-assignment percentages.
/// </summary>
public class ReportsMultiSelectTests
{
    private const string ClassesBody = """
        {"classes":[{"id":"c1","name":"Mathematics","section":"10A","students":3,"assignments":2}],"total":1}
        """;

    private const string AssignmentsBody = """
        {"assignments":[
          {"id":"w1","course_id":"c1","title":"Algebra test","max_points":20},
          {"id":"w2","course_id":"c1","title":"Geometry test","max_points":50}
        ],"total":2}
        """;

    private const string StudentsBody = """
        {"students":[
          {"student_id":"u1","official_name":"Ada Lovelace","class_label":"10A"}
        ],"last_sync":1790000000,
        "source":{"identity":"classroom","submission_status":"classroom",
                  "scores":"local","email":"not provided by Classroom"}}
        """;

    // 18/20 + 40/50 = 58/70 = 82.9 %, deliberately not (90 + 80) / 2 = 85 %.
    private const string ReportBody = """
        {"is_multi":true,
         "class":{"course_id":"c1","name":"Mathematics","section":"10A"},
         "assignments":[
           {"id":"w1","title":"Algebra test","course_id":"c1","course_name":"Mathematics",
            "course_section":"10A","max_points":20,
            "stats":{"submission":{"not_submitted":0},"math_checker":{"corrected":1,"average":90.0}}},
           {"id":"w2","title":"Geometry test","course_id":"c1","course_name":"Mathematics",
            "course_section":"10A","max_points":50,
            "stats":{"submission":{"not_submitted":0},"math_checker":{"corrected":1,"average":80.0}}}
         ],
         "students":[{
           "student_id":"u1","display_name":"Ada Lovelace","student_name":"Ada Lovelace",
           "class_name":"Year 10","class_label":"10A",
           "assignments":2,"submitted":2,"missing":0,"late":0,
           "corrected":2,"incomplete":0,"failed":0,"not_processed":0,"zero_marks":0,
           "graded_in_classroom":1,"earned":58.0,"available":70.0,"percentage":82.9,
           "classroom_earned":17.0,"classroom_available":20.0,"classroom_percentage":85.0,
           "by_assignment":[
             {"coursework_id":"w1","title":"Algebra test","status":"SUBMITTED","status_label":"Submitted",
              "mc_status":"CORRECTED","mc_earned":18.0,"mc_available":20.0,"mc_percentage":90.0},
             {"coursework_id":"w2","title":"Geometry test","status":"SUBMITTED","status_label":"Submitted",
              "mc_status":"CORRECTED","mc_earned":40.0,"mc_available":50.0,"mc_percentage":80.0}
           ]
         }],
         "stats":{"students":1,"assignments":2,"cells":2,"submitted":2,"missing":0,"late":0,
                  "corrected":2,"incomplete":0,"failed":0,"zero_marks":0,"graded_in_classroom":1,
                  "earned":58.0,"available":70.0,"percentage":82.9,
                  "classroom_earned":17.0,"classroom_available":20.0,"classroom_percentage":85.0},
         "filters":{"ids":["w1","w2"]}}
        """;

    [Fact]
    public async Task Select_all_ticks_every_visible_assignment_and_clear_ticks_none()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new ReportsViewModel(session);
            await page.LoadAsync();

            Assert.Equal(2, page.VisibleAssignments.Count);
            Assert.Empty(page.SelectedIds);

            page.SelectAllCommand.Execute(null);
            Assert.Equal(new[] { "w1", "w2" }, page.SelectedIds);

            page.ClearSelectionCommand.Execute(null);
            Assert.Empty(page.SelectedIds);
            Assert.Contains("No assignments selected", page.SelectionSummary);
        }
    }

    [Fact]
    public async Task Building_with_two_selections_asks_for_both_and_shows_the_combined_fraction()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new ReportsViewModel(session);
            await page.LoadAsync();

            // Deselect one, reselect it: selection is per assignment, not global.
            page.VisibleAssignments[0].IsSelected = true;
            Assert.Equal(new[] { "w1" }, page.SelectedIds);
            page.VisibleAssignments[1].IsSelected = true;
            Assert.Equal(new[] { "w1", "w2" }, page.SelectedIds);

            page.BuildCommand.Execute(null);
            await WaitUntilAsync(() => page.Rows.Count == 1);

            var request = handler.Requests[^1];
            Assert.Contains("reports/assignments", request.Url);
            Assert.Contains("ids=w1%2Cw2", request.Url);

            var row = Assert.Single(page.Rows);
            Assert.Equal("Ada Lovelace", row.Name);
            Assert.Equal("10A", row.ClassText);
            Assert.Equal("58 / 70", row.Score);
            Assert.Equal("82.9%", row.Percentage);
            Assert.NotEqual("85%", row.Percentage);
            Assert.Contains("85", row.Classroom);
            // Both assignments stay visible inside the combined row.
            Assert.Contains("18/20", row.Breakdown);
            Assert.Contains("40/50", row.Breakdown);

            Assert.Equal(2, page.ReportAssignments.Count);
            Assert.Contains("58/70", page.StatsLine.Replace(" ", string.Empty));
            Assert.Contains("82.9", page.StatsLine);
        }
    }

    [Fact]
    public async Task Building_with_nothing_ticked_explains_itself_and_calls_no_endpoint()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new ReportsViewModel(session);
            await page.LoadAsync();
            var before = handler.Requests.Count;

            page.BuildCommand.Execute(null);
            await WaitUntilAsync(() => page.Error is not null);

            Assert.Contains("Tick at least one assignment", page.Error);
            Assert.Equal(before, handler.Requests.Count);
        }
    }

    [Fact]
    public async Task A_class_filter_narrows_the_list_but_never_silently_drops_a_choice()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new ReportsViewModel(session);
            await page.LoadAsync();

            page.SelectAllCommand.Execute(null);
            // "All classes" is the default and keeps both assignments visible.
            Assert.Equal(2, page.VisibleAssignments.Count);

            page.SelectedClass = page.Classes.First(c => c.Id == "c1");
            Assert.Equal(2, page.VisibleAssignments.Count);
            Assert.Equal(2, page.SelectedIds.Count);

            page.SelectedClass = page.Classes.First(c => c.Id.Length == 0);
            Assert.Contains("2 assignments selected", page.SelectionSummary);
        }
    }

    [Fact]
    public async Task Student_filters_are_sent_as_the_backend_expects()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new ReportsViewModel(session);
            await page.LoadAsync();

            Assert.Contains("All classes", page.StudentClasses);
            Assert.Contains("10A", page.StudentClasses);

            page.SelectAllCommand.Execute(null);
            page.StudentClass = "10A";
            page.StudentQuery = "  Ada  ";

            page.BuildCommand.Execute(null);
            await WaitUntilAsync(() => page.Rows.Count == 1);

            var url = handler.Requests[^1].Url;
            Assert.Contains("class_label=10A", url);
            Assert.Contains("student=Ada", url);
            Assert.DoesNotContain("student=%20", url);
            // The folded scope band still states every applied filter.
            Assert.Contains("10A", page.ScopeSummary);
            Assert.Contains("name contains \"Ada\"", page.ScopeSummary);
        }
    }

    // A student who never handed anything in has no mark fraction. Printing
    // "0 / 0" would look like a zero score, which is a different, wrong fact.
    private const string MissingReportBody = """
        {"is_multi":true,
         "assignments":[{"id":"w1","title":"Algebra test","max_points":20},
                        {"id":"w2","title":"Geometry test","max_points":50}],
         "students":[{
           "student_id":"u3","display_name":"Cleo Chan","class_label":"10B",
           "assignments":2,"submitted":0,"missing":2,"corrected":0,
           "earned":0.0,"available":0.0,"percentage":null,
           "by_assignment":[
             {"coursework_id":"w1","status":"MISSING","status_label":"Missing"},
             {"coursework_id":"w2","status":"MISSING","status_label":"Missing"}]}],
         "stats":{"students":1,"assignments":2,"corrected":0,"missing":2,
                  "earned":0.0,"available":0.0,"percentage":null}}
        """;

    [Fact]
    public async Task A_student_with_no_submission_shows_no_marks_instead_of_a_zero()
    {
        var (session, handler) = await SessionAsync(MissingReportBody);
        using (handler)
        {
            var page = new ReportsViewModel(session);
            await page.LoadAsync();
            page.SelectAllCommand.Execute(null);
            page.BuildCommand.Execute(null);
            await WaitUntilAsync(() => page.Rows.Count == 1);

            var row = Assert.Single(page.Rows);
            Assert.Equal("—", row.Score);
            Assert.Equal("—", row.Percentage);
            // Each unsubmitted assignment says so, and the row repeats the count.
            Assert.Equal("missing  ·  missing", row.Breakdown);
            Assert.Contains("not submitted", row.Note);
            Assert.DoesNotContain("0 / 0", row.Score);
        }
    }

    private static async Task<(AppSession Session, StubHttpHandler Handler)> SessionAsync(
        string reportBody = ReportBody)
    {
        var handler = new StubHttpHandler()
            .Respond(HttpStatusCode.OK, ClassesBody)
            .Respond(HttpStatusCode.OK, AssignmentsBody)
            .Respond(HttpStatusCode.OK, StudentsBody)
            .Respond(HttpStatusCode.OK, reportBody);
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        return (new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs()), handler);
    }

    /// <summary>AsyncCommand.Execute is fire-and-forget by design (it feeds the
    /// UI), so a test waits for the observable effect instead of the call.</summary>
    private static async Task WaitUntilAsync(Func<bool> condition, int timeoutMs = 5000)
    {
        var started = Environment.TickCount64;
        while (!condition())
        {
            if (Environment.TickCount64 - started > timeoutMs)
            {
                throw new TimeoutException("the page never reached the expected state");
            }

            await Task.Delay(20);
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
