namespace MathChecker.Desktop.Core.Backend;

/// <summary>Everything needed to start the existing Python backend.</summary>
public sealed record BackendLaunchPlan(
    string BackendDirectory,
    string Executable,
    IReadOnlyList<string> Arguments,
    string? DataDirectory,
    int Port,
    IReadOnlyDictionary<string, string>? EnvironmentVariables = null)
{
    public string Describe() =>
        $"{Executable} {string.Join(' ', Arguments)}  (cwd: {BackendDirectory})";
}

public sealed record PythonLaunch(string Executable, IReadOnlyList<string> PrefixArguments)
{
    public string Describe() => PrefixArguments.Count == 0
        ? Executable
        : $"{Executable} {string.Join(' ', PrefixArguments)}";
}

/// <summary>
/// Finds the V5 backend and a Python interpreter without assuming a developer
/// environment. Order for the backend folder: explicit setting, the folder the
/// executable sits in (installed layout), then ancestors of both the executable
/// and the current directory (a source checkout).
/// </summary>
public static class BackendLocator
{
    public static bool LooksLikeBackend(string? directory)
    {
        if (string.IsNullOrWhiteSpace(directory))
        {
            return false;
        }

        try
        {
            return File.Exists(Path.Combine(directory, "main.py"))
                   && Directory.Exists(Path.Combine(directory, "backend", "app"));
        }
        catch (Exception)
        {
            return false;
        }
    }

    public static string? FindBackendDirectory(string? configured, string appBaseDirectory,
        string? currentDirectory = null)
    {
        if (!string.IsNullOrWhiteSpace(configured))
        {
            var expanded = Environment.ExpandEnvironmentVariables(configured.Trim());
            if (LooksLikeBackend(expanded))
            {
                return Path.GetFullPath(expanded);
            }

            // A stale setting must not make the app unusable: fall through to
            // discovery, and the status line still names the folder it found.
        }

        foreach (var start in new[] { appBaseDirectory, currentDirectory ?? Environment.CurrentDirectory })
        {
            if (string.IsNullOrWhiteSpace(start))
            {
                continue;
            }

            var directory = new DirectoryInfo(Path.GetFullPath(start));
            for (var depth = 0; directory is not null && depth < 8; depth++, directory = directory.Parent)
            {
                if (LooksLikeBackend(directory.FullName))
                {
                    return directory.FullName;
                }
            }
        }

        return null;
    }

    /// <summary>
    /// Picks the interpreter: explicit setting, bundled runtime next to the
    /// backend, the checkout's virtual environment, then PATH.
    /// </summary>
    public static PythonLaunch? ResolvePython(string? configured, string backendDirectory)
    {
        if (!string.IsNullOrWhiteSpace(configured))
        {
            var expanded = Environment.ExpandEnvironmentVariables(configured.Trim());
            var full = Path.IsPathRooted(expanded) ? expanded : Path.Combine(backendDirectory, expanded);
            if (File.Exists(full))
            {
                return new PythonLaunch(Path.GetFullPath(full), Array.Empty<string>());
            }

            // An explicit "py -3" style command: keep the word, let PATH resolve it.
            if (!expanded.Contains(Path.DirectorySeparatorChar) && !expanded.Contains('/'))
            {
                return new PythonLaunch(expanded, Array.Empty<string>());
            }
        }

        foreach (var candidate in new[]
                 {
                     Path.Combine(backendDirectory, "runtime", "python.exe"),
                     Path.Combine(backendDirectory, ".venv", "Scripts", "python.exe"),
                     Path.Combine(backendDirectory, ".venv", "bin", "python"),
                     Path.Combine(backendDirectory, "venv", "Scripts", "python.exe"),
                 })
        {
            if (File.Exists(candidate))
            {
                return new PythonLaunch(Path.GetFullPath(candidate), Array.Empty<string>());
            }
        }

        var py = FindOnPath("py.exe") ?? FindOnPath("py");
        if (py is not null)
        {
            return new PythonLaunch(py, new[] { "-3" });
        }

        var python = FindOnPath("python.exe") ?? FindOnPath("python");
        return python is null ? null : new PythonLaunch(python, Array.Empty<string>());
    }

    public static string? FindOnPath(string executable)
    {
        var path = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(path))
        {
            return null;
        }

        foreach (var folder in path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                var candidate = Path.Combine(folder.Trim('"'), executable);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch (Exception)
            {
                // Malformed PATH entry: keep looking.
            }
        }

        return null;
    }

    public static BackendLaunchPlan BuildPlan(string backendDirectory, PythonLaunch python,
        int port, string? dataDirectory,
        IReadOnlyDictionary<string, string>? environmentVariables = null)
    {
        var arguments = new List<string>(python.PrefixArguments)
        {
            "main.py",
            "--serve",
            port.ToString(),
        };

        return new BackendLaunchPlan(
            backendDirectory,
            python.Executable,
            arguments,
            string.IsNullOrWhiteSpace(dataDirectory) ? null : Environment.ExpandEnvironmentVariables(dataDirectory.Trim()),
            port,
            environmentVariables);
    }
}
