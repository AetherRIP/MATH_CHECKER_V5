using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Text.Json;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;

namespace MathChecker.Desktop.Core.Backend;

public enum BackendState
{
    Stopped,
    Starting,
    Ready,
    Attached,
    Failed,
    Stopping,
}

/// <summary>What the shell shows in the status bar.</summary>
/// <param name="Demo">True when the backend that answered reports demo mode.
/// null = the backend did not answer the environment probe.</param>
/// <param name="Warning">A visible environment mismatch (demo vs real data,
/// or a different data folder than configured). Never only a log line.</param>
public sealed record BackendStatus(
    BackendState State,
    int Port,
    bool Owned,
    string Detail,
    string? DataDirectory = null,
    bool? Demo = null,
    string? Warning = null)
{
    public bool IsUsable => State is BackendState.Ready or BackendState.Attached;

    public bool HasWarning => !string.IsNullOrWhiteSpace(Warning);

    public string Summary => State switch
    {
        BackendState.Stopped => "Backend stopped",
        BackendState.Starting => "Starting backend…",
        BackendState.Ready => $"Backend ready on port {Port}",
        BackendState.Attached => $"Attached to a running backend on port {Port}",
        BackendState.Stopping => "Stopping backend…",
        BackendState.Failed => "Backend unavailable",
        _ => State.ToString(),
    };
}

public enum HealthKind
{
    /// <summary>Nothing answers on the port.</summary>
    Free,
    /// <summary>Math Checker answered /api/health with status ok.</summary>
    Ours,
    /// <summary>Something answers, but it is not this application.</summary>
    Foreign,
}

public readonly record struct HealthResult(HealthKind Kind, string? Detail);

public interface IHealthProbe
{
    Task<HealthResult> CheckAsync(int port, CancellationToken cancellationToken);
}

public interface IProcessStarter
{
    IProcessHandle Start(BackendLaunchPlan plan);
}

/// <summary>What a running backend says about itself: its data folder and
/// whether it is the demo process. Read, never guessed.</summary>
public sealed record BackendEnvironment(string? DataDirectory, bool Demo, bool MockMode);

public interface IBackendEnvironmentProbe
{
    Task<BackendEnvironment?> ReadAsync(int port, CancellationToken cancellationToken);
}

/// <summary>Reads /api/privacy/info: a local route that already exposes the
/// redacted data folder, plus two booleans saying which environment answered.
/// Unavailable information is null, never an assumption.</summary>
public sealed class HttpBackendEnvironmentProbe : IBackendEnvironmentProbe
{
    public async Task<BackendEnvironment?> ReadAsync(int port, CancellationToken cancellationToken)
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            using var response = await client.GetAsync(
                $"http://127.0.0.1:{port}/api/privacy/info", cancellationToken).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                return null;
            }

            var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            var folder = root.TryGetProperty("data_directory", out var value)
                ? value.GetString()
                : null;

            var demo = false;
            var mock = false;
            if (root.TryGetProperty("environment", out var environment)
                && environment.ValueKind == JsonValueKind.Object)
            {
                demo = environment.TryGetProperty("demo", out var d) && d.ValueKind == JsonValueKind.True;
                mock = environment.TryGetProperty("mock_mode", out var m) && m.ValueKind == JsonValueKind.True;
            }

            return new BackendEnvironment(folder, demo, mock);
        }
        catch (Exception)
        {
            return null;
        }
    }
}

public interface IProcessHandle : IDisposable
{
    bool HasExited { get; }
    int ExitCode { get; }
    string Tail { get; }
    Task WaitForExitAsync(CancellationToken cancellationToken);

    /// <summary>Stops the process tree this handle represents. Only ever called
    /// for a process this application started.</summary>
    void StopTree();
}

/// <summary>
/// Owns the lifecycle of the existing Python backend: locate it, decide whether
/// to attach to an already-running instance or start one, wait for readiness,
/// keep it alive while the window is open, and stop only what it started.
/// </summary>
public sealed class BackendHost : IAsyncDisposable
{
    private readonly DesktopSettings _settings;
    private readonly IFileLog _log;
    private readonly IProcessStarter _starter;
    private readonly IHealthProbe _health;
    private readonly IBackendEnvironmentProbe _environment;
    private readonly string _appBaseDirectory;
    private readonly TimeSpan _heartbeatInterval;
    private readonly string _sessionId = "desktop-" + Guid.NewGuid().ToString("N");

