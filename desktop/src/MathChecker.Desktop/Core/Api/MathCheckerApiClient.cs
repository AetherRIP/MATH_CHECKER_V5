using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Core.Api;

/// <summary>
/// One typed method per backend endpoint the desktop client uses. Paths and
/// payloads are taken from the FastAPI handlers; nothing is invented here.
/// The client never stores credentials: the backend owns the keys, and a
/// public-mode session cookie lives only in the in-memory CookieContainer.
/// </summary>
public sealed partial class MathCheckerApiClient : IDisposable
{
    private readonly HttpClient _http;
    private readonly CookieContainer _cookies = new();
    //: Backstop for a rendered page: an A4 sheet at 300 dpi is ~4 MB of PNG.
    private const int MaxImageBytes = 24 * 1024 * 1024;

    private string _baseUrl = "http://127.0.0.1:8642";

    public MathCheckerApiClient(HttpMessageHandler? handler = null)
    {
        var cookieHandler = new HttpClientHandler
        {
            CookieContainer = _cookies,
            UseCookies = true,
            AllowAutoRedirect = false,
        };

        _http = handler is null
            ? new HttpClient(cookieHandler)
            : new HttpClient(handler, disposeHandler: false);

        _http.Timeout = TimeSpan.FromMinutes(10);
        _http.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
    }

    public string BaseUrl => _baseUrl;

    /// <summary>Set once the backend is known to be answering.</summary>
    public void SetBaseUrl(string baseUrl)
    {
        _baseUrl = baseUrl.TrimEnd('/');
    }

    /// <summary>CSRF token handed out by /auth/status; required for writes in
    /// public mode. Local mode does not use it.</summary>
    public string? CsrfToken { get; set; }

    public string? SessionUser { get; private set; }
    public string? SessionRole { get; private set; }

    public void Dispose() => _http.Dispose();

    // ---------------------------------------------------------------- plumbing

    private Uri Url(string path)
    {
        var trimmed = path.TrimStart('/');
        return new Uri($"{_baseUrl}/api/{trimmed}");
    }

    private static string Query(params (string Key, object? Value)[] pairs)
    {
        var parts = new List<string>();
        foreach (var (key, value) in pairs)
        {
            if (value is null)
            {
                continue;
            }

            var text = value switch
            {
                bool b => b ? "true" : "false",
                IFormattable f => f.ToString(null, System.Globalization.CultureInfo.InvariantCulture),
                _ => value.ToString(),
            };
            if (string.IsNullOrEmpty(text))
            {
                continue;
            }

            parts.Add($"{Uri.EscapeDataString(key)}={Uri.EscapeDataString(text)}");
        }

        return parts.Count == 0 ? string.Empty : "?" + string.Join("&", parts);
    }

    private HttpRequestMessage Request(HttpMethod method, string path)
    {
        var request = new HttpRequestMessage(method, Url(path));
        if (!string.IsNullOrEmpty(CsrfToken))
        {
            request.Headers.TryAddWithoutValidation("x-mc5-csrf", CsrfToken);
        }

        return request;
    }

    private static async Task<T> ReadAsync<T>(HttpResponseMessage response, string path,
        CancellationToken cancellationToken)
    {
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            throw ToException(response.StatusCode, body, path);
        }

        if (typeof(T) == typeof(string))
        {
            return (T)(object)body;
        }

