using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Core.Api;

/// <summary>
/// Peer networking. Two calls, because the feature is a switch and an
/// observation of what that switch actually produced.
/// </summary>
public sealed partial class MathCheckerApiClient
{
    /// <summary>Reads the stored preference and the observed process state.
    /// The backend starts nothing to answer this.</summary>
    public Task<PeerStatusDto> PeerStatusAsync(CancellationToken cancellationToken = default) =>
        GetAsync<PeerStatusDto>("peer/status", cancellationToken);

    /// <summary>Records the preference and starts/stops the supervisor. The
    /// answer carries what was observed afterwards, not what was asked for.</summary>
    public Task<PeerStatusDto> SetPeerEnabledAsync(bool enabled, CancellationToken cancellationToken = default) =>
        SendJsonAsync<PeerStatusDto>(HttpMethod.Post, "peer/preference", new { enabled }, cancellationToken);
}
