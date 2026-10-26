using System.Net;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Models;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The Assignment Details page must show what the backend actually recorded:
/// one row per expected student, completion and correction kept apart, and
/// summary counts that are the table's own rows counted — never a second
/// opinion computed in the client.
/// </summary>
public class AssignmentDetailPageTests
{
    private const string CourseworkId = "cw-1";

    // Five students covering every completion bucket: handed in and corrected,
    // handed in but the correction failed, a draft, a roster student with no
    // submission, and a record the sync could not verify.
    private const string StudentsBody = """
        {
          "assignment": {
            "id": "cw-1", "course_id": "c1", "title": "Ch2L3 - Simplifying Expressions",
            "course_name": "WBIS - O Level Mathematics - Mr. Abanout", "course_section": "Set 1",
            "state": "PUBLISHED", "due_date": "2026-10-16 23:59", "max_points": 9.0,
            "description": "Simplify each expression.", "has_mark_scheme": true,
            "managed": true, "local": false, "deleted_in_classroom": false
          },
          "capabilities": {"kind": "managed"},
          "students": [
            {"student_id":"u1","name":"Ada Lovelace","status":"SUBMITTED","status_label":"Submitted",
             "completion":"COMPLETED","completion_label":"Completed","completion_reason":null,
             "submitted":true,"submitted_at":"2026-10-15T09:12:00Z","late":false,
             "attachments":[{"title":"ada.pdf","link":"https://classroom.example/ada.pdf","mime_type":"application/pdf"}],
             "correction":{"state":"CORRECTED","label":"Corrected","job_status":"COMPLETED",
                           "earned":7.0,"available":9.0,"percentage":77.8,"partial":false,"error":null},
             "max_points":9.0,"assigned_grade":null,"quality":"VERIFIED","stale":false,
             "sync_error":null,"last_synced_at":1790000000.0,"synthetic":false,"submission_id":"s1"},
            {"student_id":"u2","name":"Ben Bray","status":"FAILED","status_label":"Correction failed",
             "completion":"COMPLETED","completion_label":"Completed","completion_reason":null,
             "submitted":true,"submitted_at":"2026-10-15T10:00:00Z","late":true,"attachments":[],
             "correction":{"state":"FAILED","label":"Correction failed","job_status":"FAILED",
                           "earned":null,"available":null,"percentage":null,"partial":false,
                           "error":"The Gemini request timed out."},
             "max_points":9.0,"assigned_grade":null,"quality":"VERIFIED","stale":false,
             "sync_error":null,"last_synced_at":1790000000.0,"synthetic":false,"submission_id":"s2"},
            {"student_id":"u3","name":"Cleo Chan","status":"CREATED","status_label":"Opened, not submitted",
             "completion":"NOT_COMPLETED","completion_label":"Not completed",
             "completion_reason":"The assignment was opened but nothing was handed in.",
             "submitted":false,"submitted_at":null,"late":false,"attachments":[],
             "correction":{"state":"NOT_CORRECTED","label":"Not corrected","job_status":null,
                           "earned":null,"available":null,"percentage":null,"partial":false,"error":null},
             "max_points":9.0,"assigned_grade":null,"quality":"VERIFIED","stale":false,
             "sync_error":null,"last_synced_at":1790000000.0,"synthetic":false,"submission_id":"s3"},
            {"student_id":"u4","name":"Dev Doe","status":"NOT_STARTED","status_label":"Not started",
             "completion":"NOT_COMPLETED","completion_label":"Not completed","completion_reason":null,
             "submitted":false,"submitted_at":null,"late":false,"attachments":[],
             "correction":{"state":"NOT_CORRECTED","label":"Not corrected","job_status":null,
                           "earned":null,"available":null,"percentage":null,"partial":false,"error":null},
             "max_points":9.0,"assigned_grade":null,"quality":"VERIFIED","stale":false,
             "sync_error":null,"last_synced_at":1790000000.0,"synthetic":true,"submission_id":null},
            {"student_id":"u5","name":"Fawn Frost","status":"SUBMITTED","status_label":"Submitted",
             "completion":"MISSING","completion_label":"Missing",
             "completion_reason":"This record could not be synchronized, so its submission state is unverified.",
             "submitted":false,"submitted_at":null,"late":false,"attachments":[],
             "correction":{"state":"NOT_CORRECTED","label":"Not corrected","job_status":null,
                           "earned":null,"available":null,"percentage":null,"partial":false,"error":null},
             "max_points":9.0,"assigned_grade":null,"quality":"ERROR","stale":true,
             "sync_error":"403 from Google","last_synced_at":1789000000.0,"synthetic":false,
             "submission_id":"s5"}
          ],
          "summary": {
            "expected":5,"completed":2,"not_completed":2,"missing":1,"unknown":0,
            "completion_percentage":40.0,"submitted":3,"corrected":1,"failed":1,
            "invalid":0,"awaiting_correction":0,"stale":1,"sync_errors":1,"duplicate_records":0
          },
          "sync": {"last_sync":1790000000.0,"stale_after_seconds":1800,
                   "assignment_state":"ACTIVE","roster_known":true},
          "warnings": ["1 student record could not be synchronized and are classified as missing."]
        }
        """;

