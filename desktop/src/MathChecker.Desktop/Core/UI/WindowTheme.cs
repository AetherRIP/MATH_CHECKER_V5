using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// WPF keeps the native frame, so the title bar is painted by DWM. Both the
/// current and pre-20H1 attribute numbers are tried; failure is harmless.
/// The title bar follows ThemeManager, so a light shell never keeps a
/// black caption and a dark shell never keeps a white one.
/// </summary>
public static class WindowTheme
{
    private const int UseImmersiveDarkMode = 20;
    private const int UseImmersiveDarkModeBefore20H1 = 19;

    public static void ApplyTitleBar(Window window)
    {
        if (new WindowInteropHelper(window).Handle != IntPtr.Zero)
        {
            Apply(window);
            return;
        }

        window.SourceInitialized += (_, _) => Apply(window);
    }

    /// <summary>Repaint the caption after a live appearance switch.</summary>
    public static void Refresh(Window window) => Apply(window);

    private static void Apply(Window window)
    {
        try
        {
            var handle = new WindowInteropHelper(window).Handle;
            if (handle == IntPtr.Zero)
            {
                return;
            }

            var enabled = ThemeManager.Current == AppAppearance.Dark ? 1 : 0;
            if (DwmSetWindowAttribute(handle, UseImmersiveDarkMode, ref enabled,
                    sizeof(int)) != 0)
            {
                DwmSetWindowAttribute(handle, UseImmersiveDarkModeBefore20H1, ref enabled,
                    sizeof(int));
            }
        }
        catch (DllNotFoundException)
        {
            // Not running on Windows with DWM; the window is still usable.
        }
        catch (EntryPointNotFoundException)
        {
        }
    }

    [DllImport("dwmapi.dll", PreserveSig = true)]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute,
        ref int value, int size);
}
