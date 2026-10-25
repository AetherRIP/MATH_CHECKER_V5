using System;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// The one place animation timing is decided. Windows' "Show animations"
/// setting is honoured, and every helper is a short, GPU-friendly opacity or
/// translation: no layout animation, no timers, nothing that keeps running
/// after the visual state has settled.
/// </summary>
public static class Motion
{
    public static readonly TimeSpan Quick = TimeSpan.FromMilliseconds(120);
    public static readonly TimeSpan Base = TimeSpan.FromMilliseconds(220);
    public static readonly TimeSpan Slow = TimeSpan.FromMilliseconds(320);

    /// <summary>True when the OS reports that window animations are allowed.</summary>
    public static bool IsEnabled
    {
        get
        {
            try
            {
                return SystemParameters.ClientAreaAnimation;
            }
            catch (Exception)
            {
                return true;
            }
        }
    }

    /// <summary>
    /// Fade and lift an element into place. Both properties are animated on the
    /// compositor (Opacity and a RenderTransform), so a page change never
    /// triggers a layout pass while the user is already clicking.
    /// </summary>
    public static void Enter(FrameworkElement element, double offset = 10, TimeSpan? duration = null)
    {
        if (element is null || !IsEnabled)
        {
            return;
        }

        var transform = element.RenderTransform as TranslateTransform;
        if (transform is null || transform.IsFrozen)
        {
            transform = new TranslateTransform();
            element.RenderTransform = transform;
        }

        element.Opacity = 0;
        transform.Y = offset;

        var ease = new CubicEase { EasingMode = EasingMode.EaseOut };
        var length = duration ?? Base;
        element.BeginAnimation(UIElement.OpacityProperty,
            new DoubleAnimation(0, 1, new Duration(length)) { EasingFunction = ease });
        transform.BeginAnimation(TranslateTransform.YProperty,
            new DoubleAnimation(offset, 0, new Duration(length)) { EasingFunction = ease });
    }

    /// <summary>Fade a surface in where motion would be distracting (dialogs).</summary>
    public static void FadeIn(FrameworkElement element, TimeSpan? duration = null)
    {
        if (element is null || !IsEnabled)
        {
            return;
        }

        element.BeginAnimation(UIElement.OpacityProperty,
            new DoubleAnimation(0, 1, new Duration(duration ?? Quick))
            {
                EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut },
            });
    }

    public static void Clear(FrameworkElement element)
    {
        element?.BeginAnimation(UIElement.OpacityProperty, null);
        if (element?.RenderTransform is TranslateTransform transform)
        {
            transform.BeginAnimation(TranslateTransform.YProperty, null);
        }
    }
}