        try
        {
            var value = JsonSerializer.Deserialize<T>(body, Json.Options);
            return value ?? throw new ApiException((int)response.StatusCode, "empty_response",
                "The backend returned an empty response.", path);
        }
        catch (JsonException ex)
        {
            throw new ApiException((int)response.StatusCode, "bad_json",
                $"The backend response could not be read: {ex.Message}", path, ex);
        }
    }

    private static ApiException ToException(HttpStatusCode status, string body, string path)
    {
        string? code = null;
        var message = body;
        try
        {
            using var document = JsonDocument.Parse(body);
            if (document.RootElement.ValueKind == JsonValueKind.Object)
            {
                if (document.RootElement.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String)
                {
                    message = m.GetString() ?? message;
                }

                if (document.RootElement.TryGetProperty("code", out var c))
                {
                    code = c.ValueKind == JsonValueKind.String ? c.GetString() : c.ToString();
                }

                if (document.RootElement.TryGetProperty("detail", out var detail))
                {
                    message = detail.ValueKind == JsonValueKind.String ? detail.GetString() ?? message : detail.ToString();
                }
            }
        }
        catch (JsonException)
        {
            // Non-JSON error body: use it as the message.
        }

        var statusCode = (int)status;
        if (string.IsNullOrWhiteSpace(message))
        {
            message = status switch
            {
                HttpStatusCode.Unauthorized => "Sign in is required.",
                HttpStatusCode.Forbidden => "This action requires a higher role.",
                HttpStatusCode.NotFound => "Not found.",
                _ => $"The backend returned HTTP {statusCode}.",
            };
        }

        return new ApiException(statusCode, code, message, path);
    }

    private async Task<T> GetAsync<T>(string path, CancellationToken cancellationToken)
    {
        using var request = Request(HttpMethod.Get, path);
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            cancellationToken).ConfigureAwait(false);
        return await ReadAsync<T>(response, path, cancellationToken).ConfigureAwait(false);
    }

    private async Task<JsonElement> GetJsonAsync(string path, CancellationToken cancellationToken)
    {
        using var request = Request(HttpMethod.Get, path);
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            throw ToException(response.StatusCode, body, path);
        }

        try
        {
            using var document = JsonDocument.Parse(body);
            return document.RootElement.Clone();
        }
        catch (JsonException ex)
        {
            throw new ApiException((int)response.StatusCode, "bad_json",
                $"The backend response could not be read: {ex.Message}", path, ex);
        }
    }

    private async Task<T> SendJsonAsync<T>(HttpMethod method, string path, object? payload,
        CancellationToken cancellationToken)
    {
        using var request = Request(method, path);
        if (payload is not null)
        {
            var json = JsonSerializer.Serialize(payload, Json.Options);
            request.Content = new StringContent(json, Encoding.UTF8, "application/json");
        }

        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            cancellationToken).ConfigureAwait(false);
        return await ReadAsync<T>(response, path, cancellationToken).ConfigureAwait(false);
    }

    private async Task<JsonElement> SendJsonRawAsync(HttpMethod method, string path, object? payload,
        CancellationToken cancellationToken)
    {
        using var request = Request(method, path);
        if (payload is not null)
        {
            var json = JsonSerializer.Serialize(payload, Json.Options);
            request.Content = new StringContent(json, Encoding.UTF8, "application/json");
        }

        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            throw ToException(response.StatusCode, body, path);
        }

        if (string.IsNullOrWhiteSpace(body))
        {
            using var empty = JsonDocument.Parse("{}");
            return empty.RootElement.Clone();
        }

        using var document = JsonDocument.Parse(body);
        return document.RootElement.Clone();
    }

    private async Task<DownloadFile> DownloadAsync(string path, string fallbackName,
        CancellationToken cancellationToken)
    {
        using var request = Request(HttpMethod.Get, path);
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            var error = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            throw ToException(response.StatusCode, error, path);
        }

        var bytes = await response.Content.ReadAsByteArrayAsync(cancellationToken).ConfigureAwait(false);
        var name = response.Content.Headers.ContentDisposition?.FileNameStar
                   ?? response.Content.Headers.ContentDisposition?.FileName?.Trim('"')
                   ?? fallbackName;
        var contentType = response.Content.Headers.ContentType?.MediaType;
        return new DownloadFile(SanitizeFileName(name), bytes, contentType);
    }

    /// <summary>
    /// Fetches a binary body (a rendered page). A non-2xx response is turned
    /// into an <see cref="ApiException"/> so the caller sees the backend's code
    /// (<c>original_not_stored</c>, <c>page_out_of_range</c>, …) rather than a
    /// generic HTTP failure.
    /// </summary>
    private async Task<byte[]> GetBytesAsync(string path, string expectedMediaType,
        CancellationToken cancellationToken)
    {
        using var request = Request(HttpMethod.Get, path);
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            throw ToException(response.StatusCode, body, path);
        }

        var media = response.Content.Headers.ContentType?.MediaType;
        var family = expectedMediaType.Split('/')[0] + "/";
        if (!string.IsNullOrEmpty(media)
            && !media.StartsWith(family, StringComparison.OrdinalIgnoreCase))
        {
            throw new ApiException((int)response.StatusCode, "unexpected_content_type",
                $"The backend returned {media} where {expectedMediaType} was expected.", path);
        }

        var bytes = await response.Content.ReadAsByteArrayAsync(cancellationToken).ConfigureAwait(false);
        if (bytes.Length > MaxImageBytes)
        {
            throw new ApiException((int)response.StatusCode, "image_too_large",
                "That page image is larger than the desktop client accepts.", path);
        }

        return bytes;
    }

    public static string SanitizeFileName(string name)
    {
        var cleaned = new string(name.Select(c => Path.GetInvalidFileNameChars().Contains(c) ? '_' : c).ToArray());
        return string.IsNullOrWhiteSpace(cleaned) ? "download" : cleaned.Trim();
    }

    // ------------------------------------------------------------------ health

    public Task<HealthDto> HealthAsync(CancellationToken cancellationToken = default) =>
        GetAsync<HealthDto>("health", cancellationToken);

    public Task<AuthStatusDto> AuthStatusAsync(CancellationToken cancellationToken = default) =>
        GetAsync<AuthStatusDto>("auth/status", cancellationToken);

    public async Task<AuthStatusDto> LoginAsync(string username, string password, bool remember,
        CancellationToken cancellationToken = default)
    {
        var payload = new { username, password, remember_me = remember };
        var status = await SendJsonAsync<AuthStatusDto>(HttpMethod.Post, "auth/login", payload, cancellationToken)
            .ConfigureAwait(false);
        await RefreshSessionAsync(cancellationToken).ConfigureAwait(false);
        return status;
    }

    public async Task<AuthStatusDto> RefreshSessionAsync(CancellationToken cancellationToken = default)
    {
        var status = await AuthStatusAsync(cancellationToken).ConfigureAwait(false);
        CsrfToken = status.CsrfToken;
        SessionUser = status.User;
        SessionRole = status.Role;
        return status;
    }

    public Task<JsonElement> LogoutAsync(CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "auth/logout", new { }, cancellationToken);

    // --------------------------------------------------------------- dashboard

    public Task<DashboardDto> DashboardAsync(CancellationToken cancellationToken = default) =>
        GetAsync<DashboardDto>("dashboard", cancellationToken);

    public Task<AttentionFeedDto> AttentionAsync(CancellationToken cancellationToken = default) =>
        GetAsync<AttentionFeedDto>("attention", cancellationToken);

    public Task<JsonElement> SearchAsync(string query, CancellationToken cancellationToken = default) =>
        GetJsonAsync("search" + Query(("q", query)), cancellationToken);

    public Task<JsonElement> ActivityAsync(int hours = 24, CancellationToken cancellationToken = default) =>
        GetJsonAsync("analytics/activity" + Query(("hours", hours)), cancellationToken);

    public Task<JsonElement> ClassTrendAsync(string courseId, CancellationToken cancellationToken = default) =>
        GetJsonAsync($"analytics/class/{Uri.EscapeDataString(courseId)}/trend", cancellationToken);

    // -------------------------------------------------------------- correction


    /// <summary>
    /// POST /corrections/unified — the real routing decision is made by the
    /// backend from the student PDF's page count.
    /// </summary>
    public async Task<JsonElement> SubmitUnifiedCorrectionAsync(
        string submissionPath, string markSchemePath, string studentName,
        IProgress<double>? progress, CancellationToken cancellationToken)
    {
        var files = new List<(string, string)>
        {
            ("submission", submissionPath),
            ("mark_scheme", markSchemePath),
        };
        var fields = new Dictionary<string, string>
        {
            ["student_name"] = studentName,
        };

        var streams = new List<IDisposable>();
        try
        {
            using var content = Multipart.Build(files, fields, progress, streams.Add);
            using var request = Request(HttpMethod.Post, "corrections/unified");
            request.Content = content;
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
                cancellationToken).ConfigureAwait(false);
            var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                throw ToException(response.StatusCode, body, "corrections/unified");
            }

            using var document = JsonDocument.Parse(body);
            return document.RootElement.Clone();
        }
        finally
        {
            foreach (var stream in streams)
            {
                stream.Dispose();
            }
        }
    }

    public async Task<JsonElement> SubmitManualCorrectionAsync(
        string submissionPath, string markSchemePath, string? questionPaperPath, string studentName,
        IProgress<double>? progress, CancellationToken cancellationToken)
    {
        var files = new List<(string, string)>
        {
            ("submission", submissionPath),
            ("mark_scheme", markSchemePath),
        };
        if (!string.IsNullOrWhiteSpace(questionPaperPath))
        {
            files.Add(("question_paper", questionPaperPath!));
        }

        var streams = new List<IDisposable>();
        try
        {
            using var content = Multipart.Build(files,
                new Dictionary<string, string> { ["student_name"] = studentName }, progress, streams.Add);
            using var request = Request(HttpMethod.Post, "corrections/manual");
            request.Content = content;
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
                cancellationToken).ConfigureAwait(false);
            var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                throw ToException(response.StatusCode, body, "corrections/manual");
            }

            using var document = JsonDocument.Parse(body);
            return document.RootElement.Clone();
        }
        finally
        {
            foreach (var stream in streams)
            {
                stream.Dispose();
            }
        }
    }

    // -------------------------------------------------------------------- jobs

    public Task<JobListDto> JobsAsync(string? status = null, int limit = 200,
        CancellationToken cancellationToken = default) =>
        GetAsync<JobListDto>("jobs" + Query(("status", status), ("limit", limit)), cancellationToken);

    public Task<JobDto> JobAsync(string jobId, CancellationToken cancellationToken = default) =>
        GetAsync<JobDto>($"jobs/{Uri.EscapeDataString(jobId)}", cancellationToken);

    public Task<JsonElement> CancelJobAsync(string jobId, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"jobs/{Uri.EscapeDataString(jobId)}/cancel", new { }, cancellationToken);

    public Task<JobDto> RetryJobAsync(string jobId, CancellationToken cancellationToken = default) =>
        SendJsonAsync<JobDto>(HttpMethod.Post, $"jobs/{Uri.EscapeDataString(jobId)}/retry", new { }, cancellationToken);

    public Task<JsonElement> ReprocessJobAsync(string jobId, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"jobs/{Uri.EscapeDataString(jobId)}/reprocess", new { }, cancellationToken);

    /// <summary>Live stage updates. The caller supplies a cancellation token and
    /// should fall back to polling if the stream ends.</summary>
    public async Task StreamJobsAsync(Func<JobDto, Task> onJob, CancellationToken cancellationToken)
    {
        using var request = Request(HttpMethod.Get, "jobs/stream");
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            throw ToException(response.StatusCode, body, "jobs/stream");
        }

        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
        await SseParser.ReadJobsAsync(stream, onJob, cancellationToken).ConfigureAwait(false);
    }

    // ----------------------------------------------------------------- results

    public Task<ResultListDto> ResultsAsync(string? q = null, string? status = null,
        string? courseworkId = null, string? courseId = null, int limit = 200, int offset = 0,
        CancellationToken cancellationToken = default) =>
        GetAsync<ResultListDto>("results" + Query(
            ("q", q), ("status", status), ("coursework_id", courseworkId), ("course_id", courseId),
            ("limit", limit), ("offset", offset)), cancellationToken);

    public Task<ResultDetailDto> ResultAsync(string resultId, CancellationToken cancellationToken = default) =>
        GetAsync<ResultDetailDto>($"results/{Uri.EscapeDataString(resultId)}", cancellationToken);

    public Task<DownloadFile> ExportResultAsync(string resultId, string format, string detail = "standard",
        CancellationToken cancellationToken = default) =>
        DownloadAsync($"results/{Uri.EscapeDataString(resultId)}/export" +
                      Query(("format", format), ("detail", detail)),
            $"report.{format}", cancellationToken);

    public Task<VersionListDto> ResultVersionsAsync(string resultId, CancellationToken cancellationToken = default) =>
        GetAsync<VersionListDto>($"results/{Uri.EscapeDataString(resultId)}/versions", cancellationToken);

    public Task<JsonElement> RestoreVersionAsync(string resultId, int version,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"results/{Uri.EscapeDataString(resultId)}/versions/{version}/restore", new { }, cancellationToken);

    public Task<RemarkStartDto> RemarkAsync(string resultId, CancellationToken cancellationToken = default) =>
        SendJsonAsync<RemarkStartDto>(HttpMethod.Post,
            $"results/{Uri.EscapeDataString(resultId)}/remark", new { }, cancellationToken);

    public Task<RemarkStatusDto> RemarkStatusAsync(string resultId, CancellationToken cancellationToken = default) =>
        GetAsync<RemarkStatusDto>($"results/{Uri.EscapeDataString(resultId)}/remark", cancellationToken);

    public Task<JsonElement> RemarkDecisionAsync(string resultId, string decision, int? version,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"results/{Uri.EscapeDataString(resultId)}/remark/decision",
            new { decision, version }, cancellationToken);

    public Task<JsonElement> RetryResultStepAsync(string resultId, string action,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"results/{Uri.EscapeDataString(resultId)}/retry",
            new { action }, cancellationToken);

    /// <summary>
    /// A whole-paper teacher decision. The backend casts <c>earned</c> to an
    /// integer, so the signature is <c>int?</c>: 12.5 is a 400, not a mark.
    /// Omitting <c>earned</c> records an approval of the engine's marks.
    /// </summary>
    public Task<JsonElement> OverrideResultAsync(string resultId, int? earned, string? note,
        CancellationToken cancellationToken = default)
    {
        // An approval sends no `earned` key at all; the backend then records an
        // approval of the engine's marks instead of a whole-paper override.
        object payload = earned is null ? new { note } : new { earned, note };
        return SendJsonRawAsync(HttpMethod.Post, $"results/{Uri.EscapeDataString(resultId)}/override",
            payload, cancellationToken);
    }

    /// <summary>
    /// Teacher override of one question. The backend rejects anything that is
    /// not a whole number (<c>int(payload["awarded"])</c>), so this is an int.
    /// </summary>
    public Task<JsonElement> SaveQuestionMarkAsync(string resultId, string questionNumber, int awarded,
        string? note = null, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"results/{Uri.EscapeDataString(resultId)}/questions/{Uri.EscapeDataString(questionNumber)}/mark",
            new { awarded, note }, cancellationToken);

    public Task<ResultReviewHistoryDto> ResultReviewHistoryAsync(string resultId,
        CancellationToken cancellationToken = default) =>
        GetAsync<ResultReviewHistoryDto>($"results/{Uri.EscapeDataString(resultId)}/reviews",
            cancellationToken);

    // ------------------------------------------------------------------ review

    public Task<ReviewListDto> ReviewQueueAsync(int limit = 100, CancellationToken cancellationToken = default) =>
        GetAsync<ReviewListDto>("review" + Query(("limit", limit)), cancellationToken);

    // --------------------------------------------------- classes / assignments

    public Task<ClassListDto> ClassesAsync(CancellationToken cancellationToken = default) =>
        GetAsync<ClassListDto>("classes", cancellationToken);

    public Task<AssignmentListDto> AssignmentsAsync(string? courseId = null, string? managed = null,
        string? q = null, CancellationToken cancellationToken = default) =>
        GetAsync<AssignmentListDto>("assignments" + Query(
            ("course_id", courseId), ("managed", managed), ("q", q)), cancellationToken);

    public Task<JsonElement> AssignmentAsync(string courseworkId, CancellationToken cancellationToken = default) =>
        GetJsonAsync($"assignments/{Uri.EscapeDataString(courseworkId)}", cancellationToken);

    /// <summary>The Assignment Details payload: metadata, the expected roster
    /// with each student's status, and the summary counts computed from those
    /// same rows. Throws <see cref="ApiException"/> (404) for an assignment the
    /// local database does not hold.</summary>
    public Task<AssignmentStudentsDto> AssignmentStudentsAsync(string courseworkId,
        CancellationToken cancellationToken = default) =>
        GetAsync<AssignmentStudentsDto>(
            $"assignments/{Uri.EscapeDataString(courseworkId)}/students", cancellationToken);

    public Task<JsonElement> CreateLocalAssignmentAsync(string title, double? maxPoints, string? dueDate,
        string? description, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "assignments/local",
            new { title, max_points = maxPoints, due_date = dueDate, description }, cancellationToken);

    /// <summary>Create a coursework in Google Classroom. The backend reports the
    /// id Google confirmed, so the caller never invents one.</summary>
    public Task<JsonElement> CreateClassroomAssignmentAsync(string courseId, string title,
        string? description, double? maxPoints, string? dueDate,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"classroom/courses/{Uri.EscapeDataString(courseId)}/assignments/create",
            new { title, description, max_points = maxPoints, due_date = dueDate },
            cancellationToken);

    public Task<JsonElement> MarkSchemeAsync(string courseworkId, CancellationToken cancellationToken = default) =>
        GetJsonAsync($"assignments/{Uri.EscapeDataString(courseworkId)}/mark-scheme", cancellationToken);

    public Task<JsonElement> MarkSchemePagesAsync(string courseworkId, CancellationToken cancellationToken = default) =>
        GetJsonAsync($"assignments/{Uri.EscapeDataString(courseworkId)}/mark-scheme/pages", cancellationToken);

    public Task<DownloadFile> MarkSchemePageImageAsync(string courseworkId, int page,
        CancellationToken cancellationToken = default) =>
        DownloadAsync($"assignments/{Uri.EscapeDataString(courseworkId)}/mark-scheme/page/{page}.png",
            $"mark-scheme-p{page}.png", cancellationToken);

    public Task<JsonElement> OverrideMarkSchemeAsync(string courseworkId, string text,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"assignments/{Uri.EscapeDataString(courseworkId)}/mark-scheme/override",
            new { text }, cancellationToken);

    public Task<JsonElement> RecheckMarkSchemeAsync(string courseworkId, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"assignments/{Uri.EscapeDataString(courseworkId)}/mark-scheme/recheck", new { }, cancellationToken);

    // ---------------------------------------------------------------- students

    public Task<StudentListDto> StudentsAsync(CancellationToken cancellationToken = default) =>
        GetAsync<StudentListDto>("students", cancellationToken);

    /// <summary>The full, typed profile: identity, Classroom submissions,
    /// marks and correction history. Throws <see cref="ApiException"/> with the
    /// backend's message for an unknown student.</summary>
    public Task<StudentProfileDto> StudentAsync(string studentId, CancellationToken cancellationToken = default) =>
        GetAsync<StudentProfileDto>($"students/{Uri.EscapeDataString(studentId)}", cancellationToken);

    public Task<StudentClassesDto> StudentClassesAsync(CancellationToken cancellationToken = default) =>
        GetAsync<StudentClassesDto>("students/classes", cancellationToken);

    /// <summary>Saves the teacher's local nickname/class metadata. The
    /// response is the refreshed profile, so callers do not need a second GET.</summary>
    public Task<StudentProfileDto> SaveStudentDetailsAsync(string studentId, string? nickname,
        string? className, CancellationToken cancellationToken = default) =>
        SendJsonAsync<StudentProfileDto>(HttpMethod.Put,
            $"students/{Uri.EscapeDataString(studentId)}/details",
            new { nickname, class_name = className }, cancellationToken);

    public Task<DownloadFile> StudentReportAsync(string studentId, CancellationToken cancellationToken = default) =>
        DownloadAsync($"students/{Uri.EscapeDataString(studentId)}/report", "student-report.pdf", cancellationToken);

    public Task<DownloadFile> StudentAssignmentReportAsync(string studentId, string courseworkId,
        CancellationToken cancellationToken = default) =>
        DownloadAsync($"students/{Uri.EscapeDataString(studentId)}/assignments/{Uri.EscapeDataString(courseworkId)}/report",
            "student-assignment-report.pdf", cancellationToken);

    // --------------------------------------------------------------- classroom

    public Task<ClassroomStatusDto> ClassroomStatusAsync(CancellationToken cancellationToken = default) =>
        GetAsync<ClassroomStatusDto>("classroom/status", cancellationToken);

    public Task<JsonElement> ConnectClassroomAsync(CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "classroom/connect", new { }, cancellationToken);

    public Task<JsonElement> DisconnectClassroomAsync(CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "classroom/disconnect", new { }, cancellationToken);

    public Task<ClassroomCourseListDto> ClassroomCoursesAsync(CancellationToken cancellationToken = default) =>
        GetAsync<ClassroomCourseListDto>("classroom/courses", cancellationToken);

    public Task<JsonElement> ClassroomCourseAssignmentsAsync(string courseId,
        CancellationToken cancellationToken = default) =>
        GetJsonAsync($"classroom/courses/{Uri.EscapeDataString(courseId)}/assignments", cancellationToken);

    public Task<JsonElement> ClassroomSubmissionsAsync(string courseId, string courseworkId,
        CancellationToken cancellationToken = default) =>
        GetJsonAsync($"classroom/courses/{Uri.EscapeDataString(courseId)}/assignments/{Uri.EscapeDataString(courseworkId)}/submissions",
            cancellationToken);

    public Task<JsonElement> CorrectSubmissionAsync(string courseId, string courseworkId, JsonElement payload,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"classroom/courses/{Uri.EscapeDataString(courseId)}/assignments/{Uri.EscapeDataString(courseworkId)}/correct",
            payload, cancellationToken);

    /// <summary>Side-effect-free description of a whole-assignment correction:
    /// the counts, the blockers and the papers that would be queued. Typed, so
    /// the page can show the backend's numbers without re-deriving them.</summary>
    public Task<CorrectAllPreviewDto> CorrectAllPreviewAsync(string courseworkId,
        CancellationToken cancellationToken = default) =>
        GetAsync<CorrectAllPreviewDto>(
            $"assignments/{Uri.EscapeDataString(courseworkId)}/correct-all/preview", cancellationToken);

    /// <summary>Start a correction. <paramref name="submissionIds"/> names the
    /// exact papers to queue; null means every eligible paper (the backend's
    /// whole-assignment behaviour), and an empty list is refused by the backend
    /// rather than silently correcting the class.</summary>
    public Task<JsonElement> CorrectAllAsync(string courseId, string courseworkId, bool refresh,
        IReadOnlyCollection<string>? submissionIds = null,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"classroom/courses/{Uri.EscapeDataString(courseId)}/assignments/{Uri.EscapeDataString(courseworkId)}/correct-all",
            submissionIds is null
                ? new { refresh }
                : (object)new { refresh, submission_ids = submissionIds.ToArray() },
            cancellationToken);

    /// <summary>Typed variant of <see cref="CorrectAllAsync"/>: the response's
    /// own numbers, so the page reports what really ran.</summary>
    public async Task<CorrectAllResultDto> CorrectAllPapersAsync(string courseId,
        string courseworkId, bool refresh, IReadOnlyCollection<string>? submissionIds,
        CancellationToken cancellationToken = default)
    {
        var raw = await CorrectAllAsync(courseId, courseworkId, refresh, submissionIds,
            cancellationToken).ConfigureAwait(false);
        try
        {
            return raw.Deserialize<CorrectAllResultDto>(Json.Options)
                   ?? new CorrectAllResultDto();
        }
        catch (JsonException)
        {
            return new CorrectAllResultDto();
        }
    }

    /// <summary>Allow-list toggle. The backend reads it from the query string.</summary>
    public Task<JsonElement> SetAllowlistAsync(string courseId, string courseworkId, bool allowed,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"classroom/courses/{Uri.EscapeDataString(courseId)}/assignments/{Uri.EscapeDataString(courseworkId)}/allow"
            + Query(("allowed", allowed)), null, cancellationToken);

    /// <summary>Upload the mark scheme PDF for one Classroom assignment.</summary>
    public async Task<JsonElement> UploadMarkSchemeAsync(string courseId, string courseworkId, string path,
        CancellationToken cancellationToken = default)
    {
        using var stream = File.OpenRead(path);
        using var content = new MultipartFormDataContent
        {
            { new StreamContent(stream), "file", Path.GetFileName(path) },
        };
        using var request = Request(HttpMethod.Post,
            $"classroom/courses/{Uri.EscapeDataString(courseId)}/assignments/{Uri.EscapeDataString(courseworkId)}/mark-scheme");
        request.Content = content;
        using var response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            throw ToException(response.StatusCode, body, "classroom mark-scheme upload");
        }

        using var document = JsonDocument.Parse(body);
        return document.RootElement.Clone();
    }

    /// <summary>Starts a sync in the background; progress is polled through
    /// <see cref="ClassroomSyncProgressAsync"/>. With both ids it is scoped to
    /// ONE assignment (used to retry just the parts a run failed on), with
    /// neither it is the full class sync.</summary>
    public Task<JsonElement> StartClassroomSyncAsync(string? courseId = null, string? courseworkId = null,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            "classroom/sync/start" + Query(("course_id", courseId), ("coursework_id", courseworkId)),
            new { }, cancellationToken);

    public Task<JsonElement> ClassroomSyncProgressAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("classroom/sync/progress", cancellationToken);

    public Task<JsonElement> ClassroomTrackingAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("classroom/tracking", cancellationToken);

    public Task<DownloadFile> ClassroomTrackingExportAsync(CancellationToken cancellationToken = default) =>
        DownloadAsync("classroom/tracking/export", "classroom-tracking.csv", cancellationToken);

    public Task<JsonElement> PushGradesAsync(string courseId, string courseworkId,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "classroom/grades/push"
            + Query(("course_id", courseId), ("coursework_id", courseworkId)), null, cancellationToken);

    // ----------------------------------------------------------------- reports

    public Task<JsonElement> ReportOptionsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("reports/options", cancellationToken);

    public Task<JsonElement> AssignmentReportAsync(string courseworkId, CancellationToken cancellationToken = default) =>
        GetJsonAsync($"reports/assignment/{Uri.EscapeDataString(courseworkId)}", cancellationToken);

    public Task<JsonElement> ClassReportAsync(string courseId, CancellationToken cancellationToken = default) =>
        GetJsonAsync($"reports/class/{Uri.EscapeDataString(courseId)}", cancellationToken);

    /// <summary>One report over several chosen assignments. The combined figure
    /// is the backend's mark fraction (sum earned / sum available), never an
    /// average of per-assignment percentages.</summary>
    public Task<JsonElement> MultiAssignmentReportAsync(IEnumerable<string> courseworkIds,
        string? classLabel = null, string? courseId = null, string? student = null,
        CancellationToken cancellationToken = default) =>
        GetJsonAsync("reports/assignments" + Query(
            ("ids", string.Join(",", courseworkIds)),
            ("class_label", classLabel), ("course_id", courseId), ("student", student)),
            cancellationToken);

    public Task<DownloadFile> ExportMultiAssignmentReportAsync(IEnumerable<string> courseworkIds,
        string format = "pdf", string? classLabel = null, string? courseId = null,
        string? student = null, CancellationToken cancellationToken = default) =>
        DownloadAsync("reports/assignments/export" + Query(
                ("ids", string.Join(",", courseworkIds)),
                ("class_label", classLabel), ("course_id", courseId),
                ("student", student), ("format", format)),
            $"assignments-report.{format}", cancellationToken);

    public Task<JsonElement> ClassReportFilteredAsync(string? classLabel, string? courseId,
        CancellationToken cancellationToken = default) =>
        GetJsonAsync("reports/class-report" + Query(("class", classLabel), ("course_id", courseId)), cancellationToken);

    public Task<JsonElement> BuildReportAsync(string reportType, string? classLabel, string? courseworkId,
        string? format, CancellationToken cancellationToken = default) =>
        GetJsonAsync("reports/build" + Query(
            ("type", reportType), ("class", classLabel), ("coursework_id", courseworkId), ("format", format)),
            cancellationToken);

    public Task<DownloadFile> ExportAssignmentReportAsync(string courseworkId, string format = "pdf",
        CancellationToken cancellationToken = default) =>
        DownloadAsync($"reports/assignment/{Uri.EscapeDataString(courseworkId)}/export" + Query(("format", format)),
            $"assignment-report.{format}", cancellationToken);

    public Task<DownloadFile> ExportClassReportAsync(string courseId, string format = "pdf",
        CancellationToken cancellationToken = default) =>
        DownloadAsync($"reports/class/{Uri.EscapeDataString(courseId)}/export" + Query(("format", format)),
            $"class-report.{format}", cancellationToken);

    public Task<DownloadFile> ExportCustomReportAsync(string reportType, string? classLabel,
        string? courseworkId, string format = "pdf", CancellationToken cancellationToken = default) =>
        DownloadAsync("reports/build/export" + Query(
                ("type", reportType), ("class", classLabel), ("coursework_id", courseworkId), ("format", format)),
            $"report.{format}", cancellationToken);

    // ------------------------------------------------------------ large PDF


    // ------------------------------------------------------------- automation

    public Task<AutomationStatusDto> AutomationStatusAsync(CancellationToken cancellationToken = default) =>
        GetAsync<AutomationStatusDto>("automation/status", cancellationToken);

    public Task<JsonElement> SetAutomationWorkerAsync(bool enabled, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "automation/worker", new { enabled }, cancellationToken);

    public Task<JsonElement> SetAutomationSettingsAsync(object payload,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "automation/settings", payload, cancellationToken);

    public Task<AutomationJobsDto> AutomationJobsAsync(string? status = null, int limit = 100,
        CancellationToken cancellationToken = default) =>
        GetAsync<AutomationJobsDto>("automation/jobs" + Query(("status", status), ("limit", limit)),
            cancellationToken);

    public Task<JsonElement> AutomationAssignmentsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("automation/assignments", cancellationToken);

    public Task<JsonElement> SetAutomationAssignmentAsync(string courseworkId, bool enabled,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"automation/assignments/{Uri.EscapeDataString(courseworkId)}",
            new { enabled }, cancellationToken);

    public Task<JsonElement> RunAutomationNowAsync(CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "automation/run-now", new { }, cancellationToken);

    public Task<JsonElement> ApproveAutomationJobAsync(string jobId, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"automation/jobs/{Uri.EscapeDataString(jobId)}/approve", new { },
            cancellationToken);

    public Task<JsonElement> RetryAutomationJobAsync(string jobId, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"automation/jobs/{Uri.EscapeDataString(jobId)}/retry", new { },
            cancellationToken);

    public Task<JsonElement> ReconcileAutomationAsync(CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "automation/jobs/reconcile", new { }, cancellationToken);

    public Task<JsonElement> AutomationLogAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("automation/log", cancellationToken);

    public Task<JsonElement> ClearAutomationLogAsync(CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "automation/log/clear", new { }, cancellationToken);

    public Task<JsonElement> GradeBoundariesAsync(string courseworkId, CancellationToken cancellationToken = default) =>
        GetJsonAsync($"automation/grade-boundaries/{Uri.EscapeDataString(courseworkId)}", cancellationToken);

    public Task<JsonElement> SaveGradeBoundariesAsync(string courseworkId, JsonElement payload,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"automation/grade-boundaries/{Uri.EscapeDataString(courseworkId)}",
            payload, cancellationToken);

    // -------------------------------------------------------------------- keys

    public Task<KeysDto> KeysAsync(CancellationToken cancellationToken = default) =>
        GetAsync<KeysDto>("keys", cancellationToken);

    public Task<KeyTestDto> TestKeyAsync(string fingerprint, CancellationToken cancellationToken = default) =>
        SendJsonAsync<KeyTestDto>(HttpMethod.Post,
            $"keys/{Uri.EscapeDataString(fingerprint)}/test", new { }, cancellationToken);

    public Task<KeysDto> DisableKeyAsync(string fingerprint, CancellationToken cancellationToken = default) =>
        SendJsonAsync<KeysDto>(HttpMethod.Post,
            $"keys/{Uri.EscapeDataString(fingerprint)}/disable", new { }, cancellationToken);

    public Task<KeysDto> EnableKeyAsync(string fingerprint, CancellationToken cancellationToken = default) =>
        SendJsonAsync<KeysDto>(HttpMethod.Post,
            $"keys/{Uri.EscapeDataString(fingerprint)}/re-enable", new { }, cancellationToken);

    public Task<JsonElement> RemoveKeyAsync(string fingerprint, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"keys/{Uri.EscapeDataString(fingerprint)}/remove", new { }, cancellationToken);

    public Task<JsonElement> AddKeysAsync(IEnumerable<string> keys, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "settings/gemini-keys", new { keys = keys.ToArray() }, cancellationToken);

    public async Task StreamKeyTestsAsync(Func<JsonElement, Task> onEvent, CancellationToken cancellationToken)
    {
        using var request = Request(HttpMethod.Get, "keys/test/stream");
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
            cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            throw ToException(response.StatusCode, body, "keys/test/stream");
        }

        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
        await SseParser.ReadAsync(stream, onEvent, cancellationToken).ConfigureAwait(false);
    }

    // ------------------------------------------------------------ system pages

    public Task<WorkerStateDto> WorkersAsync(CancellationToken cancellationToken = default) =>
        GetAsync<WorkerStateDto>("workers", cancellationToken);

    /// <summary>Sets the pool size. The field name is the backend's: it reads
    /// ``workers`` and ignores anything else, so posting ``count`` left every
    /// apply resizing the pool to the backend's default of 2.</summary>
    public Task<WorkerStateDto> ResizeWorkersAsync(int count, CancellationToken cancellationToken = default) =>
        SendJsonAsync<WorkerStateDto>(HttpMethod.Post, "workers/resize", new { workers = count }, cancellationToken);

    public Task<SettingsDto> SettingsAsync(CancellationToken cancellationToken = default) =>
        GetAsync<SettingsDto>("settings", cancellationToken);

    public Task<JsonElement> SaveSettingsAsync(object payload, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "settings", payload, cancellationToken);

    public Task<UsageDto> UsageAsync(int hours = 24, CancellationToken cancellationToken = default) =>
        GetAsync<UsageDto>("usage" + Query(("hours", hours)), cancellationToken);

    public Task<DataInventoryDto> DataInventoryAsync(CancellationToken cancellationToken = default) =>
        GetAsync<DataInventoryDto>("data/inventory", cancellationToken);

    public Task<DownloadFile> DataExportAsync(CancellationToken cancellationToken = default) =>
        DownloadAsync("data/export", "math_checker_export.json", cancellationToken);

    public async Task<JsonElement> PreviewImportAsync(string path, CancellationToken cancellationToken = default)
    {
        using var stream = File.OpenRead(path);
        using var content = new MultipartFormDataContent
        {
            { new StreamContent(stream), "file", Path.GetFileName(path) },
        };
        using var request = Request(HttpMethod.Post, "data/import/preview");
        request.Content = content;
        using var response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            throw ToException(response.StatusCode, body, "data/import/preview");
        }

        using var document = JsonDocument.Parse(body);
        return document.RootElement.Clone();
    }

    public async Task<JsonElement> ImportAsync(string path, string mode, string? confirm,
        CancellationToken cancellationToken = default)
    {
        using var stream = File.OpenRead(path);
        using var content = new MultipartFormDataContent
        {
            { new StreamContent(stream), "file", Path.GetFileName(path) },
            { new StringContent(mode), "mode" },
            { new StringContent(confirm ?? string.Empty), "confirm" },
        };
        using var request = Request(HttpMethod.Post, "data/import");
        request.Content = content;
        using var response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
        {
            throw ToException(response.StatusCode, body, "data/import");
        }

        using var document = JsonDocument.Parse(body);
        return document.RootElement.Clone();
    }

    public Task<PrivacyInfoDto> PrivacyInfoAsync(CancellationToken cancellationToken = default) =>
        GetAsync<PrivacyInfoDto>("privacy/info", cancellationToken);

    public Task<DownloadFile> DiagnosticExportAsync(CancellationToken cancellationToken = default) =>
        DownloadAsync("privacy/diagnostic-export", "math_checker_diagnostics.txt", cancellationToken);

    public Task<JsonElement> FactoryResetAsync(string confirmation, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "privacy/factory-reset", new { confirm = confirmation },
            cancellationToken);

    public Task<JsonElement> DiagnosticsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("diagnostics", cancellationToken);

    public Task<JsonElement> DiagnosticLogsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("diagnostics/logs", cancellationToken);

    public Task<JsonElement> ClearDiagnosticLogsAsync(CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "diagnostics/logs/clear", new { }, cancellationToken);

    public Task<JsonElement> DiagnosticDbAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("diagnostics/db", cancellationToken);

    public Task<JsonElement> SelfCheckAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("diagnostics/selfcheck", cancellationToken);

    public Task<JsonElement> PromptsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("dev/prompts", cancellationToken);

    public Task<JsonElement> SavePromptsAsync(string? systemPrompt, string? userPrompt,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "dev/prompts",
            new { system_prompt_override = systemPrompt, user_prompt_override = userPrompt }, cancellationToken);

    public Task<JsonElement> AuditSummaryAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("audit/summary", cancellationToken);

    public Task<JsonElement> AuditReportsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("audit/reports", cancellationToken);

    public Task<JsonElement> SchemeSummaryAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("scheme/summary", cancellationToken);

    /// <summary>Discrepancies, optionally narrowed to one review status.
    ///
    /// The endpoint with no filter answers with the WHOLE audit table, decided
    /// rows included. Only <c>review_status=AWAITING_REVIEW</c> is the open set a
    /// reviewer still has to decide — the same set `/scheme/summary` counts — so
    /// a screen that lists "open" discrepancies must ask for it, or a decided row
    /// sits in the list next to a count that says nothing is awaiting review.
    /// </summary>
    public Task<JsonElement> DiscrepanciesAsync(string? reviewStatus = null,
        CancellationToken cancellationToken = default) =>
        GetJsonAsync("scheme/discrepancies" + Query(("review_status", reviewStatus)),
            cancellationToken);

    public Task<JsonElement> ProposalsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("scheme/proposals", cancellationToken);

    public Task<JsonElement> DecideDiscrepancyAsync(string discrepancyId, string decision,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"scheme/discrepancies/{Uri.EscapeDataString(discrepancyId)}/decision", new { decision },
            cancellationToken);

    public Task<JsonElement> ProposeDiscrepancyAsync(string discrepancyId, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            $"scheme/discrepancies/{Uri.EscapeDataString(discrepancyId)}/propose", new { }, cancellationToken);

    /// <summary>Approval MUST carry <c>confirm: true</c>; the backend refuses a
    /// bulk grade change without it.</summary>
    public Task<JsonElement> DecideProposalAsync(string proposalId, string decision,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, $"scheme/proposals/{Uri.EscapeDataString(proposalId)}/{decision}",
            decision == "approve" ? new { confirm = true } : new { }, cancellationToken);

    /// <summary>One discrepancy with the mark-scheme page it was found on, the
    /// scheme's own wording and the student's stored answer. Nothing here is
    /// reconstructed: an absent field means the backend has none.</summary>
    public Task<JsonElement> DiscrepancyAsync(string discrepancyId,
        CancellationToken cancellationToken = default) =>
        GetJsonAsync($"scheme/discrepancies/{Uri.EscapeDataString(discrepancyId)}", cancellationToken);

    /// <summary>Every question this assignment's mark scheme declares, with its
    /// marks, page and whether it is currently being marked.</summary>
    public Task<JsonElement> SchemeQuestionsAsync(string courseworkId,
        CancellationToken cancellationToken = default) =>
        GetJsonAsync("scheme/questions" + Query(("coursework_id", courseworkId)), cancellationToken);

    /// <summary>Every question and sub-question of one assignment's scheme with
    /// its extracted answer, marks, extraction state and handling decision.
    /// This is what the Mark scheme page reads.</summary>
    public Task<JsonElement> SchemeDecisionsAsync(string courseworkId,
        CancellationToken cancellationToken = default) =>
        GetJsonAsync("scheme/decisions" + Query(("coursework_id", courseworkId)), cancellationToken);

    /// <summary>Read this assignment's attached mark scheme into questions, now.
    ///
    /// The structure is otherwise produced only while correcting a paper, which
    /// left a brand-new assignment with nothing to review. Answers ``read``
    /// false with a ``reason`` when it could not: an unreadable file, or Demo
    /// Mode, which never extracts a scheme.
    /// </summary>
    public Task<JsonElement> ReadMarkSchemeAsync(string courseworkId,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post,
            "scheme/read" + Query(("coursework_id", courseworkId)), new { },
            cancellationToken);

    /// <summary>Set one question's handling decision (AUTO / MANUAL / SKIP).
    /// SKIP is written through the exclusion table, so the marking worker and
    /// this screen can never disagree about what is being marked.</summary>
    public Task<JsonElement> SetSchemeDecisionAsync(string courseworkId, string questionNumber,
        string decision, string? note = null, string? reason = null,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "scheme/decision", new
        {
            coursework_id = courseworkId,
            question_number = questionNumber,
            decision,
            note,
            reason,
        }, cancellationToken);

    /// <summary>Replace the expected answer used for one question. Send
    /// <paramref name="answer"/> as null to restore the extracted answer; an
    /// empty string is kept as "there is no acceptable answer".</summary>
    public Task<JsonElement> SetSchemeAnswerAsync(string courseworkId, string questionNumber,
        string? answer, string? reason = null, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "scheme/answer", new
        {
            coursework_id = courseworkId,
            question_number = questionNumber,
            // Named explicitly so the null is serialised: the backend treats a
            // missing key as a bad request, and "clear the edit" must work.
            answer = answer,
            reason,
        }, cancellationToken);

    /// <summary>Every decision and answer change on one assignment, newest first.</summary>
    public Task<JsonElement> SchemeDecisionHistoryAsync(string courseworkId,
        string? questionNumber = null, CancellationToken cancellationToken = default) =>
        GetJsonAsync("scheme/decision-history" + Query(
            ("coursework_id", courseworkId), ("question_number", questionNumber)),
            cancellationToken);

    /// <summary>Sign off the review. Undecided questions need
    /// <paramref name="acknowledgeUnresolved"/> or the backend refuses.</summary>
    public Task<JsonElement> FinalizeSchemeReviewAsync(string courseworkId,
        bool acknowledgeUnresolved, string? note = null,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "scheme/finalize", new
        {
            coursework_id = courseworkId,
            acknowledge_unresolved = acknowledgeUnresolved,
            note,
        }, cancellationToken);

    /// <summary>Take one question (or one subquestion) out of marking for an
    /// assignment. Existing results keep their marks; later corrections skip it
    /// and its marks leave the total.
    /// </summary>
    public Task<JsonElement> IgnoreSchemeQuestionAsync(string courseworkId, string questionNumber,
        string? note = null, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "scheme/ignore",
            new { coursework_id = courseworkId, question_number = questionNumber, note }, cancellationToken);

    public Task<JsonElement> RestoreSchemeQuestionAsync(string courseworkId, string questionNumber,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Delete,
            $"scheme/ignored/{Uri.EscapeDataString(courseworkId)}/{Uri.EscapeDataString(questionNumber)}",
            null, cancellationToken);

    /// <summary>The real uploaded page of an assignment's mark scheme, rendered
    /// by the backend. Used by the review screen to show what the scheme says.
    /// </summary>
    public Task<DownloadFile> SchemePageImageAsync(string courseworkId, int page,
        CancellationToken cancellationToken = default) =>
        DownloadAsync($"assignments/{Uri.EscapeDataString(courseworkId)}/mark-scheme/page/{page}.png",
            $"mark-scheme-p{page}.png", cancellationToken);

    // ------------------------------------------------------ distributed layer

    public Task<JsonElement> DistributedCapabilitiesAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("distributed/capabilities", cancellationToken);

    public Task<JsonElement> DistributedSchoolsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("distributed/schools", cancellationToken);

    public Task<JsonElement> DistributedAccountsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("distributed/accounts", cancellationToken);

    public Task<JsonElement> DistributedTeamsAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("distributed/teams", cancellationToken);

    public Task<JsonElement> DistributedDevicesAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("distributed/devices", cancellationToken);

    public Task<JsonElement> DistributedWorkersAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("distributed/workers", cancellationToken);

    public Task<JsonElement> ReleasesOverviewAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("distributed/releases/overview", cancellationToken);

    public Task<JsonElement> ReleasesAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("distributed/releases", cancellationToken);

    public Task<JsonElement> AccessStateAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("access/state", cancellationToken);

    public Task<JsonElement> RemoteStatusAsync(CancellationToken cancellationToken = default) =>
        GetJsonAsync("remote/status", cancellationToken);

    public Task<JsonElement> SetPublicAddressAsync(bool enabled, CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "access/public-address", new { enabled }, cancellationToken);

    // ------------------------------------------------------------------- setup

    public Task<SetupStatusDto> SetupStatusAsync(CancellationToken cancellationToken = default) =>
        GetAsync<SetupStatusDto>("setup/status", cancellationToken);

    public Task<JsonElement> CompleteSetupAsync(string teacherName, string schoolName,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, "setup/complete",
            new { teacher_name = teacherName, school_name = schoolName }, cancellationToken);

    /// <summary>Raw JSON for screens that display backend diagnostics verbatim.</summary>
    public Task<JsonElement> RawAsync(string path, CancellationToken cancellationToken = default) =>
        GetJsonAsync(path, cancellationToken);

    public Task<JsonElement> PostRawAsync(string path, object? payload,
        CancellationToken cancellationToken = default) =>
        SendJsonRawAsync(HttpMethod.Post, path, payload ?? new { }, cancellationToken);
}
