using System.Text;

namespace MathChecker.Desktop.Core.Diagnostics;

public interface IFileLog
{
    string LogPath { get; }
    void Info(string message);
    void Warn(string message);
    void Error(string message, Exception? exception = null);
    IReadOnlyList<string> Tail(int lines);
}

/// <summary>
/// Daily rolling text log under <c>%LOCALAPPDATA%\MathCheckerDesktop\logs</c>.
/// Every line is redacted before it is written. A logging failure never
/// propagates into the application: diagnostics are a convenience.
/// </summary>
public sealed class FileLog : IFileLog
{
    private readonly object _gate = new();
    private readonly string _directory;
    private readonly int _retainDays;
    private string _currentDate = DateTime.Now.ToString("yyyyMMdd");

    public FileLog(string? directory = null, int retainDays = 14)
    {
        _directory = directory ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "MathCheckerDesktop", "logs");
        _retainDays = retainDays;
        try
        {
            Directory.CreateDirectory(_directory);
            Prune();
        }
        catch (Exception)
        {
            // Read-only profile: logging becomes a no-op rather than a crash.
        }
    }

    public string LogPath => Path.Combine(_directory, $"desktop-{_currentDate}.log");

    public void Info(string message) => Write("INFO", message);

    public void Warn(string message) => Write("WARN", message);

    public void Error(string message, Exception? exception = null) =>
        Write("ERROR", exception is null ? message : $"{message} :: {exception.GetType().Name}: {exception.Message}");

    public IReadOnlyList<string> Tail(int lines)
    {
        try
        {
            if (!File.Exists(LogPath))
            {
                return Array.Empty<string>();
            }

            return File.ReadLines(LogPath).TakeLast(Math.Max(1, lines)).ToArray();
        }
        catch (Exception)
        {
            return Array.Empty<string>();
        }
    }

    private void Write(string level, string message)
    {
        var line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} {level,-5} {SecretRedactor.Redact(message)}";
        lock (_gate)
        {
            try
            {
                if (DateTime.Now.ToString("yyyyMMdd") != _currentDate)
                {
                    _currentDate = DateTime.Now.ToString("yyyyMMdd");
                    Prune();
                }

                File.AppendAllText(LogPath, line + Environment.NewLine, Encoding.UTF8);
            }
            catch (Exception)
            {
                // Never let diagnostics break the app.
            }
        }
    }

    private void Prune()
    {
        try
        {
            var cutoff = DateTime.Now.AddDays(-_retainDays);
            foreach (var file in Directory.EnumerateFiles(_directory, "desktop-*.log"))
            {
                if (File.GetLastWriteTime(file) < cutoff)
                {
                    File.Delete(file);
                }
            }
        }
        catch (Exception)
        {
            // Best effort.
        }
    }
}

/// <summary>In-memory log used by tests and by the design-time view models.</summary>
public sealed class MemoryLog : IFileLog
{
    private readonly List<string> _lines = new();
    public string LogPath => "(memory)";

    public void Info(string message) => Add("INFO", message);
    public void Warn(string message) => Add("WARN", message);
    public void Error(string message, Exception? exception = null) =>
        Add("ERROR", exception is null ? message : $"{message} :: {exception.Message}");

    public IReadOnlyList<string> Tail(int lines) => _lines.TakeLast(lines).ToArray();
    public IReadOnlyList<string> All => _lines;

    private void Add(string level, string message)
    {
        lock (_lines)
        {
            _lines.Add($"{level} {SecretRedactor.Redact(message)}");
        }
    }
}
