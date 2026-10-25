using System;
using System.Linq;
using System.Windows;

namespace MathChecker.Desktop.Core.UI;

public enum AppAppearance
{
    Light,
    Dark,
}

/// <summary>
/// Swaps the one theme dictionary at the front of the application resources.
/// Every colour in the shell is referenced with <c>DynamicResource</c>, so a
/// switch repaints the open window immediately; no page is rebuilt and no
/// backend call is made.
/// </summary>
public static class ThemeManager
{
    private const string LightSource = "Themes/LightTheme.xaml";
    private const string DarkSource = "Themes/DarkTheme.xaml";

    public static AppAppearance Current { get; private set; } = AppAppearance.Light;

    public static event EventHandler? Changed;

    public static AppAppearance Parse(string? value) =>
        string.Equals(value, "dark", StringComparison.OrdinalIgnoreCase)
            ? AppAppearance.Dark
            : AppAppearance.Light;

    public static string Serialize(AppAppearance appearance) =>
        appearance == AppAppearance.Dark ? "dark" : "light";

    public static void Apply(AppAppearance appearance)
    {
        Current = appearance;
        var app = Application.Current;
        if (app is null)
        {
            return;
        }

        var dictionaries = app.Resources.MergedDictionaries;
        var theme = new ResourceDictionary
        {
            Source = new Uri(appearance == AppAppearance.Dark ? DarkSource : LightSource,
                UriKind.Relative),
        };

        var index = -1;
        for (var i = 0; i < dictionaries.Count; i++)
        {
            var source = dictionaries[i].Source?.OriginalString;
            if (source is null)
            {
                continue;
            }

            if (source.EndsWith("LightTheme.xaml", StringComparison.OrdinalIgnoreCase)
                || source.EndsWith("DarkTheme.xaml", StringComparison.OrdinalIgnoreCase))
            {
                index = i;
                break;
            }
        }

        if (index >= 0)
        {
            dictionaries[index] = theme;
        }
        else
        {
            dictionaries.Insert(0, theme);
        }

        Changed?.Invoke(null, EventArgs.Empty);
    }

    /// <summary>Apply the appearance saved in desktop settings.</summary>
    public static void ApplySaved(string? saved) => Apply(Parse(saved));

    public static AppAppearance Next(AppAppearance appearance) =>
        appearance == AppAppearance.Dark ? AppAppearance.Light : AppAppearance.Dark;

    public static bool HasThemeDictionary() =>
        Application.Current?.Resources.MergedDictionaries.Any(d =>
            d.Source?.OriginalString.EndsWith("Theme.xaml", StringComparison.OrdinalIgnoreCase) == true)
        ?? false;
}
