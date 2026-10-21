using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Controls;

public partial class PaperPaneView : UserControl
{
    public PaperPaneView()
    {
        InitializeComponent();
        Loaded += (_, _) => UpdateViewport();
        SizeChanged += (_, _) => UpdateViewport();
        DataContextChanged += (_, _) => UpdateViewport();
        PreviewMouseWheel += OnPreviewMouseWheel;
        PreviewKeyDown += OnPreviewKeyDown;
    }

    private PaperPaneViewModel? ViewModel => DataContext as PaperPaneViewModel;

    /// <summary>
    /// Gives the paper keyboard focus so PageUp/PageDown and Space act on it.
    /// Returns false when there is no page to focus (missing original).
    /// </summary>
    public bool FocusPaper()
    {
        if (ViewModel is not { IsAvailable: true })
        {
            return false;
        }

        return PageScroll.Focus();
    }

    private void UpdateViewport()
    {
        // The border's chrome and the scrollbar gutter are not page width.
        ViewModel?.SetViewportWidth(Math.Max(0, ActualWidth - 32));
    }

    private void OnPreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (ViewModel is not { } viewModel
            || !Keyboard.Modifiers.HasFlag(ModifierKeys.Control))
        {
            return;
        }

        if (e.Delta > 0)
        {
            viewModel.ZoomInCommand.Execute(null);
        }
        else
        {
            viewModel.ZoomOutCommand.Execute(null);
        }

        e.Handled = true;
    }

    private void OnPreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (ViewModel is not { } viewModel)
        {
            return;
        }

        var control = Keyboard.Modifiers.HasFlag(ModifierKeys.Control);
        switch (e.Key)
        {
            case Key.PageUp:
                viewModel.PreviousPageCommand.Execute(null);
                e.Handled = true;
                break;
            case Key.PageDown:
                viewModel.NextPageCommand.Execute(null);
                e.Handled = true;
                break;
            case Key.Add or Key.OemPlus when control:
                viewModel.ZoomInCommand.Execute(null);
                e.Handled = true;
                break;
            case Key.Subtract or Key.OemMinus when control:
                viewModel.ZoomOutCommand.Execute(null);
                e.Handled = true;
                break;
            case Key.D0 when control:
                viewModel.FitWidthCommand.Execute(null);
                e.Handled = true;
                break;
        }
    }
}
