using System.Net;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Tests;

public class ApiClientTests
{
    private static MathCheckerApiClient Client(StubHttpHandler handler)
    {
        var client = new MathCheckerApiClient(handler);
        client.SetBaseUrl("http://127.0.0.1:8642");
        return client;
    }

    [Fact]
    public async Task Dashboard_deserializes_the_real_shape()
    {
        // Copied from a live /api/dashboard response (demo data).
        const string body = """
            {"corrections_today":0,"pending":0,"processing":0,"completed":8,"failed":0,
             "jobs":{"COMPLETED":8},
             "api_health":{"healthy":0,"cooldown":0,"invalid":0,"total":0,"has_keys":false,
                           "aggregate":{"requests":0,"successes":0,"failures":0}},
             "classroom":{"configured":false,"connected":false,"email":null,"courses":2,
                          "allowlisted":3,"pending_submissions":0},
             "role":"teacher","mock_mode":true,"version":"5.2.1","build_number":"20261013-rc1",
             "demo_mode":true,"first_run":true}
            """;
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, body);
        using var client = Client(handler);

        var dashboard = await client.DashboardAsync();

        Assert.Equal(8, dashboard.Completed);
        Assert.Equal(2, dashboard.Classroom!.Courses);
        Assert.False(dashboard.ApiHealth!.HasKeys);
        Assert.Equal("5.2.1", dashboard.Version);
        Assert.True(dashboard.DemoMode);
        Assert.Equal("http://127.0.0.1:8642/api/dashboard", handler.Requests[0].Url);
    }

    [Fact]
    public async Task Results_list_reads_status_and_score_fields()
    {
        const string body = """
            {"results":[
              {"id":"r1","student_name":"Student A","display_name":"Student A","earned":16,
               "available":20,"percentage":80.0,"provisional_grade":"8","confidence":"HIGH",
               "review_required":0,"manual_override":0,"created_at":1791815824.008,
               "assignment_title":"Algebra","status":"corrected","class_label":"10A"}],
             "total":1,"limit":100,"offset":0}
            """;
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, body);
        using var client = Client(handler);

        var results = await client.ResultsAsync();

        var item = Assert.Single(results.Results);
        Assert.Equal("16 / 20", item.ScoreText);
        Assert.Equal("80%", item.PercentageText);
        Assert.Equal("Corrected", item.StatusLabel);
        Assert.Equal("Student A", item.Name);
    }

    [Fact]
    public async Task Results_query_parameters_are_escaped_and_typed()
    {
        var handler = new StubHttpHandler()
            .Respond(HttpStatusCode.OK, """{"results":[],"total":0,"limit":50,"offset":0}""");
        using var client = Client(handler);

        await client.ResultsAsync(q: "a b&c", status: "needs_review", limit: 50);

        var url = handler.Requests[0].Url;
        Assert.Contains("q=a%20b%26c", url);
        Assert.Contains("status=needs_review", url);
        Assert.Contains("limit=50", url);
    }

    [Fact]
    public async Task Backend_error_is_surfaced_with_its_code_and_status()
    {
        const string body = """{"code":"mark_scheme_missing","message":"Upload the mark scheme first."}""";
        var handler = new StubHttpHandler().Respond(HttpStatusCode.BadRequest, body);
        using var client = Client(handler);

        var error = await Assert.ThrowsAsync<ApiException>(() => client.CorrectAllAsync("c1", "w1", true));

        Assert.Equal(400, error.Status);
        Assert.Equal("mark_scheme_missing", error.Code);
        Assert.Equal("Upload the mark scheme first.", error.Message);
        Assert.Contains("mark_scheme_missing", error.Display);
    }

    [Fact]
    public async Task Forbidden_is_recognised_for_developer_only_endpoints()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.Forbidden,
            """{"code":"forbidden","message":"Developer role required."}""");
        using var client = Client(handler);

        var error = await Assert.ThrowsAsync<ApiException>(() => client.DiagnosticsAsync());

        Assert.True(error.IsForbidden);
        Assert.Equal("Developer role required.", error.Message);
    }

    [Fact]
    public async Task Csrf_header_is_sent_once_the_session_provides_one()
    {
        var handler = new StubHttpHandler()
            .Respond(HttpStatusCode.OK, """{"results":[]}""");
        using var client = Client(handler);
        client.CsrfToken = "token-123";

        await client.ResultsAsync();

        Assert.Equal("token-123", handler.Requests[0].Headers["x-mc5-csrf"]);
    }

    [Fact]
    public async Task Download_uses_the_filename_the_backend_chose()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, "%PDF-1.4", "application/pdf",
            "attachment; filename=\"Report_Student_A.pdf\"");
        using var client = Client(handler);

        var file = await client.ExportResultAsync("r1", "pdf");

        Assert.Equal("Report_Student_A.pdf", file.FileName);
        Assert.Equal("application/pdf", file.ContentType);
        Assert.Equal(8, file.Bytes.Length);
        Assert.Contains("format=pdf", handler.Requests[0].Url);
    }

    [Fact]
    public async Task Download_prefers_the_rfc5987_name_for_a_non_ascii_student()
    {
        // The backend sends an ASCII fallback plus the real name; the app must
        // use the real one so an exported report is not saved as "student_.pdf".
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, "%PDF-1.4", "application/pdf",
            "attachment; filename=\"student____10_20261022_101500.pdf\"; "
            + "filename*=UTF-8''%D8%AE%D8%A7%D9%84%D8%AF_10_20261022_101500.pdf");
        using var client = Client(handler);

        var file = await client.ExportResultAsync("r1", "pdf");

        Assert.Equal("خالد_10_20261022_101500.pdf", file.FileName);
    }

    [Fact]
    public async Task Download_never_writes_a_path_out_of_a_hostile_filename()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, "%PDF-1.4", "application/pdf",
            "attachment; filename=\"../../etc/passwd.pdf\"");
        using var client = Client(handler);

        var file = await client.ExportResultAsync("r1", "pdf");

        // Separators are neutralised, so the save dialog's folder still wins.
        Assert.DoesNotContain("/", file.FileName);
        Assert.DoesNotContain("\\", file.FileName);
        var folder = Path.Combine(Path.GetTempPath(), "mc5-export-check");
        var resolved = Path.GetFullPath(Path.Combine(folder, file.FileName));
        Assert.StartsWith(Path.GetFullPath(folder), resolved);
    }

    [Fact]
    public async Task Unified_upload_sends_both_files_and_form_fields()
    {
        var temp = Directory.CreateTempSubdirectory("mc5-upload-");
        try
        {
            var submission = Path.Combine(temp.FullName, "answers.pdf");
            var scheme = Path.Combine(temp.FullName, "scheme.pdf");
            await File.WriteAllTextAsync(submission, "STUDENT");
            await File.WriteAllTextAsync(scheme, "SCHEME");

            var handler = new StubHttpHandler().Respond(HttpStatusCode.OK,
                """{"job_id":"abc","page_count":4,"reason":"standard"}""");
            using var client = Client(handler);

            var result = await client.SubmitUnifiedCorrectionAsync(submission, scheme, "Student A",
                null, CancellationToken.None);

            Assert.Equal("abc", result.GetProperty("job_id").GetString());

            var request = handler.Requests[0];
            Assert.Equal("POST", request.Method);
            Assert.Contains("multipart/form-data", request.Headers["Content-Type"]);
            Assert.Contains("name=student_name", request.Body);
            Assert.Contains("Student A", request.Body);
            Assert.DoesNotContain("name=pipeline", request.Body);
            Assert.Contains("name=submission", request.Body);
            Assert.Contains("answers.pdf", request.Body);
            Assert.Contains("name=mark_scheme", request.Body);
            Assert.Contains("scheme.pdf", request.Body);
        }
        finally
        {
            temp.Delete(true);
        }
    }

    [Fact]
    public async Task Result_detail_reads_the_engine_question_keys()
    {
        // Keys exactly as legacy_marking.normalise_result writes them. The old
        // fixture used "number"/"awarded"/"max", which the engine never emits
        // for a new correction, so the marks column silently showed — / —.
        const string body = """
            {"id":"r1","student_name":"Student A","earned":5,"available":6,"percentage":83.3,
             "verification_json":{"verified":true,"status":"VALID","anomalies":[],"warnings":[],
                                  "review_questions":[]},
             "result_json":{"questions":[
               {"question_number":"1","awarded_marks":2,"max_marks":2,"topic":"Fractions",
                "loss_reason":""},
               {"question_number":"2","awarded_marks":3,"max_marks":4,"topic":"Algebra",
                "loss_reason":"sign error"}]}}
            """;
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, body);
        using var client = Client(handler);

        var detail = await client.ResultAsync("r1");

        var questions = detail.Questions();
        Assert.Equal(2, questions.Count);
        Assert.Equal("1", questions[0].NumberText);
        Assert.Equal("2 / 2", questions[0].MarkText);
        Assert.Equal("3 / 4", questions[1].MarkText);
        Assert.Equal("sign error", questions[1].LossReason);
        Assert.True(detail.Verification()!.Verified);
        Assert.Equal("VALID", detail.Verification()!.Status);
    }

    [Fact]
    public async Task Result_detail_still_reads_the_older_short_question_keys()
    {
        const string body = """
            {"id":"r1","result_json":{"questions":[
              {"number":"3","awarded":1,"max":2,"topic":"Geometry"}]}}
            """;
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, body);
        using var client = Client(handler);

        var detail = await client.ResultAsync("r1");

        var question = Assert.Single(detail.Questions());
        Assert.Equal("3", question.NumberText);
        Assert.Equal("1 / 2", question.MarkText);
    }

    [Fact]
    public async Task Result_detail_without_a_question_array_reports_none()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK,
            """{"id":"r1","result_json":{},"earned":1,"available":2}""");
        using var client = Client(handler);

        var detail = await client.ResultAsync("r1");

        Assert.Empty(detail.Questions());
    }

    [Fact]
    public void File_names_from_the_backend_are_sanitised()
    {
        Assert.Equal("a_b_c.pdf", MathCheckerApiClient.SanitizeFileName("a/b\\c.pdf"));
        Assert.Equal("download", MathCheckerApiClient.SanitizeFileName("   "));
    }
}
