using System.ComponentModel;
using System.Windows;
using System.Windows.Input;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop;

public partial class MainWindow : Window
{
    private readonly AppSession _session;
    private readonly MainViewModel _viewModel;
    private bool _shutdownStarted;
    private bool _shutdownFinished;

    public MainWindow(AppSession session)
    {
        InitializeComponent();
        WindowTheme.ApplyTitleBar(this);
        ThemeManager.Changed += (_, _) => WindowTheme.Refresh(this);
        _session = session;
        _viewModel = new MainViewModel(session);
        DataContext = _viewModel;
        _viewModel.ExitRequested += Close;

        var settings = session.Settings;
        var workArea = SystemParameters.WorkArea;
        // WPF reports the full screen for WorkArea under PerMonitorV2 on some
        // shells; keep a taskbar's height clear so the status footer is visible.
        var usableHeight = Math.Max(MinHeight, workArea.Height - 48);
        Width = Math.Max(MinWidth, Math.Min(settings.WindowWidth, workArea.Width));
        Height = Math.Max(MinHeight, Math.Min(settings.WindowHeight, usableHeight));
        if (settings.WindowMaximized)
        {
            WindowState = WindowState.Maximized;
        }

        _viewModel.PropertyChanged += OnViewModelPropertyChanged;
        Loaded += async (_, _) => await _viewModel.InitializeAsync();
        Closing += OnClosing;
    }

    private void OnViewModelPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName != nameof(MainViewModel.IsPaletteOpen) || !_viewModel.IsPaletteOpen)
        {
            return;
        }

        // The overlay is only in the tree while open; wait for the visibility
        // change to settle before taking focus.
        Dispatcher.BeginInvoke(new Action(() =>
        {
            PaletteInput.Focus();
            PaletteInput.SelectAll();
        }), DispatcherPriority.Input);
    }

    private void OnPaletteKeyDown(object sender, KeyEventArgs e)
    {
        switch (e.Key)
        {
            case Key.Escape:
                _viewModel.ClosePalette();
                e.Handled = true;
                break;
            case Key.Enter:
                _viewModel.CommitPalette();
                e.Handled = true;
                break;
            case Key.Down:
                _viewModel.MovePaletteSelection(1);
                e.Handled = true;
                break;
            case Key.Up:
                _viewModel.MovePaletteSelection(-1);
                e.Handled = true;
                break;
        }
    }

    private void OnPaletteScrimClick(object sender, MouseButtonEventArgs e)
    {
        _viewModel.ClosePalette();
        e.Handled = true;
    }

    private void OnPaletteCardClick(object sender, MouseButtonEventArgs e)
    {
        // Clicks inside the palette must not reach the scrim's dismiss handler.
        e.Handled = true;
    }

    private async void OnClosing(object? sender, CancelEventArgs e)
    {
        if (_shutdownFinished)
        {
            return;
        }

        // Shutdown stops a backend process this window started; that must
        // finish before the process exits, so the close is deferred once.
        e.Cancel = true;
        if (_shutdownStarted)
        {
            return;
        }

        _shutdownStarted = true;
        _session.Settings.WindowWidth = WindowState == WindowState.Maximized
            ? RestoreBounds.Width : ActualWidth;
        _session.Settings.WindowHeight = WindowState == WindowState.Maximized
            ? RestoreBounds.Height : ActualHeight;

        try
        {
            await _viewModel.ShutdownAsync();
        }
        catch (Exception ex)
        {
            _session.Log.Warn("shutdown failed: " + ex.Message);
        }
        finally
        {
            _shutdownFinished = true;
            // Close() re-entered from inside a cancelled Closing handler is
            // ignored by WPF, which left the window (and the backend it owns)
            // alive. Shutdown is the supported way to finish after async work.
            Application.Current.Shutdown();
        }
    }
}
