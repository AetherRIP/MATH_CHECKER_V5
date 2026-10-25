using System.Windows;
using System.Windows.Input;
using MathChecker.Desktop.Core.UI;

namespace MathChecker.Desktop.Views;

public enum MessageKind
{
    Confirm,
    Info,
    Error,
}

/// <summary>
/// A themed replacement for the native message box: the same surface, radius
/// and type as the rest of the shell, in the current appearance. Confirm asks a
/// question, Info and Error acknowledge one.
/// </summary>
public partial class MessageWindow : Window
{
    public MessageWindow(string title, string message, MessageKind kind)
    {
        InitializeComponent();
        TitleText.Text = title;
        MessageText.Text = message;
        KindText.Text = kind switch
        {
            MessageKind.Error => "SOMETHING WENT WRONG",
            MessageKind.Info => "MATH CHECKER",
            _ => "PLEASE CONFIRM",
        };
        MarkGlyph.Text = kind switch
        {
            MessageKind.Error => "\uE783",       // error badge
            MessageKind.Info => "\uE946",        // info
            _ => "\uE8BD",                       // help / question
        };
        if (kind == MessageKind.Error)
        {
            Mark.Background = (System.Windows.Media.Brush)FindResource("DangerSurfaceBrush");
            MarkGlyph.Foreground = (System.Windows.Media.Brush)FindResource("DangerTextBrush");
            OkButton.Style = (Style)FindResource("DangerButton");
        }

        CancelButton.Visibility = kind == MessageKind.Confirm ? Visibility.Visible : Visibility.Collapsed;
        OkButton.Content = kind == MessageKind.Confirm ? "Confirm" : "OK";

        Loaded += (_, _) =>
        {
            Motion.FadeIn(this);
            OkButton.Focus();
        };
    }

    public bool Accepted { get; private set; }

    public static bool Confirm(string title, string message)
    {
        var window = new MessageWindow(title, message, MessageKind.Confirm);
        var owner = Application.Current?.MainWindow;
        if (owner is { IsLoaded: true } && !ReferenceEquals(owner, window))
        {
            window.Owner = owner;
        }
        return window.ShowDialog() == true;
    }

    public static void Show(string title, string message, MessageKind kind)
    {
        var window = new MessageWindow(title, message, kind);
        var owner = Application.Current?.MainWindow;
        if (owner is { IsLoaded: true } && !ReferenceEquals(owner, window))
        {
            window.Owner = owner;
        }
        window.ShowDialog();
    }

    private void OnOk(object sender, RoutedEventArgs e)
    {
        Accepted = true;
        DialogResult = true;
    }

    private void OnCancel(object sender, RoutedEventArgs e)
    {
        Accepted = false;
        DialogResult = false;
    }

    private void OnChromeMouseDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ChangedButton == MouseButton.Left && e.OriginalSource is not System.Windows.Controls.Button)
        {
            try { DragMove(); } catch (InvalidOperationException) { }
        }
    }
}
