using System.Net.Http;
using System.Text.Json;
using MathChecker.Desktop.Core.Api;

namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// The one place a failure becomes a sentence a teacher can act on.
///
/// The backend already writes its own errors for people (<see cref="ApiException.Display"/>),
/// so those pass through untouched. Anything else — a dropped connection, a
/// timeout, a payload the client could not parse — is described in plain
/// language here instead of printing the exception's own text, which is aimed
/// at a developer. The original exception is still logged by the caller.
/// </summary>
public static class UserMessage
{
    public const string RestartHint = "If it keeps happening, restart the backend from the Backend menu.";

    /// <summary>A complete, actionable sentence for any failure.</summary>
    public static string For(Exception exception) => exception switch
    {
        ApiException api => api.Display,
        HttpRequestException => "The backend is not reachable. Check that it is running, then refresh.",
        TaskCanceledException or TimeoutException =>
            "The backend did not answer in time. It may still be working; refresh to check.",
        JsonException or FormatException or InvalidOperationException =>
            "The backend answered with something this application could not read. " + RestartHint,
        _ => "Something went wrong while talking to the backend. " + RestartHint,
    };

    /// <summary>The same sentence, for a caller that prefixes context
    /// ("The paper was not approved: …") so the result still reads as one
    /// grammatical sentence.</summary>
    public static string With(string context, Exception exception) =>
        context.TrimEnd().TrimEnd(':') + ": " + LowerFirst(For(exception));

    private static string LowerFirst(string text) =>
        text.Length == 0 || char.IsLower(text[0]) ? text : char.ToLowerInvariant(text[0]) + text[1..];
}