    private IProcessHandle? _process;
    private CancellationTokenSource? _monitorCts;
    private Task? _monitorTask;
    private Task? _heartbeatTask;
    private CancellationTokenSource? _heartbeatCts;
    private HttpClient? _client;

    public BackendHost(DesktopSettings settings, IFileLog log,
        IProcessStarter? starter = null, IHealthProbe? health = null,
        string? appBaseDirectory = null, TimeSpan? heartbeatInterval = null,
        IBackendEnvironmentProbe? environment = null)
    {
        _settings = settings;
        _log = log;
        _starter = starter ?? new ProcessStarter();
        _health = health ?? new HttpHealthProbe();
        _environment = environment ?? new HttpBackendEnvironmentProbe();
        _appBaseDirectory = appBaseDirectory ?? AppContext.BaseDirectory;
        _heartbeatInterval = heartbeatInterval ?? TimeSpan.FromSeconds(20);
    }

    public BackendStatus Status { get; private set; } = new(BackendState.Stopped, 0, false, "Not started");

    public event Action<BackendStatus>? StatusChanged;

    public string BaseUrl => $"http://127.0.0.1:{Status.Port}";

    /// <summary>Starts or attaches. Never throws for an expected failure: the
    /// returned status carries the reason.</summary>
    public async Task<BackendStatus> StartAsync(CancellationToken cancellationToken = default)
    {
        if (Status.IsUsable)
        {
            return Status;
        }

        var configuredPort = _settings.Port > 0 ? _settings.Port : 8642;
        SetStatus(new BackendStatus(BackendState.Starting, configuredPort, false, "Checking port…"));

        var probe = await _health.CheckAsync(configuredPort, cancellationToken).ConfigureAwait(false);
        var port = configuredPort;

        if (probe.Kind == HealthKind.Ours)
        {
            if (_settings.AttachToRunningBackend)
            {
                // A loopback port is machine-wide, not per Windows user, so
                // "Math Checker answered" is not the same as "MY backend
                // answered". Only attach when the running backend reports the
                // same data folder this launch would use; otherwise a second
                // Windows account (or a development checkout) would be shown
                // as if it were this user's class - and edits would land in
                // someone else's database.
                var running = await _environment.ReadAsync(port, cancellationToken).ConfigureAwait(false);
                if (CanAttachTo(running, _settings))
                {
                    await AttachAsync(port, cancellationToken).ConfigureAwait(false);
                    return Status;
                }

                var ownPort = FindFreePort(configuredPort + 1);
                if (ownPort == 0)
                {
                    SetStatus(new BackendStatus(BackendState.Failed, port, false,
                        "Another Math Checker backend is using a different data folder and no free "
                        + "port was found to start this installation's own backend.",
                        running?.DataDirectory));
                    return Status;
                }

                _log.Warn($"not attaching: the backend on {port} serves {running?.DataDirectory ?? "another data folder"}"
                          + $"; starting this installation's own backend on {ownPort}");
                port = ownPort;
            }
            else
            {
                SetStatus(new BackendStatus(BackendState.Failed, port, false,
                    "A Math Checker backend is already running on this port. Close it first, or enable "
                    + "\"attach to a running backend\" in the desktop settings."));
                return Status;
            }
        }

        if (probe.Kind == HealthKind.Foreign)
        {
            var alternative = FindFreePort(configuredPort + 1);
            if (alternative == 0)
            {
                SetStatus(new BackendStatus(BackendState.Failed, configuredPort, false,
                    $"Port {configuredPort} is used by another program and no free port was found nearby."));
                return Status;
            }

            _log.Warn($"port {configuredPort} is used by another program; using {alternative}");
            port = alternative;
        }

        if (!_settings.StartBackendAutomatically)
        {
            SetStatus(new BackendStatus(BackendState.Failed, port, false,
                "No backend is running and automatic start is disabled in the desktop settings."));
            return Status;
        }

        var backendDirectory = BackendLocator.FindBackendDirectory(
            _settings.BackendDirectory, _appBaseDirectory);
        if (backendDirectory is null)
        {
            SetStatus(new BackendStatus(BackendState.Failed, port, false,
                "The Math Checker backend was not found. Set its folder (the one containing main.py) "
                + "in File → Backend settings. This build looks next to the executable and in the "
                + "source checkout's parent folders."));
            return Status;
        }

        var python = BackendLocator.ResolvePython(_settings.PythonExecutable, backendDirectory);
        if (python is null)
        {
            SetStatus(new BackendStatus(BackendState.Failed, port, false,
                $"No Python interpreter was found for {backendDirectory}. Install Python 3.12+, or point "
                + "the desktop settings at a python.exe (a bundled runtime or the project's .venv)."));
            return Status;
        }

        var launch = BackendLocator.BuildPlan(backendDirectory, python, port, _settings.DataDirectory,
            _settings.EnvironmentVariables);

        SetStatus(new BackendStatus(BackendState.Starting, port, true,
            $"Starting backend on port {port}…"));
        _log.Info($"starting backend: {launch.Describe()}");

        try
        {
            _process = _starter.Start(launch);
        }
        catch (Exception ex)
        {
            _log.Error("backend could not be started", ex);
            SetStatus(new BackendStatus(BackendState.Failed, port, false,
                $"The backend process could not be started: {ex.Message}"));
            return Status;
        }

        var ready = await WaitForReadyAsync(port, _settings.StartupTimeoutSeconds, cancellationToken)
            .ConfigureAwait(false);
        if (ready is not null)
        {
            SetStatus(new BackendStatus(BackendState.Failed, port, true, ready));
            await StopOwnedProcessAsync().ConfigureAwait(false);
            return Status;
        }

        await BecomeReadyAsync(port, owned: true, cancellationToken).ConfigureAwait(false);
        StartMonitoring();
        StartHeartbeat();
        return Status;
    }

