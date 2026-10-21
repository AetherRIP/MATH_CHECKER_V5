using System.Text.Json;
using System.Text.Json.Serialization;

namespace MathChecker.Desktop.Core.Configuration;

/// <summary>
/// Desktop-only preferences. Nothing secret is stored here: the Gemini keys,
/// OAuth material and the application database belong to the existing backend
/// and stay in its data folder. This file holds paths, a port and window state.
/// </summary>
public sealed class DesktopSettings
{
    /// <summary>Folder containing <c>main.py</c> and <c>backend\</c>. Empty = auto-detect.</summary>
    public string? BackendDirectory { get; set; }

    /// <summary>Explicit python.exe. Empty = bundled runtime, then .venv, then PATH.</summary>
    public string? PythonExecutable { get; set; }

    /// <summary>Explicit data folder (passed as MC5_DATA_DIR). Empty = backend default.</summary>
    public string? DataDirectory { get; set; }

    /// <summary>Port the backend is expected on. 0 = pick a free port.</summary>
    public int Port { get; set; } = 8642;

    /// <summary>Start the backend when the app starts.</summary>
    public bool StartBackendAutomatically { get; set; } = true;

    /// <summary>Use an already-running backend instead of starting a second one.</summary>
    public bool AttachToRunningBackend { get; set; } = true;

    /// <summary>How long to wait for /api/health before reporting a failure.</summary>
    public int StartupTimeoutSeconds { get; set; } = 120;

    /// <summary>
    /// Extra environment variables for the backend process (for example
    /// MC5_DEMO=1). Nothing here is a credential store: keys belong to the
    /// backend's own data folder.
    /// </summary>
    public Dictionary<string, string> EnvironmentVariables { get; set; } = new();

    /// <summary>Where downloaded reports/exports default to.</summary>
    public string? LastExportDirectory { get; set; }

    /// <summary>Last selected feature area, restored on start.</summary>
    public string? LastPage { get; set; }

    public double WindowWidth { get; set; } = 1280;
    public double WindowHeight { get; set; } = 820;
    public bool WindowMaximized { get; set; }

    public static string DefaultDirectory => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "MathCheckerDesktop");

    public static string DefaultPath => Path.Combine(DefaultDirectory, "settings.json");

    public static DesktopSettings Load(string? path = null)
    {
        path ??= DefaultPath;
        try
        {
            if (!File.Exists(path))
            {
                return new DesktopSettings();
            }

            var json = File.ReadAllText(path);
            var loaded = JsonSerializer.Deserialize<DesktopSettings>(json, JsonOptions) ?? new DesktopSettings();
            return loaded.Normalize();
        }
        catch (Exception)
        {
            // A corrupt settings file must never stop the application from
            // starting; defaults are safe because nothing is destructive.
            return new DesktopSettings();
        }
    }

    public void Save(string? path = null)
    {
        path ??= DefaultPath;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temp = path + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(this, JsonOptions));
        File.Move(temp, path, overwrite: true);
    }

    public DesktopSettings Normalize()
    {
        Port = Port is >= 1 and <= 65535 ? Port : 8642;
        StartupTimeoutSeconds = Math.Clamp(StartupTimeoutSeconds, 5, 600);
        WindowWidth = Math.Clamp(WindowWidth, 900, 10000);
        WindowHeight = Math.Clamp(WindowHeight, 600, 10000);
        return this;
    }

    public static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };
}
