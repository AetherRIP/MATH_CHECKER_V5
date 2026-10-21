using System.Net;
using System.Text;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The Marking Desk's client contract: page metadata, rendered page bytes and
/// the two write paths whose integer types the backend enforces.
/// </summary>
public class PageClientTests
{
    private static MathCheckerApiClient Client(StubHttpHandler handler)
    {
        var client = new MathCheckerApiClient(handler);
        client.SetBaseUrl("http://127.0.0.1:8642");
        return client;
    }

    [Fact]
    public async Task Pages_metadata_uses_the_viewer_route_and_keeps_the_reason()
    {
        const string body = """
            {"result_id":"r1","available":false,"reason":"retention_disabled",
             "message":"Submission retention is OFF.","retryable":false,
             "retention":{"keep_submissions":"off","keep_days":0,"keeps":false},
             "page_count":0,"dpi":150,
             "page_url_template":"/api/results/r1/pages/{page}.png"}
            """;
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, body);
        using var client = Client(handler);

        var pages = await client.ResultPagesAsync("r1");

        Assert.Equal("http://127.0.0.1:8642/api/results/r1/pages", handler.Requests[0].Url);
        Assert.Equal("GET", handler.Requests[0].Method);
        Assert.False(pages.Available);
        Assert.Equal("retention_disabled", pages.Reason);
    }

    [Fact]
    public async Task Page_image_requests_the_dpi_and_returns_the_bytes()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, "PNGDATA", "image/png");
        using var client = Client(handler);

        var bytes = await client.ResultPagePngAsync("r1", 2, dpi: 220);

        Assert.Equal("http://127.0.0.1:8642/api/results/r1/pages/2.png?dpi=220",
            handler.Requests[0].Url);
        Assert.Equal("PNGDATA", Encoding.UTF8.GetString(bytes));
    }

    [Fact]
    public async Task Page_image_defaults_to_the_backends_own_dpi()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, "PNG", "image/png");
        using var client = Client(handler);

        await client.ResultPagePngAsync("r1", 1);

        Assert.EndsWith("/pages/1.png?dpi=150", handler.Requests[0].Url);
    }

    [Fact]
    public async Task Missing_original_preserves_the_backend_error_code()
    {
        const string body = """
            {"code":"original_not_stored","message":"The original submission file is not stored on this device."}
            """;
        var handler = new StubHttpHandler().Respond(HttpStatusCode.NotFound, body);
        using var client = Client(handler);

        var error = await Assert.ThrowsAsync<ApiException>(() => client.ResultPagePngAsync("r1", 3));

        Assert.Equal(404, error.Status);
        Assert.Equal("original_not_stored", error.Code);
        Assert.Contains("not stored", error.Message);
    }

    [Fact]
    public async Task A_json_body_where_an_image_was_expected_is_rejected()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, "{}");
        using var client = Client(handler);

        var error = await Assert.ThrowsAsync<ApiException>(() => client.ResultPagePngAsync("r1", 1));

        Assert.Equal("unexpected_content_type", error.Code);
        Assert.Contains("image/png", error.Message);
    }

    [Fact]
    public async Task Question_mark_is_sent_as_a_whole_number()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, """{"updated":true}""");
        using var client = Client(handler);

        await client.SaveQuestionMarkAsync("r1", "4(b)", 3, "Set in the marking desk");

        var request = handler.Requests[0];
        Assert.Equal("POST", request.Method);
        Assert.Contains("/api/results/r1/questions/4%28b%29/mark", request.Url);
        Assert.Contains("\"awarded\":3", request.Body);
        Assert.DoesNotContain("\"awarded\":3.0", request.Body);
        Assert.Contains("Set in the marking desk", request.Body);
    }

    [Fact]
    public async Task Approval_omits_the_earned_key()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, """{"overridden":true}""");
        using var client = Client(handler);

        await client.OverrideResultAsync("r1", null, "Reviewed in the marking desk");

        var body = handler.Requests[0].Body!;
        Assert.Contains("\"note\":\"Reviewed in the marking desk\"", body);
        Assert.DoesNotContain("\"earned\"", body);
    }

    [Fact]
    public async Task Override_with_a_mark_sends_an_integer()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, """{"overridden":true}""");
        using var client = Client(handler);

        await client.OverrideResultAsync("r1", 14, "Whole-paper mark");

        var body = handler.Requests[0].Body!;
        Assert.Contains("\"earned\":14", body);
        Assert.DoesNotContain("\"earned\":14.0", body);
    }

    [Fact]
    public async Task Review_history_is_deserialised_into_the_decision_type()
    {
        const string body = """
            {"result_id":"r1","history":[
              {"action":"approved","previous_earned":14,"new_earned":14,
               "previous_confidence":"LOW","note":"Reviewed","created_at":1791815824.0}]}
            """;
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, body);
        using var client = Client(handler);

        var history = await client.ResultReviewHistoryAsync("r1");

        var entry = Assert.Single(history.History);
        Assert.Equal("approved", entry.Action);
        Assert.Equal("Approved", entry.ActionText);
        Assert.Equal(1791815824.0, entry.CreatedAt!.Value, 3);
    }
}
