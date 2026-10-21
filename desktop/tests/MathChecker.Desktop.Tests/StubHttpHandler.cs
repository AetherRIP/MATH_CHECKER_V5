using System.Net;
using System.Text;

namespace MathChecker.Desktop.Tests;

/// <summary>Records every request and returns a queued response.</summary>
public sealed class StubHttpHandler : HttpMessageHandler
{
    private readonly Queue<Func<HttpRequestMessage, HttpResponseMessage>> _responses = new();

    public List<RecordedRequest> Requests { get; } = new();

    public StubHttpHandler Respond(HttpStatusCode status, string body, string contentType = "application/json",
        string? contentDisposition = null)
    {
        _responses.Enqueue(request =>
        {
            var response = new HttpResponseMessage(status)
            {
                Content = new StringContent(body, Encoding.UTF8, contentType),
                RequestMessage = request,
            };
            if (contentDisposition is not null)
            {
                response.Content.Headers.TryAddWithoutValidation("Content-Disposition", contentDisposition);
            }

            return response;
        });
        return this;
    }

    public StubHttpHandler Respond(Func<HttpRequestMessage, HttpResponseMessage> factory)
    {
        _responses.Enqueue(factory);
        return this;
    }

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        var body = request.Content is null
            ? null
            : await request.Content.ReadAsStringAsync(cancellationToken);
        var headers = request.Headers.ToDictionary(h => h.Key, h => string.Join(",", h.Value),
            StringComparer.OrdinalIgnoreCase);
        if (request.Content is not null)
        {
            foreach (var header in request.Content.Headers)
            {
                headers[header.Key] = string.Join(",", header.Value);
            }
        }

        // AbsoluteUri keeps the escaping (ToString() would decode %20 back to a
        // space and make the assertion meaningless).
        Requests.Add(new RecordedRequest(request.Method.Method, request.RequestUri!.AbsoluteUri,
            body, headers));
        if (_responses.Count == 0)
        {
            throw new InvalidOperationException("No stub response queued for " + request.RequestUri);
        }

        return _responses.Dequeue()(request);
    }
}

public sealed record RecordedRequest(string Method, string Url, string? Body,
    Dictionary<string, string> Headers);
