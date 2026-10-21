using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// Answers the two page-viewer routes by URL instead of by call order, so a
/// prefetch or a retry cannot consume the wrong queued response. A gate can
/// hold an image response open to exercise in-flight de-duplication.
/// </summary>
internal sealed class PageHandler : HttpMessageHandler
{
    private readonly object _gate = new();
    private readonly List<int> _imagePages = new();

    public string Metadata { get; set; } = """{"available":false,"reason":"no_link","page_count":0}""";

    public Func<HttpResponseMessage>? MetadataResponse { get; set; }

    public Func<int, HttpResponseMessage>? Image { get; set; }

    public TaskCompletionSource? Gate { get; set; }

    public List<string> Urls { get; } = new();

    public int MetadataRequests { get; private set; }

    public IReadOnlyList<int> ImagePages
    {
        get
        {
            lock (_gate)
            {
                return _imagePages.ToArray();
            }
        }
    }

    public int ImageRequests => ImagePages.Count;

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var path = request.RequestUri!.AbsolutePath;
        lock (_gate)
        {
            Urls.Add(request.RequestUri.AbsoluteUri);
        }

        if (path.EndsWith("/pages", StringComparison.Ordinal))
        {
            MetadataRequests++;
            return MetadataResponse?.Invoke() ?? Json(Metadata);
        }

        if (TryPage(path, out var page))
        {
            lock (_gate)
            {
                _imagePages.Add(page);
            }

            if (Gate is not null)
            {
                await Gate.Task.WaitAsync(cancellationToken);
            }

            return Image?.Invoke(page) ?? Png(page);
        }

        return Error(HttpStatusCode.NotFound, "unexpected_url", "No stub for " + path);
    }

    public static string Available(int pages = 3, string fileName = "answers.pdf") => $$"""
        {"result_id":"r1","available":true,"reason":null,"message":null,"retryable":false,
         "job_status":"COMPLETED",
         "retention":{"keep_submissions":"always","keep_days":0,"keeps":true},
         "checked_at":1791815824.0,"page_count":{{pages}},"file_kind":"pdf",
         "file_name":"{{fileName}}","source":"retained","dpi":150,
         "page_url_template":"/api/results/r1/pages/{page}.png"}
        """;

    public static string Missing(string reason, string message) => $$"""
        {"result_id":"r1","available":false,"reason":"{{reason}}","message":"{{message}}",
         "retryable":false,"job_status":"COMPLETED",
         "retention":{"keep_submissions":"off","keep_days":0,"keeps":false},
         "checked_at":1791815824.0,"page_count":0,"dpi":150,
         "page_url_template":"/api/results/r1/pages/{page}.png"}
        """;

    public static HttpResponseMessage Json(string json) =>
        new(HttpStatusCode.OK)
        {
            Content = new StringContent(json, Encoding.UTF8, "application/json"),
        };

    public static HttpResponseMessage Png(int page)
    {
        var content = new ByteArrayContent(Encoding.UTF8.GetBytes($"PAGE{page}"));
        content.Headers.ContentType = new MediaTypeHeaderValue("image/png");
        return new HttpResponseMessage(HttpStatusCode.OK) { Content = content };
    }

    public static HttpResponseMessage Error(HttpStatusCode status, string code, string message)
    {
        var body = JsonSerializer.Serialize(new { code, message });
        return new HttpResponseMessage(status)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json"),
        };
    }

    private static bool TryPage(string path, out int page)
    {
        const string marker = "/pages/";
        var index = path.LastIndexOf(marker, StringComparison.Ordinal);
        if (index >= 0)
        {
            var tail = path[(index + marker.Length)..];
            if (tail.EndsWith(".png", StringComparison.OrdinalIgnoreCase)
                && int.TryParse(tail[..^4], out page))
            {
                return true;
            }
        }

        page = 0;
        return false;
    }
}
