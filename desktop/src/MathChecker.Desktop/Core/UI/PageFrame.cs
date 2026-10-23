using System.Windows;
using System.Windows.Controls;

namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// The shared page shell: space eyebrow, title, one-line description, the
/// page's actions, its honest status, and the backend error strip. Every page
/// renders inside one of these so headers, failures and spacing never drift
/// between screens.
/// </summary>
public class PageFrame : ContentControl
{
    public static readonly DependencyProperty EyebrowProperty = DependencyProperty.Register(
        nameof(Eyebrow), typeof(string), typeof(PageFrame), new PropertyMetadata(string.Empty));

    public static readonly DependencyProperty TitleProperty = DependencyProperty.Register(
        nameof(Title), typeof(string), typeof(PageFrame), new PropertyMetadata(string.Empty));

    public static readonly DependencyProperty DescriptionProperty = DependencyProperty.Register(
        nameof(Description), typeof(string), typeof(PageFrame), new PropertyMetadata(string.Empty));

    public static readonly DependencyProperty StatusTextProperty = DependencyProperty.Register(
        nameof(StatusText), typeof(string), typeof(PageFrame), new PropertyMetadata(string.Empty));

    public static readonly DependencyProperty ErrorTextProperty = DependencyProperty.Register(
        nameof(ErrorText), typeof(string), typeof(PageFrame), new PropertyMetadata(
            string.Empty, OnErrorTextChanged));

    public static readonly DependencyProperty IsBusyProperty = DependencyProperty.Register(
        nameof(IsBusy), typeof(bool), typeof(PageFrame), new PropertyMetadata(false));

    public static readonly DependencyProperty ShowHeaderProperty = DependencyProperty.Register(
        nameof(ShowHeader), typeof(bool), typeof(PageFrame), new PropertyMetadata(true));

    public static readonly DependencyProperty BodyPaddingProperty = DependencyProperty.Register(
        nameof(BodyPadding), typeof(Thickness), typeof(PageFrame), new PropertyMetadata(new Thickness(0, 20, 0, 0)));

    public static readonly DependencyProperty ActionsProperty = DependencyProperty.Register(
        nameof(Actions), typeof(object), typeof(PageFrame), new PropertyMetadata(null));

    private static readonly DependencyPropertyKey HasErrorKey = DependencyProperty.RegisterReadOnly(
        nameof(HasError), typeof(bool), typeof(PageFrame), new PropertyMetadata(false));

    public static readonly DependencyProperty HasErrorProperty = HasErrorKey.DependencyProperty;

    public string Eyebrow
    {
        get => (string)GetValue(EyebrowProperty);
        set => SetValue(EyebrowProperty, value);
    }

    public string Title
    {
        get => (string)GetValue(TitleProperty);
        set => SetValue(TitleProperty, value);
    }

    public string Description
    {
        get => (string)GetValue(DescriptionProperty);
        set => SetValue(DescriptionProperty, value);
    }

    /// <summary>Short, honest progress or completion text set by the page.</summary>
    public string StatusText
    {
        get => (string)GetValue(StatusTextProperty);
        set => SetValue(StatusTextProperty, value);
    }

    /// <summary>The backend's message for the last failed call, or empty.</summary>
    public string ErrorText
    {
        get => (string)GetValue(ErrorTextProperty);
        set => SetValue(ErrorTextProperty, value);
    }

    public bool IsBusy
    {
        get => (bool)GetValue(IsBusyProperty);
        set => SetValue(IsBusyProperty, value);
    }

    public bool ShowHeader
    {
        get => (bool)GetValue(ShowHeaderProperty);
        set => SetValue(ShowHeaderProperty, value);
    }

    /// <summary>Gap between the header rule and the page body.</summary>
    public Thickness BodyPadding
    {
        get => (Thickness)GetValue(BodyPaddingProperty);
        set => SetValue(BodyPaddingProperty, value);
    }

    /// <summary>Buttons and chips on the right of the header. Pages keep their
    /// own command bindings; the frame only lays them out.</summary>
    public object? Actions
    {
        get => GetValue(ActionsProperty);
        set => SetValue(ActionsProperty, value);
    }

    public bool HasError => (bool)GetValue(HasErrorProperty);

    private static void OnErrorTextChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is PageFrame frame)
        {
            frame.SetValue(HasErrorKey, !string.IsNullOrWhiteSpace(e.NewValue as string));
        }
    }
}