    private async Task AttachAsync(int port, CancellationToken cancellationToken)
    {
        _log.Info($"attaching to an existing backend on port {port}");
        await BecomeReadyAsync(port, owned: false, cancellationToken).ConfigureAwait(false);
        StartHeartbeat();
    }

    private async Task BecomeReadyAsync(int port, bool owned, CancellationToken cancellationToken)
    {
        var environment = await _environment.ReadAsync(port, cancellationToken).ConfigureAwait(false);
        var warning = DescribeEnvironmentMismatch(environment, _settings);
        if (warning is not null)
        {
            _log.Warn("backend environment mismatch: " + warning);
        }

        SetStatus(new BackendStatus(owned ? BackendState.Ready : BackendState.Attached, port, owned,
            owned ? "Backend started" : "Attached to a running backend",
            environment?.DataDirectory, environment?.Demo, warning));
    }

    /// <summary>
    /// The desktop must never look like it is showing the teacher's class when
    /// it is not. Two things are checked against what the desktop ASKED for:
    /// demo vs real data, and the data folder the backend reports.
    /// Returns null when everything matches (or when the backend cannot say).
    /// </summary>
    internal static string? DescribeEnvironmentMismatch(
        BackendEnvironment? environment, DesktopSettings settings)
    {
        if (environment is null)
        {
            return null;
        }

        var wantsDemo = WantsDemo(settings);

        var warnings = new List<string>();
        if (environment.Demo && !wantsDemo)
        {
            warnings.Add("This backend is running in DEMO mode: every student, assignment and "
                         + "result shown is synthetic demo data, not your class. Nothing you see "
                         + "here is real student work.");
        }
        else if (!environment.Demo && wantsDemo)
        {
            warnings.Add("The desktop is configured to start DEMO data, but this backend is "
                         + "serving real data. Nothing has been changed; check the demo setting "
                         + "before editing anything.");
        }

        var expected = ExpectedDataDirectory(settings, wantsDemo);
        if (expected is not null && environment.DataDirectory is not null
            && !SameFolder(expected, environment.DataDirectory))
        {
            warnings.Add($"This backend is using a different data folder ({environment.DataDirectory}) "
                         + $"than the one configured here ({expected}).");
        }

        return warnings.Count == 0 ? null : string.Join(" ", warnings);
    }

