using System.Windows;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;

namespace MathChecker.Desktop;

public partial class App : Application
{
    private IFileLog? _log;
    private BackendHost? _host;
    private MathCheckerApiClient? _api;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // A double-click while the app is open must not start a second client
        // (and a second backend): raise the existing window and leave.
        if (!AppInstance.TryAcquire())
        {
            Shutdown();
            return;
        }

        // Machines without a usable GPU (some VMs and remote sessions) can show
        // an unpainted window under hardware acceleration. This is an opt-in
        // fallback for those machines, not the default.
        if (Environment.GetEnvironmentVariable("MC5_SOFTWARE_RENDER") == "1")
        {
            System.Windows.Media.RenderOptions.ProcessRenderMode =
                System.Windows.Interop.RenderMode.SoftwareOnly;
        }

        var settings = DesktopSettings.Load();
        _log = new FileLog();
        _log.Info($"desktop starting (base: {AppContext.BaseDirectory})");

        DispatcherUnhandledException += OnDispatcherUnhandledException;
        AppDomain.CurrentDomain.UnhandledException += (_, args) =>
            _log?.Error("unhandled domain exception", args.ExceptionObject as Exception);

        _host = new BackendHost(settings, _log);
        _api = new MathCheckerApiClient();
        var session = new AppSession(settings, _log, _host, _api, new DialogService());

        var window = new MainWindow(session);
        MainWindow = window;
        window.Show();
    }

    private void OnDispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        _log?.Error("unhandled UI exception", e.Exception);
        MessageBox.Show(
            "Math Checker hit an unexpected error:\n\n" + e.Exception.Message
            + "\n\nDetails were written to the desktop log.",
            "Math Checker", MessageBoxButton.OK, MessageBoxImage.Error);
        e.Handled = true;
    }

    protected override void OnExit(ExitEventArgs e)
    {
        try
        {
            // The window already stopped the backend it owns; this is a safety
            // net for an exit that bypassed it (for example Alt+F4 during start).
            _host?.Dispose();
        }
        catch (Exception ex)
        {
            _log?.Warn("shutdown cleanup failed: " + ex.Message);
        }

        _api?.Dispose();
        AppInstance.Release();
        _log?.Info("desktop exited");
        base.OnExit(e);
    }
}
