using System.Text.RegularExpressions;

namespace MathChecker.Desktop.Core.Diagnostics;

/// <summary>
/// Removes credentials and personal identifiers before a line reaches the
/// desktop log file. The backend already avoids logging secrets; the client
/// must not reintroduce them through exception messages or URLs.
/// </summary>
public static partial class SecretRedactor
{
    private const string Redacted = "<redacted>";

    public static string Redact(string? value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return string.Empty;
        }

        var text = GoogleApiKey().Replace(value, "<google-key>");
        text = BearerToken().Replace(text, "Bearer " + Redacted);
        text = CredentialPair().Replace(text, m => m.Groups[1].Value + Redacted);
        text = EmailAddress().Replace(text, "<email>");
        return text;
    }

    // AIza... is the shape of a Gemini/Google API key.
    [GeneratedRegex(@"AIza[0-9A-Za-z_\-]{20,}", RegexOptions.CultureInvariant)]
    private static partial Regex GoogleApiKey();

    [GeneratedRegex(@"Bearer\s+[A-Za-z0-9._\-]{8,}", RegexOptions.CultureInvariant | RegexOptions.IgnoreCase)]
    private static partial Regex BearerToken();

    // key=..., "api_key": "...", password=..., token: ...
    [GeneratedRegex("(?i)((?:api[_-]?key|password|passwd|secret|access[_-]?token|refresh[_-]?token|authorization|session|csrf)[\"']?\\s*[:=]\\s*[\"']?)([^\\s\"',;}&]+)")]
    private static partial Regex CredentialPair();

    // Student e-mail addresses must not reach a log file.
    [GeneratedRegex(@"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", RegexOptions.CultureInvariant)]
    private static partial Regex EmailAddress();
}