    /// <summary>
    /// May this launch attach to the backend already answering on the port?
    ///
    /// Only when it serves the data folder this installation would use. A
    /// backend that cannot say (null) is attached to as before: the check must
    /// not turn a missing diagnostic route into a startup failure. A DIFFERENT
    /// folder is refused, because the alternative is showing - and writing to -
    /// another Windows user's (or the developer's) classes.
    /// </summary>
    internal static bool CanAttachTo(BackendEnvironment? running, DesktopSettings settings)
    {
        if (running?.DataDirectory is null || string.IsNullOrWhiteSpace(running.DataDirectory))
        {
            return true;
        }

        // A demo backend is synthetic by construction: attaching shows it with
        // the existing "this is not your class" warning and cannot expose real
        // student data, so the folder rule does not apply to it.
        if (running.Demo)
        {
            return true;
        }

        var wantsDemo = WantsDemo(settings);
        var expected = ExpectedDataDirectory(settings, wantsDemo)
                       ?? DefaultUserDataDirectory(wantsDemo);
        return SameFolder(expected, running.DataDirectory);
    }

    /// <summary>Where this launch keeps its data when nothing is configured.
    /// Mirrors backend/app/data_location.py for an installed copy: the
    /// per-user folder, never the installation directory.</summary>
    internal static string DefaultUserDataDirectory(bool demo)
    {
        var baseDirectory = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "MathChecker");
        return demo ? Path.Combine(baseDirectory, "demo") : baseDirectory;
    }

    private static bool WantsDemo(DesktopSettings settings) =>
        (settings.EnvironmentVariables ?? new Dictionary<string, string>())
        .Any(kv =>
            string.Equals(kv.Key, "MC5_DEMO", StringComparison.OrdinalIgnoreCase)
            && string.Equals(kv.Value?.Trim(), "1", StringComparison.Ordinal));

    /// <summary>The folder this launch should have produced, mirroring
    /// main.py --demo (which appends \"demo\" to the base folder once).</summary>
    private static string? ExpectedDataDirectory(DesktopSettings settings, bool wantsDemo)
    {
        if (string.IsNullOrWhiteSpace(settings.DataDirectory))
        {
            return null;
        }

        try
        {
            var expected = Path.GetFullPath(
                Environment.ExpandEnvironmentVariables(settings.DataDirectory.Trim()));
            if (wantsDemo && !string.Equals(Path.GetFileName(expected.TrimEnd(
                    Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)),
                    "demo", StringComparison.OrdinalIgnoreCase))
            {
                expected = Path.Combine(expected, "demo");
            }

            return expected;
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>Compares folders after expanding the backend's redacted
    /// %USERPROFILE% prefix, so redaction does not read as a mismatch.</summary>
    internal static bool SameFolder(string expected, string reported)
    {
        static string Normalize(string path) => Path.GetFullPath(
                Environment.ExpandEnvironmentVariables(path.Trim()))
            .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);

        try
        {
            return string.Equals(Normalize(expected), Normalize(reported),
                OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal);
        }
        catch (Exception)
        {
            return false;
        }
    }

    /// <summary>Returns null when ready, otherwise the failure detail.</summary>
    private async Task<string?> WaitForReadyAsync(int port, int timeoutSeconds,
        CancellationToken cancellationToken)
    {
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        while (DateTime.UtcNow < deadline)
        {
            cancellationToken.ThrowIfCancellationRequested();

            if (_process is { HasExited: true })
            {
                var tail = LastLines(_process.Tail, 12);
                return $"The backend exited during startup (exit code {_process.ExitCode})."
                       + (string.IsNullOrWhiteSpace(tail) ? string.Empty : Environment.NewLine + tail);
            }

            var probe = await _health.CheckAsync(port, cancellationToken).ConfigureAwait(false);
            if (probe.Kind == HealthKind.Ours)
            {
                return null;
            }

            await Task.Delay(300, cancellationToken).ConfigureAwait(false);
        }

        var tailAtTimeout = LastLines(_process?.Tail, 12);
        return $"The backend did not answer /api/health within {timeoutSeconds} s."
               + (string.IsNullOrWhiteSpace(tailAtTimeout) ? string.Empty : Environment.NewLine + tailAtTimeout);
    }

    private void StartMonitoring()
    {
        if (_process is null)
        {
            return;
        }

        _monitorCts = new CancellationTokenSource();
        var token = _monitorCts.Token;
        _monitorTask = Task.Run(async () =>
        {
            try
            {
                await _process.WaitForExitAsync(token).ConfigureAwait(false);
                if (token.IsCancellationRequested || Status.State is BackendState.Stopping or BackendState.Stopped)
                {
                    return;
                }

                var tail = LastLines(_process.Tail, 12);
                _log.Error($"backend exited unexpectedly with code {_process.ExitCode}");
                SetStatus(new BackendStatus(BackendState.Failed, Status.Port, true,
                    $"The backend stopped unexpectedly (exit code {_process.ExitCode})."
                    + (string.IsNullOrWhiteSpace(tail) ? string.Empty : Environment.NewLine + tail)));
            }
            catch (OperationCanceledException)
            {
                // Expected on shutdown.
            }
        }, token);
    }

    private void StartHeartbeat()
    {
        _client?.Dispose();
        _client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        var client = _client;
        var port = Status.Port;
        _heartbeatCts?.Cancel();
        _heartbeatCts?.Dispose();
        _heartbeatCts = new CancellationTokenSource();
        var token = _heartbeatCts.Token;
        _heartbeatTask = Task.Run(async () =>
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    await Task.Delay(_heartbeatInterval, token).ConfigureAwait(false);
                    if (!Status.IsUsable)
                    {
                        break;
                    }

                    using var response = await client.GetAsync(
                        $"http://127.0.0.1:{port}/api/lifecycle/heartbeat?sid={_sessionId}",
                        token).ConfigureAwait(false);
                    if (!response.IsSuccessStatusCode)
                    {
                        _log.Warn($"lifecycle heartbeat returned {(int)response.StatusCode}");
                    }
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    _log.Warn("lifecycle heartbeat failed: " + ex.Message);
                    try
                    {
                        await Task.Delay(TimeSpan.FromSeconds(5), token).ConfigureAwait(false);
                    }
                    catch (OperationCanceledException)
                    {
                        break;
                    }
                }
            }
        }, token);
    }

    public async Task StopAsync()
    {
        if (Status.State is BackendState.Stopped or BackendState.Failed)
        {
            Dispose();
            return;
        }

        var owned = Status.Owned;
        var port = Status.Port;
        SetStatus(Status with { State = BackendState.Stopping, Detail = "Stopping" });
        try
        {
            if (_client is not null && port > 0)
            {
                using var goodbye = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                await _client.GetAsync(
                    $"http://127.0.0.1:{port}/api/lifecycle/leave?sid={_sessionId}",
                    goodbye.Token).ConfigureAwait(false);
            }
        }
        catch (Exception)
        {
            // The backend may already be gone.
        }

        _heartbeatCts?.Cancel();
        _monitorCts?.Cancel();

        if (owned)
        {
            await StopOwnedProcessAsync().ConfigureAwait(false);
        }

        _client?.Dispose();
        _client = null;
        SetStatus(new BackendStatus(BackendState.Stopped, port, false, "Stopped", Status.DataDirectory));
    }

    private async Task StopOwnedProcessAsync()
    {
        var process = _process;
        _process = null;
        if (process is null)
        {
            return;
        }

        try
        {
            if (!process.HasExited)
            {
                _log.Info("stopping the backend process tree started by this window");
                process.StopTree();
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
            }
        }
        catch (Exception ex)
        {
            _log.Warn("backend stop failed: " + ex.Message);
        }
        finally
        {
            process.Dispose();
        }
    }

    private void SetStatus(BackendStatus status)
    {
        Status = status;
        StatusChanged?.Invoke(status);
    }

    public static int FindFreePort(int start)
    {
        for (var port = Math.Max(1, start); port <= Math.Min(65535, start + 40); port++)
        {
            try
            {
                using var listener = new TcpListener(IPAddress.Loopback, port);
                listener.Start();
                listener.Stop();
                return port;
            }
            catch (SocketException)
            {
                // Taken.
            }
        }

        return 0;
    }

    private static string LastLines(string? text, int count)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return string.Empty;
        }

        var lines = text.Replace("\r\n", "\n").Split('\n', StringSplitOptions.RemoveEmptyEntries);
        return string.Join(Environment.NewLine, lines.TakeLast(count));
    }

    public void Dispose()
    {
        _heartbeatCts?.Cancel();
        _heartbeatCts?.Dispose();
        _heartbeatCts = null;
        _monitorCts?.Cancel();
        _monitorCts?.Dispose();
        _monitorCts = null;
        _client?.Dispose();
        _client = null;
        _process?.Dispose();
        _process = null;
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync().ConfigureAwait(false);
        Dispose();
    }
}

