using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace MathChecker.Desktop.Core.UI;

/// <summary>One labelled value in a payload summary.</summary>
public sealed record FactRow(string Label, string Value);

/// <summary>
/// Turns a backend payload into sentences and labelled facts.
///
/// The pages that used to dump <c>Json.Pretty()</c> into a monospace box all go
/// through here, so a key like <c>has_mark_scheme</c> reads "Has mark scheme ·
/// No" everywhere, a timestamp is shown in local time, and an enum status is
/// never shouted at a teacher. Raw JSON survives only inside an explicit
/// "Technical details" expander.
/// </summary>
public static class PayloadFacts
{
    private static readonly Dictionary<string, string> KnownWords = new(StringComparer.OrdinalIgnoreCase)
    {
        ["id"] = "ID",
        ["ids"] = "IDs",
        ["pdf"] = "PDF",
        ["ai"] = "AI",
        ["api"] = "API",
        ["url"] = "URL",
        ["uri"] = "URI",
        ["oauth"] = "OAuth",
        ["csv"] = "CSV",
        ["json"] = "JSON",
        ["tsv"] = "TSV",
        ["mb"] = "MB",
        ["gb"] = "GB",
        ["mc"] = "Math Checker",
        ["ui"] = "UI",
        ["http"] = "HTTP",
        ["https"] = "HTTPS",
        ["ip"] = "IP",
        ["db"] = "Database",
    };

    /// <summary>Human label for a payload key: <c>has_mark_scheme</c> becomes
    /// "Has mark scheme", <c>courseworkId</c> becomes "Coursework ID".</summary>
    public static string KeyLabel(string key)
    {
        if (string.IsNullOrWhiteSpace(key))
        {
            return string.Empty;
        }

        var spaced = Regex.Replace(key, "([a-z0-9])([A-Z])", "$1 $2");
        var parts = spaced.Split(new[] { '_', '-', ' ' }, StringSplitOptions.RemoveEmptyEntries);
        var builder = new StringBuilder();
        foreach (var part in parts)
        {
            if (builder.Length > 0)
            {
                builder.Append(' ');
            }

            builder.Append(KnownWords.TryGetValue(part, out var known)
                ? known
                : char.ToUpperInvariant(part[0]) + part[1..]);
        }

        return builder.ToString();
    }

    /// <summary>An internal state such as <c>TURNED_IN</c> or
    /// <c>CONFIRM_SCHEME_CORRECT</c> written the way a person would say it.</summary>
    public static string StatusLabel(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return "—";
        }

        var text = value.Trim().Replace('_', ' ').ToLowerInvariant();
        if (KnownWords.TryGetValue(value.Trim(), out var known))
        {
            return known;
        }