    [Fact]
    public async Task Loading_shows_the_assignment_record_the_roster_and_matching_counts()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new AssignmentDetailViewModel(session);
            session.OpenAssignment(CourseworkId);

            await page.LoadAsync();

            Assert.Null(page.Error);

            // The request went to the assignment that was opened.
            Assert.Contains(handler.Requests,
                r => r.Url.Contains("/api/assignments/cw-1/students", StringComparison.Ordinal));

            Assert.Equal("Ch2L3 - Simplifying Expressions", page.Heading);
            Assert.Contains("WBIS - O Level Mathematics - Mr. Abanout", page.HeaderDescription);
            Assert.Contains("Due 2026-10-16 23:59", page.HeaderDescription);

            Assert.Equal(5, page.Items.Count);
            Assert.Equal("5", page.ExpectedText);
            Assert.Equal("2", page.CompletedText);
            Assert.Equal("2", page.NotCompletedText);
            Assert.Equal("1", page.MissingText);
            Assert.Equal("0", page.UnknownText);
            Assert.Contains("40", page.CompletionText);

            // One row per student, and the buckets add up to the roster.
            Assert.Equal(5, page.Items.Select(r => r.StudentIdText).Distinct().Count());
            Assert.Equal(page.Items.Count,
                page.Items.Count(r => r.Completion == "COMPLETED")
                + page.Items.Count(r => r.Completion == "NOT_COMPLETED")
                + page.Items.Count(r => r.Completion == "MISSING")
                + page.Items.Count(r => r.Completion == "UNKNOWN"));

            var ada = page.Items.Single(r => r.Name == "Ada Lovelace");
            Assert.Equal("COMPLETED", ada.Completion);
            Assert.Equal("Submitted", ada.StatusLabel);
            Assert.Equal("Corrected · 7/9 (77.8%)", ada.CorrectionText);
            Assert.Equal("ada.pdf", ada.AttachmentText);
            Assert.True(ada.HasAttachmentLink);

