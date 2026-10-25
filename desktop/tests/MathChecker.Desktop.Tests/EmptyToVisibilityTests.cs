using System.Globalization;
using System.Windows;
using MathChecker.Desktop.Converters;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// EmptyToVisibility drives placeholder text drawn over a control. It must be
/// visible ONLY when there is genuinely nothing to show: a selected object is
/// content, not an empty value. Treating it as empty left "Choose a course"
/// painted on top of the chosen course's name.
/// </summary>
public class EmptyToVisibilityTests
{
    private static Visibility Convert(object? value) =>
        (Visibility)new EmptyStringToVisibilityConverter()
            .Convert(value, typeof(Visibility), null, CultureInfo.InvariantCulture);

    [Fact]
    public void No_value_shows_the_placeholder() => Assert.Equal(Visibility.Visible, Convert(null));

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void A_blank_string_shows_the_placeholder(string value) =>
        Assert.Equal(Visibility.Visible, Convert(value));

    [Fact]
    public void A_selected_object_hides_the_placeholder() =>
        Assert.Equal(Visibility.Collapsed, Convert(new object()));

    [Fact]
    public void Typed_text_hides_the_placeholder() =>
        Assert.Equal(Visibility.Collapsed, Convert("TESTING — 12Se"));
}
