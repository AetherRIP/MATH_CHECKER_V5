using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;

namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// Opens a control's <see cref="ContextMenu"/> on a normal left click, so a
/// header "More" button can hold the actions that do not deserve a button of
/// their own. WPF only opens a context menu on right click or the menu key,
/// which is the wrong gesture for a button that says More.
/// </summary>
public static class ClickMenu
{
    public static readonly DependencyProperty OpenOnClickProperty = DependencyProperty.RegisterAttached(
        "OpenOnClick", typeof(bool), typeof(ClickMenu), new PropertyMetadata(false, OnOpenOnClickChanged));

    public static bool GetOpenOnClick(DependencyObject element) =>
        (bool)element.GetValue(OpenOnClickProperty);

    public static void SetOpenOnClick(DependencyObject element, bool value) =>
        element.SetValue(OpenOnClickProperty, value);

    private static void OnOpenOnClickChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is not ButtonBase button)
        {
            return;
        }

        button.Click -= OnClick;
        if (e.NewValue is true)
        {
            button.Click += OnClick;
        }
    }

    private static void OnClick(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement { ContextMenu: { } menu })
        {
            return;
        }

        menu.PlacementTarget = (UIElement)sender;
        menu.Placement = PlacementMode.Bottom;
        menu.IsOpen = true;
        e.Handled = true;
    }
}
