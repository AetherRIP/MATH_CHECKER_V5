using System.Text.Json;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Core.Api;

/// <summary>
/// The Marking Desk's read paths. Split from the main client so the page
/// viewer's contract (metadata + PNG bytes) stays next to its own comments.
/// </summary>
public sealed partial class MathCheckerApiClient
{
    /// <summary>
    /// Which pages of the original submission exist for a result, or the
    /// backend's specific reason why they do not. Never invent a reason from
    /// this side: <see cref="Models.ResultPagesDto.Reason"/> is authoritative.
    /// </summary>
    public Task<ResultPagesDto> ResultPagesAsync(string resultId,
        CancellationToken cancellationToken = default) =>
        GetAsync<ResultPagesDto>($"results/{Uri.EscapeDataString(resultId)}/pages",
            cancellationToken);

    /// <summary>
    /// One rendered page of the student's work. The backend caches renders on
    /// disk per document/page/dpi and clamps <paramref name="dpi"/> to 72–300.
    /// </summary>
    public Task<byte[]> ResultPagePngAsync(string resultId, int page, int dpi = 150,
        CancellationToken cancellationToken = default) =>
        GetBytesAsync(
            $"results/{Uri.EscapeDataString(resultId)}/pages/{page}.png" + Query(("dpi", dpi)),
            "image/png", cancellationToken);

    /// <summary>
    /// Records a teacher decision for the whole paper. Passing no mark approves
    /// the engine's marks; passing one settles the paper (required when the
    /// correction is partial).
    /// </summary>
    public Task<JsonElement> ApproveResultAsync(string resultId, int? earned, string note,
        CancellationToken cancellationToken = default) =>
        OverrideResultAsync(resultId, earned, note, cancellationToken);
}
