using System.Text.Json.Serialization;

namespace MathChecker.Desktop.Models;

/// <summary>
/// What the backend reports about peer networking.
///
/// <see cref="Enabled"/> is the STORED preference; <see cref="State"/> is what was
/// observed when this was built. They are separate fields because they disagree
/// routinely — a machine that was switched on yesterday shows Enabled with a
/// state of STOPPED until something starts it.
/// </summary>
public sealed class PeerStatusDto
{
    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; }

    [JsonPropertyName("state")]
    public string? State { get; set; }

    [JsonPropertyName("label")]
    public string? Label { get; set; }

    [JsonPropertyName("detail")]
    public string? Detail { get; set; }

    [JsonPropertyName("corrupt")]
    public bool Corrupt { get; set; }

    /// <summary>True only when the backend process answering this request owns a
    /// supervisor for the child. A false value with Enabled true means switched
    /// on but not started here.</summary>
    [JsonPropertyName("supervised")]
    public bool Supervised { get; set; }

    [JsonPropertyName("credentials_present")]
    public bool CredentialsPresent { get; set; }

    /// <summary>Present only on the answer to a switch change: did the requested
    /// operation reach the state it asked for?</summary>
    [JsonPropertyName("confirmed")]
    public bool? Confirmed { get; set; }

    [JsonPropertyName("service")]
    public PeerServiceDto? Service { get; set; }
}

public sealed class PeerServiceDto
{
    [JsonPropertyName("pid")]
    public int? Pid { get; set; }

    [JsonPropertyName("restarts")]
    public int Restarts { get; set; }

    [JsonPropertyName("encrypted_store")]
    public bool? EncryptedStore { get; set; }

    [JsonPropertyName("store_detail")]
    public string? StoreDetail { get; set; }

    [JsonPropertyName("started_at")]
    public double? StartedAt { get; set; }
}
