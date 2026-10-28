using System.Collections;
using System.Resources;
using MathChecker.Desktop.Core.UI;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The theme dictionary must be addressed in the DESKTOP assembly, not resolved
/// against whichever assembly started the process.
///
/// <c>ThemeManager</c> used a relative pack URI, and WPF resolves those against
/// the entry assembly. That is the app itself in a normal launch, so it looked
/// fine — but hosting the desktop from another assembly (the end-to-end harness
/// does exactly this) threw "Cannot locate resource 'themes/lighttheme.xaml'" at
/// startup, before any window could appear. The source must therefore be an
/// absolute pack URI carrying this assembly's name.
/// </summary>
public class ThemeResourceTests
{
    [Theory]
    [InlineData(AppAppearance.Light, "Themes/LightTheme.xaml")]
    [InlineData(AppAppearance.Dark, "Themes/DarkTheme.xaml")]
    public void The_theme_source_is_an_absolute_uri_in_the_desktop_assembly(
        AppAppearance appearance, string expectedPath)
    {
        var source = ThemeManager.SourceFor(appearance);

        // Shape: absolute pack URI, qualified with the assembly that holds the
        // theme. A relative path here is the bug this test exists for.
        Assert.StartsWith("pack://application:,,,/", source, StringComparison.OrdinalIgnoreCase);
        var assembly = typeof(ThemeManager).Assembly.GetName().Name;
        Assert.Contains($"/{assembly};component/", source, StringComparison.OrdinalIgnoreCase);
        Assert.EndsWith("/" + expectedPath, source, StringComparison.OrdinalIgnoreCase);

        // And WPF can actually construct it (the old relative form only worked
        // when the process happened to be the desktop itself). Touching
        // PackUriHelper first registers WPF's "pack" scheme parser, which a
        // bare test host has not done yet; the real app registers it at startup.
        _ = System.IO.Packaging.PackUriHelper.UriSchemePack;
        var uri = new Uri(source, UriKind.Absolute);
        Assert.True(uri.IsAbsoluteUri);
        Assert.Equal("pack", uri.Scheme);
    }

    [Fact]
    public void Both_theme_files_are_embedded_in_the_desktop_assembly()
    {
        var assembly = typeof(ThemeManager).Assembly;
        var pack = assembly.GetManifestResourceNames()
            .SingleOrDefault(n => n.EndsWith(".g.resources", StringComparison.OrdinalIgnoreCase));
        Assert.NotNull(pack);

        // Compiled XAML lives as .baml inside the "<assembly>.g.resources" pack,
        // which is exactly what the pack URI above reaches.
        using var stream = assembly.GetManifestResourceStream(pack!)!;
        using var reader = new ResourceReader(stream);
        var entries = reader.Cast<DictionaryEntry>().Select(e => (string)e.Key).ToList();

        Assert.Contains(entries, k => k.EndsWith("themes/lighttheme.baml", StringComparison.OrdinalIgnoreCase));
        Assert.Contains(entries, k => k.EndsWith("themes/darktheme.baml", StringComparison.OrdinalIgnoreCase));
    }
}
