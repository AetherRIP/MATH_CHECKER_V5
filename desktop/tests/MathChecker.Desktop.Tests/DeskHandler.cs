using System.Net;
using System.Text;
using System.Text.Json;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// Routes the desk's real URLs (detail, pages, question mark, override,
/// reviews, list) so tests exercise the client's paths, not call order.
/// </summary>
internal sealed class DeskHandler : HttpMessageHandler
{
    public const string DetailJson = """
        {
          "id":"r1","job_id":"j1","student_name":"Amara O.","display_name":"Amara O.",
          "class_label":"10C","earned":10,"available":16,"percentage":62.5,
          "provisional_grade":"6","confidence":"LOW","review_required":1,"manual_override":0,
          "created_at":1792531408.0,
          "result_json":{"questions":[
            {"question_number":"1","max_marks":3,"awarded_marks":2,"topic":"Algebra",
             "student_answer":"x = 4","loss_reason":"Wrong substitution",
             "mark_breakdown":[{"code":"M1","awarded":true,"reason":"Correct substitution"},
                               {"code":"A1","awarded":false,"reason":"Final value"}],
             "status":"VALID"},
            {"question_number":"2","max_marks":4,"awarded_marks":4,"topic":"Algebra","status":"VALID"},
            {"question_number":"3(a)","max_marks":2,"awarded_marks":1,"topic":"Geometry",
             "status":"REVIEW","review_reason":"Handwriting unclear"},
            {"question_number":"4","max_marks":2,"awarded_marks":0,"topic":"Geometry",
             "scheme_check":{"disagrees_with_scheme":true,"student_answer_valid":true,
               "scheme_answer":"7","independently_verified_answer":"7",
               "evidence":"The scheme adds incorrectly","discrepancy_type":"arithmetic",
               "confidence":0.8,"justified_marks":2}},
            {"question_number":"10","max_marks":5,"awarded_marks":3,"topic":"Number","status":"VALID"}
          ]},
          "verification_json":{"verified":false,"partial":false,
            "coverage":{"expected":5,"marked":5,"requires_review":1,"unanswered":0},
            "review_questions":["3(a)"],"anomalies":[],"warnings":[]},
          "confidence_reasons":["One question needs review"]
        }
        """;

    public const string PartialDetailJson = """
        {
          "id":"r1","job_id":"j1","student_name":"Amara O.","display_name":"Amara O.",
          "class_label":"10C","earned":9,"available":11,"percentage":81.8,
          "provisional_grade":"8","confidence":"LOW","review_required":1,"manual_override":0,
          "created_at":1792531408.0,
          "result_json":{"questions":[
            {"question_number":"1","max_marks":3,"awarded_marks":2,"topic":"Algebra","status":"VALID"},
            {"question_number":"2","max_marks":4,"awarded_marks":4,"topic":"Algebra","status":"VALID"},
            {"question_number":"3","max_marks":2,"awarded_marks":1,"topic":"Geometry","status":"VALID"},
            {"question_number":"4","max_marks":2,"awarded_marks":2,"topic":"Geometry","status":"VALID"}
          ]},
          "verification_json":{"verified":false,"partial":true,
            "coverage":{"expected":60,"marked":22,"requires_review":0,"unanswered":38},
            "paper_total_marks":60,"unresolved_marks":45,
            "review_questions":[],"anomalies":[],"warnings":[]}
        }
        """;

    public string Detail { get; set; } = DetailJson;

    public string Reviews { get; set; } = """{"result_id":"r1","history":[]}""";

    public Func<HttpResponseMessage>? MarkResponse { get; set; }

    public Func<HttpResponseMessage>? OverrideResponse { get; set; }

    public List<RecordedRequest> Requests { get; } = new();

    public List<string> MarkBodies { get; } = new();

    public List<string> OverrideBodies { get; } = new();

    public int PageRequests { get; private set; }

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var path = request.RequestUri!.AbsolutePath;
        var body = request.Content is null
            ? null
            : await request.Content.ReadAsStringAsync(cancellationToken);
        Requests.Add(new RecordedRequest(request.Method.Method, request.RequestUri.AbsoluteUri,
            body, new Dictionary<string, string>()));

        if (path.EndsWith("/pages", StringComparison.Ordinal))
        {
            return PageHandler.Json(PageHandler.Available(3));
        }

        if (path.Contains("/pages/", StringComparison.Ordinal)
            && path.EndsWith(".png", StringComparison.OrdinalIgnoreCase))
        {
            PageRequests++;
            return PageHandler.Png(1);
        }

        if (path.EndsWith("/reviews", StringComparison.Ordinal))
        {
            return PageHandler.Json(Reviews);
        }

        if (path.EndsWith("/mark", StringComparison.Ordinal)
            && request.Method == HttpMethod.Post)
        {
            MarkBodies.Add(body ?? string.Empty);
            return MarkResponse?.Invoke() ?? PageHandler.Json("""
                {"updated":true,"result_id":"r1","question_number":"1","awarded":3,
                 "previous":2,"earned":11,"available":16,"percentage":68.75}
                """);
        }

        if (path.EndsWith("/override", StringComparison.Ordinal)
            && request.Method == HttpMethod.Post)
        {
            OverrideBodies.Add(body ?? string.Empty);
            return OverrideResponse?.Invoke() ?? PageHandler.Json(
                """{"overridden":true,"result_id":"r1","action":"approved"}""");
        }

        if (path.EndsWith("/remark", StringComparison.Ordinal))
        {
            return PageHandler.Json("""{"remarked":true,"result_id":"r1","job_id":"j2"}""");
        }

        if (path.EndsWith("/results", StringComparison.Ordinal)
            && request.Method == HttpMethod.Get)
        {
            return PageHandler.Json("""
                {"results":[
                  {"id":"r1","student_name":"Amara O.","earned":10,"available":16,
                   "percentage":62.5,"created_at":1792531408.0},
                  {"id":"r2","student_name":"Ben S.","earned":8,"available":16,
                   "percentage":50.0,"created_at":1792531000.0}
                ],"total":2,"limit":300,"offset":0}
                """);
        }

        if (request.Method == HttpMethod.Get
            && path.Contains("/results/", StringComparison.Ordinal))
        {
            return PageHandler.Json(Detail);
        }

        return PageHandler.Error(HttpStatusCode.NotFound, "unexpected_url",
            "No stub for " + path);
    }

    public static string ApprovedHistory => """
        {"result_id":"r1","history":[
          {"action":"approved","previous_earned":10,"new_earned":10,
           "previous_confidence":"LOW","note":"Approved in the marking desk",
           "created_at":1792532000.0}
        ]}
        """;

    public static HttpResponseMessage Error(HttpStatusCode status, string code, string message) =>
        PageHandler.Error(status, code, message);

    public static HttpResponseMessage Json(string json) => PageHandler.Json(json);

    public static HttpResponseMessage Png() => PageHandler.Png(1);

    public static bool TryReadInt(string json, string property, out int value)
    {
        using var document = JsonDocument.Parse(json);
        if (document.RootElement.TryGetProperty(property, out var element)
            && element.TryGetInt32(out value))
        {
            return true;
        }

        value = 0;
        return false;
    }

    public static string Utf8(string json) => Encoding.UTF8.GetString(Encoding.UTF8.GetBytes(json));
}