/// <summary>Real OS process, with redirected output collected into a tail buffer.</summary>
public sealed class ProcessStarter : IProcessStarter
{
    public IProcessHandle Start(BackendLaunchPlan plan)
    {
        var info = new ProcessStartInfo
        {
            FileName = plan.Executable,
            WorkingDirectory = plan.BackendDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };

        foreach (var argument in plan.Arguments)
        {
            info.ArgumentList.Add(argument);
        }

        info.Environment["MC5_NO_BROWSER"] = "1";
        info.Environment["MC5_SUPERVISED"] = "1";
        info.Environment["MC5_PORT"] = plan.Port.ToString();
        info.Environment["PYTHONUNBUFFERED"] = "1";
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        if (!string.IsNullOrWhiteSpace(plan.DataDirectory))
        {
            info.Environment["MC5_DATA_DIR"] = plan.DataDirectory!;
        }

        if (plan.EnvironmentVariables is not null)
        {
            foreach (var (key, value) in plan.EnvironmentVariables)
            {
                if (!string.IsNullOrWhiteSpace(key))
                {
                    info.Environment[key] = value ?? string.Empty;
                }
            }
        }

        var process = Process.Start(info)
                      ?? throw new InvalidOperationException("Process.Start returned no process.");

        return new ProcessHandle(process);
    }