            Assert.Contains(page.Facts, f => f.Label == "Assignment ID" && f.Value == "cw-1");
            Assert.Contains(page.Facts, f => f.Label == "Mark scheme" && f.Value == "Attached");
            Assert.Contains(page.Facts, f => f.Label == "Description" && f.Value == "Simplify each expression.");
            Assert.True(page.HasWarnings);
            Assert.Single(page.Warnings);
            Assert.Contains("Roster synced", page.SyncText);
        }
    }

    [Fact]
    public async Task A_failed_correction_stays_completed_and_says_why_it_failed()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new AssignmentDetailViewModel(session);
            session.OpenAssignment(CourseworkId);
            await page.LoadAsync();

            var ben = page.Items.Single(r => r.Name == "Ben Bray");
            Assert.Equal("COMPLETED", ben.Completion);
            Assert.Equal("Correction failed", ben.CorrectionLabel);
            Assert.Equal("Correction failed", ben.CorrectionText);
            Assert.Equal("Not marked", ben.MarksText);
            // The failure is the warning, not a second "missing" claim.
            Assert.Equal("The Gemini request timed out.", ben.Warning);
            Assert.Contains("late", ben.SubmittedText);
        }
    }

    [Fact]
    public async Task A_draft_and_a_roster_student_are_not_completed_and_not_missing()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new AssignmentDetailViewModel(session);
            session.OpenAssignment(CourseworkId);
            await page.LoadAsync();

            var cleo = page.Items.Single(r => r.Name == "Cleo Chan");
            Assert.Equal("NOT_COMPLETED", cleo.Completion);
            Assert.Contains("nothing was handed in", cleo.CompletionText);
            Assert.Equal("No submission", cleo.SubmittedText);

            var dev = page.Items.Single(r => r.Name == "Dev Doe");
            Assert.Equal("NOT_COMPLETED", dev.Completion);
            Assert.Equal("Not started", dev.StatusLabel);

            // The unverifiable record is the only "Missing" row.
            var fawn = page.Items.Single(r => r.Name == "Fawn Frost");
            Assert.Equal("MISSING", fawn.Completion);
            Assert.Equal("Sync error: 403 from Google", fawn.Warning);
            Assert.True(fawn.HasWarning);
        }
    }

    [Fact]
    public async Task Filters_narrow_the_rows_without_changing_the_records()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new AssignmentDetailViewModel(session);
            session.OpenAssignment(CourseworkId);
            await page.LoadAsync();

            page.StatusFilter = "Completed";
            Assert.Equal(2, page.Items.Count);
            // The summary still describes the whole assignment, not the filter.
            Assert.Equal("5", page.ExpectedText);
            Assert.Equal("2", page.CompletedText);

            page.CorrectionFilter = "Correction failed";
            Assert.Single(page.Items);
            Assert.Equal("Ben Bray", page.Items[0].Name);

            // A null write from a rebuilt combo means "no filter", not "no rows".
            page.CorrectionFilter = null!;
            page.StatusFilter = "Missing";
            Assert.Single(page.Items);
            Assert.Equal("Fawn Frost", page.Items[0].Name);

            page.StatusFilter = "All statuses";
            page.Filter = "ada";
            Assert.Single(page.Items);
            Assert.Equal("Ada Lovelace", page.Items[0].Name);

            page.Filter = string.Empty;
            Assert.Equal(5, page.Items.Count);
        }
    }

    [Fact]
    public async Task Sorting_by_status_puts_the_rows_that_need_attention_first()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new AssignmentDetailViewModel(session);
            session.OpenAssignment(CourseworkId);
            await page.LoadAsync();

            Assert.Equal("Ada Lovelace", page.Items[0].Name); // default: by name

            page.SortBy = "Status";
            Assert.Equal("Fawn Frost", page.Items[0].Name);   // missing first
            Assert.Equal("Missing", page.Items[0].CompletionLabel);
            // Completed rows sort after the ones that need action; both
            // handed-in students end the list.
            Assert.Equal("COMPLETED", page.Items[^1].Completion);
            Assert.Equal("COMPLETED", page.Items[^2].Completion);

            page.SortBy = "Submitted";
            Assert.Contains(page.Items.Take(2), r => r.Name == "Ada Lovelace");
            Assert.Contains(page.Items.Take(2), r => r.Name == "Ben Bray");
            // Students who never handed anything in sort last.
            Assert.Equal("No submission", page.Items[^1].SubmittedText);
        }
    }

    [Fact]
    public async Task Refreshing_reloads_the_same_assignment()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new AssignmentDetailViewModel(session);
            session.OpenAssignment(CourseworkId);
            await page.LoadAsync();
            await page.LoadAsync();

            // Two roster reads, and nothing that names another assignment.
            Assert.Equal(2, handler.Requests.Count(
                r => r.Url.Contains("/api/assignments/cw-1/students", StringComparison.Ordinal)));
            Assert.All(handler.Requests,
                request => Assert.Contains("/api/assignments/cw-1/", request.Url));
            Assert.Equal(5, page.Items.Count);
            Assert.Null(page.Error);
        }
    }

    [Fact]
    public async Task An_unknown_assignment_clears_the_previous_roster_and_reports_the_backend_message()
    {
        var handler = new DetailHandler { Missing = "cw-deleted" };
        using (handler)
        {
            var session = await SessionAsync(handler);
            var page = new AssignmentDetailViewModel(session);
            session.OpenAssignment(CourseworkId);
            await page.LoadAsync();
            Assert.Equal(5, page.Items.Count);

            session.OpenAssignment("cw-deleted");
            await page.LoadAsync();

            Assert.Empty(page.Items);
            Assert.Equal("0", page.ExpectedText);
            Assert.Contains("not in the local database", page.Error);
            Assert.Equal("Assignment details", page.Heading);
        }
    }

    [Fact]
    public async Task The_page_explains_itself_when_no_assignment_is_open()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new AssignmentDetailViewModel(session);
            await page.LoadAsync();

            Assert.Empty(page.Items);
            Assert.Null(page.Error);
            Assert.Contains("Choose an assignment", page.Status);
            Assert.Empty(handler.Requests);
        }
    }

    [Fact]
    public async Task Opening_an_assignment_navigates_with_that_identity()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            string? navigated = null;
            session.Navigate = key => navigated = key;

            var list = new AssignmentsViewModel(session);
            list.OpenDetails(new AssignmentDto { Id = "cw-1", Title = "Ch2L3 - Simplifying Expressions" });

            Assert.Equal("assignment", navigated);
            Assert.Equal("cw-1", session.TakePendingAssignmentId());
            // The identity is consumed once: a later load cannot replay it.
            Assert.Null(session.TakePendingAssignmentId());

            // The name column's command is the same entry point.
            list.OpenAssignmentCommand.Execute(new AssignmentDto { Id = "cw-2" });
            Assert.Equal("cw-2", session.TakePendingAssignmentId());

            // A command with nothing to open does nothing at all.
            list.OpenDetails(null);
            Assert.Null(session.TakePendingAssignmentId());
        }
    }

    private static async Task<(AppSession Session, DetailHandler Handler)> SessionAsync()
    {
        var handler = new DetailHandler();
        return (await SessionAsync(handler), handler);
    }

    private static async Task<AppSession> SessionAsync(DetailHandler handler)
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

    /// <summary>Answers the page's two reads by URL: the roster, then the mark
    /// scheme. A queued-response stub would drift the moment the page reads one
    /// more thing about the same assignment.</summary>
    private sealed class DetailHandler : HttpMessageHandler
    {
        public List<RecordedRequest> Requests { get; } = new();

        /// <summary>An assignment id the backend says no longer exists.</summary>
        public string? Missing { get; set; }

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            var url = request.RequestUri!.AbsoluteUri;
            Requests.Add(new RecordedRequest(request.Method.Method, url, null,
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)));

            if (Missing is not null && url.Contains("/assignments/" + Missing + "/", StringComparison.Ordinal))
            {
                return Task.FromResult(Json(HttpStatusCode.NotFound,
                    """{"code":"not_found","message":"That assignment is not in the local database. Sync Google Classroom first."}"""));
            }

            if (url.Contains("/mark-scheme", StringComparison.Ordinal))
            {
                return Task.FromResult(Json(HttpStatusCode.OK, """
                    {"coursework_id":"cw-1","has_mark_scheme":true,"file_name":"ch2l3-ms.pdf",
                     "pages":21,"processed":true,"total_marks":9,"subquestion_count":9,
                     "verification_state":"VERIFIED","warnings":[]}
                    """));
            }

            return Task.FromResult(Json(HttpStatusCode.OK, StudentsBody));
        }

        private static HttpResponseMessage Json(HttpStatusCode status, string body) =>
            new(status)
            {
                Content = new StringContent(body, System.Text.Encoding.UTF8, "application/json"),
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