        return char.ToUpperInvariant(text[0]) + text[1..];
    }

    /// <summary>Flattens any payload into labelled facts. Nested objects use a
    /// "Parent · Child" label; arrays report their length and expand a few
    /// entries rather than printing brackets and quotes.</summary>
    public static IReadOnlyList<FactRow> Of(JsonElement element, int maxRows = 40)
    {
        var rows = new List<FactRow>();
        Walk(element, null, rows, maxRows, 0);
        return rows;
    }

    private static void Walk(JsonElement element, string? prefix, List<FactRow> rows, int maxRows, int depth)
    {
        if (rows.Count >= maxRows)
        {
            return;
        }

        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                foreach (var property in element.EnumerateObject())
                {
                    if (rows.Count >= maxRows)
                    {
                        return;
                    }

                    var label = string.IsNullOrEmpty(prefix)
                        ? KeyLabel(property.Name)
                        : $"{prefix} · {KeyLabel(property.Name)}";

                    if (property.Value.ValueKind == JsonValueKind.Object && depth < 3)
                    {
                        Walk(property.Value, label, rows, maxRows, depth + 1);
                        continue;
                    }

                    if (property.Value.ValueKind == JsonValueKind.Array)
                    {
                        AddArray(property.Name, property.Value, label, rows, maxRows, depth);
                        continue;
                    }

                    rows.Add(new FactRow(label, ValueLabel(property.Name, property.Value)));
                }

                if (rows.Count == 0)
                {
                    rows.Add(new FactRow(string.IsNullOrEmpty(prefix) ? "Contents" : prefix, "Nothing recorded"));
                }

                break;

            case JsonValueKind.Array:
                AddArray(string.Empty, element, prefix ?? "Items", rows, maxRows, depth);
                break;

            default:
                rows.Add(new FactRow(prefix ?? "Value", ValueLabel(prefix ?? string.Empty, element)));
                break;
        }
    }

    private static void AddArray(string key, JsonElement array, string label, List<FactRow> rows, int maxRows,
        int depth)
    {
        var items = array.EnumerateArray().ToList();
        if (items.Count == 0)
        {
            rows.Add(new FactRow(label, "None"));
            return;
        }

        if (items.All(item => item.ValueKind != JsonValueKind.Object && item.ValueKind != JsonValueKind.Array))
        {
            var joined = string.Join(", ", items.Take(8).Select(item => ValueLabel(key, item)));
            rows.Add(new FactRow(label,
                items.Count > 8 ? $"{joined} … ({items.Count} in total)" : joined));
            return;
        }

        rows.Add(new FactRow(label, items.Count == 1 ? "1 entry" : $"{items.Count} entries"));
        var shown = 0;
        foreach (var item in items)
        {
            if (rows.Count >= maxRows || shown >= 4)
            {
                break;
            }

            var itemLabel = $"{label} {shown + 1}";
            Walk(item, itemLabel, rows, maxRows, depth + 1);
            shown++;
        }

        if (items.Count > shown)
        {
            rows.Add(new FactRow(label, $"{items.Count - shown} more not shown"));
        }
    }

    /// <summary>One readable value: booleans as Yes/No, timestamps in local
    /// time, percentages and durations with their unit.</summary>
    public static string ValueLabel(string key, JsonElement value)
    {
        switch (value.ValueKind)
        {
            case JsonValueKind.True:
                return "Yes";
            case JsonValueKind.False:
                return "No";
            case JsonValueKind.Null:
            case JsonValueKind.Undefined:
                return "Not set";
            case JsonValueKind.Number:
                return NumberLabel(key, value);
            case JsonValueKind.String:
                return StringLabel(key, value.GetString() ?? string.Empty);
            case JsonValueKind.Array:
                var count = value.GetArrayLength();
                return count switch
                {
                    0 => "None",
                    1 => "1 entry",
                    _ => $"{count} entries",
                };
            default:
                return "Not set";
        }
    }

    private static string NumberLabel(string key, JsonElement value)
    {
        var number = value.TryGetDouble(out var parsed) ? parsed : 0;
        var text = Math.Abs(number % 1) < 0.0001
            ? number.ToString("0", CultureInfo.InvariantCulture)
            : number.ToString("0.##", CultureInfo.InvariantCulture);

        var lower = key.ToLowerInvariant();
        if (lower.Contains("percent") || lower.EndsWith("_pct"))
        {
            return text + "%";
        }

        if (lower.EndsWith("_ms") || lower.Contains("milliseconds"))
        {
            return text + " ms";
        }

        if (lower.EndsWith("_mb"))
        {
            return text + " MB";
        }

        if (lower.EndsWith("_seconds") || lower.EndsWith("_s") && lower.Contains("elapsed"))
        {
            return text + " s";
        }

        return text;
    }

    private static string StringLabel(string key, string raw)
    {
        var text = raw.Trim();
        if (text.Length == 0)
        {
            return "—";
        }

        var lower = key.ToLowerInvariant();
        if ((lower.Contains("at") || lower.Contains("time") || lower.Contains("date"))
            && TryFormatTimestamp(text, out var formatted))
        {
            return formatted;
        }

        // An all-caps token is an internal enum, not prose.
        if (text.Length > 2 && text.All(c => !char.IsLetter(c) || char.IsUpper(c)) && text.Contains('_'))
        {
            return StatusLabel(text);
        }

        if (TryFormatTimestamp(text, out var iso))
        {
            return iso;
        }

        return text;
    }

    /// <summary>ISO-8601 timestamps are shown in local time; anything else is
    /// left alone so a value is never silently rewritten.</summary>
    public static bool TryFormatTimestamp(string raw, out string formatted)
    {
        formatted = raw;
        if (raw.Length < 10 || !raw.Contains('T') || !raw.Contains('-'))
        {
            return false;
        }

        if (!DateTimeOffset.TryParse(raw, CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var parsed))
        {
            return false;
        }

        formatted = parsed.ToLocalTime().ToString("d MMM yyyy HH:mm", CultureInfo.InvariantCulture);
        return true;
    }

    /// <summary>A short, plain description of a payload for headers and cards.</summary>
    public static string Summarize(JsonElement element)
    {
        if (element.ValueKind != JsonValueKind.Object)
        {
            return string.Empty;
        }

        foreach (var candidate in new[] { "message", "detail", "reason", "summary", "status" })
        {
            if (element.TryGetProperty(candidate, out var value)
                && value.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(value.GetString()))
            {
                return value.GetString()!;
            }
        }

        return string.Empty;
    }

    /// <summary>The raw payload, only ever shown inside technical details.</summary>
    public static string Raw(JsonElement element)
    {
        try
        {
            return JsonSerializer.Serialize(element, new JsonSerializerOptions { WriteIndented = true });
        }
        catch
        {
            return element.ToString();
        }
    }
}
