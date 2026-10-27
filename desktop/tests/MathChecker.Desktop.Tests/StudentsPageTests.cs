using System.Net;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

public class StudentsPageTests
{
    private const string StudentsBody = """
        {"students":[
          {"student_id":"s1","official_name":"Student A","class_label":"10A","email":"student.a@demo.invalid"},
          {"student_id":"s2","official_name":"Student B","class_label":"10A","email":"student.b@demo.invalid"},
          {"student_id":"s3","official_name":"Student C","class_label":"10B","email":"student.c@demo.invalid"},
          {"student_id":"s4","official_name":"Student D","class_label":"10B","email":"student.d@demo.invalid"},
          {"student_id":"s5","official_name":"Student E","class_label":null,"email":"student.e@demo.invalid"}
        ]}
        """;

    [Fact]
    public async Task Loading_students_keeps_every_row_after_the_class_list_is_rebuilt()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new StudentsViewModel(session);

            await page.LoadAsync();

            Assert.Null(page.Error);
            Assert.Equal(5, page.Items.Count);
            Assert.Contains("All classes", page.ClassFilters);
            Assert.Contains("10A", page.ClassFilters);
        }
    }

    [Fact]
    public async Task A_null_class_selection_means_no_filter_rather_than_no_rows()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new StudentsViewModel(session);
            await page.LoadAsync();

            // WPF writes null through the two-way SelectedItem binding when the
            // filter list is cleared; that must not hide every student.
            page.ClassFilter = null!;

            Assert.Equal(5, page.Items.Count);
            Assert.Equal("All classes", page.ClassFilter);
        }
    }

    [Fact]
    public async Task Class_and_text_filters_narrow_the_list_and_survive_a_reload()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new StudentsViewModel(session);
            await page.LoadAsync();

            page.ClassFilter = "10B";
            Assert.Equal(2, page.Items.Count);

            page.Filter = "Student D";
            Assert.Single(page.Items);

            page.Filter = string.Empty;
            await page.LoadAsync();
            Assert.Equal(2, page.Items.Count);
            Assert.Equal("10B", page.ClassFilter);
        }
    }

    // The People row shows what the backend actually knows: Classroom's own
    // grade, Math Checker's own correction, and when the roster last synced.
    private const string EnrichedBody = """
        {"students":[
          {"student_id":"u1","official_name":"Ada Lovelace","class_label":"10A",
           "email":"ada@demo.invalid","classroom_courses":["Mathematics — 10A"],
           "assignments":15,"submitted":12,"tracking_available":true,
           "corrections":12,"total_earned":240,"total_available":300,"pending":1,
           "role":"student","latest_status":"SUBMITTED","latest_status_label":"Submitted",
           "latest_assignment":"Algebra test","latest_correction":"Corrected",
           "latest_result":"18/20 (90.0%)","latest_classroom_grade":"17/20 (85.0%)",
           "graded_count":2,"last_synced_at":1790000000}
        ],"last_sync":1790000000,
        "source":{"identity":"classroom","submission_status":"classroom",
                  "scores":"local","email":"not provided by Classroom"}}
        """;

    [Fact]
    public async Task A_person_row_carries_the_classroom_grade_correction_and_sync_facts()
    {
        var (session, handler) = await SessionAsync(EnrichedBody);
        using (handler)
        {
            var page = new StudentsViewModel(session);
            await page.LoadAsync();

            var row = Assert.Single(page.Items);
            Assert.Equal("Ada Lovelace", row.Name);
            Assert.Equal("10A", row.ClassText);
            Assert.Equal("Mathematics — 10A", row.CoursesText);
            Assert.Equal("12 of 15", row.SubmittedText);
            Assert.Equal("Corrected · 18/20 (90.0%)", row.CorrectionText);
            // The Classroom grade is its own field, never folded into the score.
            Assert.Equal("17/20 (85.0%)", row.ClassroomGradeText);
            Assert.Equal("1 in progress", row.PendingText);
            Assert.NotEqual("never", row.SyncText);

            Assert.Contains("Roster synced", page.LastSyncText);
            Assert.Equal("not provided by Classroom", page.EmailSource);
            Assert.Equal("1", page.StudentsCountText);
            Assert.Equal("1", page.ClassesCountText);
            Assert.Equal("12", page.CorrectionsCountText);
            Assert.Equal("1 shown", page.ShownText);
        }
    }

    [Fact]
    public async Task A_student_with_no_class_or_email_is_labelled_honestly()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new StudentsViewModel(session);
            await page.LoadAsync();

            var unknown = page.Items.Single(s => s.StudentId == "s5");
            Assert.Equal("Not assigned", unknown.ClassText);
            Assert.Equal("No Classroom course", unknown.CoursesText);
            Assert.Equal("Not corrected", unknown.CorrectionText);
            Assert.Equal("Not graded in Classroom", unknown.ClassroomGradeText);
            Assert.Equal("Not tracked", unknown.SubmittedText);
        }
    }

    private static async Task<(AppSession Session, StubHttpHandler Handler)> SessionAsync(
        string body = StudentsBody)
    {
        // The page reads the Classroom connection state before the roster (the
        // strip that holds Connect/Disconnect), so that answer comes first.
        var handler = new StubHttpHandler()
            .Respond(HttpStatusCode.OK, """{"connected":false,"client_configured":true}""")
            .Respond(HttpStatusCode.OK, body);
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        return (new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs()), handler);
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
