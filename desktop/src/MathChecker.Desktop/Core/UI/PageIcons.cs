namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// One Segoe MDL2 Assets glyph per page. Kept out of the view models so the
/// navigation template only has to bind a string; unknown keys fall back to a
/// neutral document glyph rather than rendering a missing-character box.
/// </summary>
public static class PageIcons
{
    private const string Fallback = "\uE8A5";

    private static readonly Dictionary<string, string> Glyphs = new(StringComparer.OrdinalIgnoreCase)
    {
        ["dashboard"] = "\uE80F",   // Home
        ["search"] = "\uE721",      // Search
        ["correct"] = "\uE710",     // Add
        ["workers"] = "\uE9F5",     // Processing
        ["tasks"] = "\uE9D5",       // Checklist
        ["jobs"] = "\uE81C",        // History
        ["automation"] = "\uE945",  // Lightning
        ["review"] = "\uE73E",      // CheckMark
        ["scheme"] = "\uE8A5",      // Document
        ["desk"] = "\uE70F",        // Edit
        ["results"] = "\uE9D2",     // Chart
        ["reports"] = "\uE8A5",     // Document
        ["usage"] = "\uE9D9",       // Diagnostic
        ["classroom"] = "\uE716",   // People
        ["classes"] = "\uE716",     // People
        ["students"] = "\uE77B",    // Contact
        ["assignments"] = "\uE8A5", // Document
        ["settings"] = "\uE713",    // Settings
        ["ai"] = "\uE945",          // Lightning
        ["prompts"] = "\uE8A5",     // Document
        ["storage"] = "\uE74E",     // Save
        ["privacy"] = "\uE72E",     // Lock
        ["diagnostics"] = "\uE9D9", // Diagnostic
        ["audit"] = "\uE9D5",       // Checklist
        ["multischool"] = "\uE716", // People
        ["remote"] = "\uE774",      // Globe
        ["updates"] = "\uE895",     // Sync
    };

    public static string For(string? key) =>
        key is not null && Glyphs.TryGetValue(key, out var glyph) ? glyph : Fallback;

    private static readonly Dictionary<string, string> SpaceGlyphs = new(StringComparer.OrdinalIgnoreCase)
    {
        ["Desk"] = "\uE80F",      // Home
        ["Correct"] = "\uE710",   // Add
        ["Review"] = "\uE73E",    // CheckMark
        ["Results"] = "\uE9D2",   // Chart
        ["People"] = "\uE716",    // People
        ["Config"] = "\uE713",    // Settings
        ["System"] = "\uE9D9",    // Diagnostic
    };

    /// <summary>Glyph for a navigation space, used by the rail and palette.</summary>
    public static string ForSpace(string? space) =>
        space is not null && SpaceGlyphs.TryGetValue(space, out var glyph) ? glyph : Fallback;
}
