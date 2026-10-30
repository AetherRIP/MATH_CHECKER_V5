using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Core.Api;

/// <summary>
/// The teacher-certified benchmark contract, kept in its own partial file so
/// the main client stays a catalogue of the correction endpoints. Paths and
/// payload fields are the FastAPI routes in app/api/benchmark_routes.py; the
/// desktop page never derives a certified score of its own.
/// </summary>
public sealed partial class MathCheckerApiClient
{
    public Task<BenchmarkOverviewDto> BenchmarkOverviewAsync(
        CancellationToken cancellationToken = default) =>
        GetAsync<BenchmarkOverviewDto>("benchmark/overview", cancellationToken);

    public Task<BenchmarkReferencesDto> BenchmarkReferencesAsync(string? status = null,
        string? partition = null, int limit = 500, CancellationToken cancellationToken = default) =>
        GetAsync<BenchmarkReferencesDto>(
            "benchmark/references" + Query(("status", status), ("partition", partition),
                ("limit", limit)),
            cancellationToken);

    public Task<BenchmarkReferenceDto> BenchmarkReferenceAsync(string referenceId,
        CancellationToken cancellationToken = default) =>
        GetAsync<BenchmarkReferenceDto>(
            $"benchmark/references/{Uri.EscapeDataString(referenceId)}", cancellationToken);

    /// <summary>
    /// Appends one teacher assessment. <paramref name="confirmed"/> is sent
    /// verbatim: the backend rejects anything without an explicit true, and the
    /// view model only calls this after the teacher ticks the confirmation box.
    /// </summary>
    public Task<BenchmarkVerifyResultDto> VerifyBenchmarkReferenceAsync(string referenceId,
        int? awarded, string status, string assessor, string role, string note,
        IReadOnlyList<string> alternativeMethods, bool confirmed,
        CancellationToken cancellationToken = default)
    {
        var payload = new
        {
            confirm = confirmed,
            awarded,
            status,
            assessor,
            role,
            note,
            alternative_methods = alternativeMethods.ToArray(),
        };
        return SendJsonAsync<BenchmarkVerifyResultDto>(HttpMethod.Post,
            $"benchmark/references/{Uri.EscapeDataString(referenceId)}/verify",
            payload, cancellationToken);
    }

    /// <summary>
    /// Recomputes a recorded run from its stored reference snapshot and raw
    /// outcomes. It contacts no provider and reads no live assessment store, so
    /// it is safe from the page; starting a NEW evaluation is not exposed.
    /// </summary>
    public Task<BenchmarkReproductionDto> ReproduceBenchmarkRunAsync(string runId,
        CancellationToken cancellationToken = default) =>
        SendJsonAsync<BenchmarkReproductionDto>(HttpMethod.Post,
            $"benchmark/runs/{Uri.EscapeDataString(runId)}/reproduce", new { },
            cancellationToken);
}
