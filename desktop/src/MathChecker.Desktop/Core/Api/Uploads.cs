using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Core.Api;

/// <summary>Wraps a file stream and reports bytes written so the UI can show
/// upload progress for large PDFs without a fake animation.</summary>
public sealed class ProgressStreamContent : HttpContent
{
    private readonly Stream _stream;
    private readonly long _length;
    private readonly IProgress<double>? _progress;
    private readonly int _bufferSize;

    public ProgressStreamContent(Stream stream, long length, IProgress<double>? progress,
        int bufferSize = 81920)
    {
        _stream = stream;
        _length = length;
        _progress = progress;
        _bufferSize = bufferSize;
    }

    protected override async Task SerializeToStreamAsync(Stream stream, TransportContext? context)
    {
        var buffer = new byte[_bufferSize];
        long sent = 0;
        int read;
        while ((read = await _stream.ReadAsync(buffer).ConfigureAwait(false)) > 0)
        {
            await stream.WriteAsync(buffer.AsMemory(0, read)).ConfigureAwait(false);
            sent += read;
            if (_length > 0)
            {
                _progress?.Report(Math.Min(1d, sent / (double)_length));
            }
        }
    }

    protected override bool TryComputeLength(out long length)
    {
        length = _length;
        return _length >= 0;
    }
}

/// <summary>Parses the backend's text/event-stream endpoints.</summary>
public static class SseParser
{
    /// <summary>
    /// Reads <c>data: {...}</c> events until the stream ends or the token is
    /// cancelled. Comments (<c>: keepalive</c>) and blank separators are skipped.
    /// </summary>
    public static async Task ReadAsync(Stream stream, Func<JsonElement, Task> onEvent,
        CancellationToken cancellationToken)
    {
        using var reader = new StreamReader(stream, Encoding.UTF8, detectEncodingFromByteOrderMarks: false,
            bufferSize: 4096, leaveOpen: true);

        while (!cancellationToken.IsCancellationRequested)
        {
            var line = await reader.ReadLineAsync(cancellationToken).ConfigureAwait(false);
            if (line is null)
            {
                break;
            }

            if (!line.StartsWith("data:", StringComparison.Ordinal))
            {
                continue;
            }

            var payload = line[5..].Trim();
            if (payload.Length == 0 || payload == "[DONE]")
            {
                continue;
            }

            JsonElement element;
            try
            {
                using var document = JsonDocument.Parse(payload);
                element = document.RootElement.Clone();
            }
            catch (JsonException)
            {
                // A malformed event must not kill the stream.
                continue;
            }

            await onEvent(element).ConfigureAwait(false);
        }
    }

    /// <summary>Reads an SSE endpoint into the typed event callback.</summary>
    public static Task ReadJobsAsync(Stream stream, Func<JobDto, Task> onJob,
        CancellationToken cancellationToken) =>
        ReadAsync(stream, async element =>
        {
            if (element.ValueKind != JsonValueKind.Object)
            {
                return;
            }

            if (element.TryGetProperty("type", out var type) && type.ValueKind == JsonValueKind.String)
            {
                return; // "hello" marker: no job attached.
            }

            var job = JsonSerializer.Deserialize<JobDto>(element.GetRawText(), Json.Options);
            if (job is not null && !string.IsNullOrEmpty(job.Id))
            {
                await onJob(job).ConfigureAwait(false);
            }
        }, cancellationToken);
}

public static class Multipart
{
    public static HttpContent Build(
        IReadOnlyList<(string Field, string FilePath)> files,
        IReadOnlyDictionary<string, string>? formFields,
        IProgress<double>? progress = null,
        Action<IDisposable>? track = null)
    {
        var content = new MultipartFormDataContent();
        var total = files.Sum(f => new FileInfo(f.FilePath).Length);
        var uploaded = 0d;

        foreach (var (field, path) in files)
        {
            var info = new FileInfo(path);
            var stream = File.OpenRead(path);
            track?.Invoke(stream);
            var progressForFile = new Progress<double>(fraction =>
            {
                progress?.Report(Math.Min(1d, (uploaded + info.Length * fraction) / Math.Max(1, total)));
            });
            var fileContent = new ProgressStreamContent(stream, info.Length, progressForFile)
            {
                Headers = { ContentType = new MediaTypeHeaderValue(GuessContentType(info.Name)) },
            };
            content.Add(fileContent, field, info.Name);
            uploaded += info.Length;
        }

        if (formFields is not null)
        {
            foreach (var (key, value) in formFields)
            {
                content.Add(new StringContent(value ?? string.Empty, Encoding.UTF8), key);
            }
        }

        return content;
    }

    public static string GuessContentType(string name) => Path.GetExtension(name).ToLowerInvariant() switch
    {
        ".pdf" => "application/pdf",
        ".png" => "image/png",
        ".jpg" or ".jpeg" => "image/jpeg",
        ".webp" => "image/webp",
        ".json" => "application/json",
        _ => "application/octet-stream",
    };
}
