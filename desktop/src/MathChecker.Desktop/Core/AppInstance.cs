using System.Diagnostics;
using System.Runtime.InteropServices;

namespace MathChecker.Desktop.Core;

/// <summary>
/// One application window per user session. A second launch (double-clicking the
/// exe again, or a shortcut while the app is open) raises the existing window
/// instead of starting a second client and a second backend.
/// </summary>
public static class AppInstance
{
    private const string MutexName = @"Local\MathChecker.V5.Desktop";
    private static Mutex? _mutex;
    private static bool _owned;

    /// <summary>
    /// Takes the per-session mutex. Returns false when another instance owns it,
    /// after asking that instance's window to come forward.
    /// </summary>
    public static bool TryAcquire()
    {
        var mutex = new Mutex(initiallyOwned: false, MutexName);
        bool owned;
        try
        {
            // Zero timeout: either another instance holds it, or it is free
            // (including the case where the previous owner crashed).
            owned = mutex.WaitOne(0, exitContext: false);
        }
        catch (AbandonedMutexException)
        {
            // The previous process died without releasing; the mutex is ours.
            owned = true;
        }

        if (!owned)
        {
            mutex.Dispose();
            ActivateExistingWindow();
            return false;
        }

        _mutex = mutex;
        _owned = true;
        return true;
    }

    /// <summary>Releases the mutex on exit; a hard kill leaves it abandoned,
    /// which the next launch treats as free.</summary>
    public static void Release()
    {
        if (_mutex is null)
        {
            return;
        }

        if (_owned)
        {
            try
            {
                _mutex.ReleaseMutex();
            }
            catch (ApplicationException)
            {
                // Not the owner any more; nothing to release.
            }
        }

        _mutex.Dispose();
        _mutex = null;
        _owned = false;
    }

    private static void ActivateExistingWindow()
    {
        try
        {
            var current = Process.GetCurrentProcess();
            foreach (var process in Process.GetProcessesByName(current.ProcessName))
            {
                if (process.Id == current.Id)
                {
                    continue;
                }

                var handle = process.MainWindowHandle;
                if (handle == IntPtr.Zero)
                {
                    continue;
                }

                ShowWindow(handle, SwRestore);
                SetForegroundWindow(handle);
                return;
            }
        }
        catch (Exception)
        {
            // Activation is a convenience; never fail the launch over it.
        }
    }

    private const int SwRestore = 9;

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int command);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);
}