    private sealed class ProcessHandle : IProcessHandle
    {
        private readonly Process _process;
        private readonly Queue<string> _tail = new();
        private readonly object _gate = new();

        public ProcessHandle(Process process)
        {
            _process = process;
            _process.OutputDataReceived += (_, e) => Append(e.Data);
            _process.ErrorDataReceived += (_, e) => Append(e.Data);
            _process.BeginOutputReadLine();
            _process.BeginErrorReadLine();
        }

        private void Append(string? line)
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                return;
            }

            lock (_gate)
            {
                _tail.Enqueue(SecretRedactor.Redact(line));
                while (_tail.Count > 200)
                {
                    _tail.Dequeue();
                }
            }
        }

        public bool HasExited
        {
            get
            {
                try
                {
                    return _process.HasExited;
                }
                catch (InvalidOperationException)
                {
                    return true;
                }
            }
        }

        public int ExitCode
        {
            get
            {
                try
                {
                    return _process.ExitCode;
                }
                catch (InvalidOperationException)
                {
                    return -1;
                }
            }
        }

        public string Tail
        {
            get
            {
                lock (_gate)
                {
                    return string.Join(Environment.NewLine, _tail);
                }
            }
        }

        public Task WaitForExitAsync(CancellationToken cancellationToken) =>
            _process.WaitForExitAsync(cancellationToken);

        public void StopTree()
        {
            try
            {
                _process.Kill(entireProcessTree: true);
            }
            catch (Exception)
            {
                // Already gone.
            }
        }

        public void Dispose() => _process.Dispose();
    }
}

/// <summary>HTTP probe that distinguishes "ours" from "something else".</summary>
public sealed class HttpHealthProbe : IHealthProbe
{
    private readonly HttpClient _client;

    public HttpHealthProbe(HttpClient? client = null)
    {
        _client = client ?? new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
    }

    public async Task<HealthResult> CheckAsync(int port, CancellationToken cancellationToken)
    {
        try
        {
            using var response = await _client.GetAsync(
                $"http://127.0.0.1:{port}/api/health", cancellationToken).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                return new HealthResult(HealthKind.Foreign,
                    $"port {port} answered HTTP {(int)response.StatusCode} but not /api/health");
            }

            var body = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            return body.Contains("\"ok\"", StringComparison.OrdinalIgnoreCase)
                ? new HealthResult(HealthKind.Ours, null)
                : new HealthResult(HealthKind.Foreign, $"port {port} answered HTTP but is not Math Checker");
        }
        catch (HttpRequestException)
        {
            return new HealthResult(HealthKind.Free, null);
        }
        catch (TaskCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return new HealthResult(HealthKind.Foreign, $"port {port} did not answer within 3 s");
        }
        catch (Exception ex)
        {
            return new HealthResult(HealthKind.Foreign, ex.Message);
        }
    }
}
