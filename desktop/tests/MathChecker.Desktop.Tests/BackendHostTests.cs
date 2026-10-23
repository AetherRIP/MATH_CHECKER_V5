using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;

namespace MathChecker.Desktop.Tests;

public class BackendHostTests : IDisposable
{
    private readonly string _backendDirectory;

    public BackendHostTests()
    {
        _backendDirectory = Directory.CreateTempSubdirectory("mc5-backend-").FullName;
        File.WriteAllText(Path.Combine(_backendDirectory, "main.py"), "# stub");
        Directory.CreateDirectory(Path.Combine(_backendDirectory, "backend", "app"));
        Directory.CreateDirectory(Path.Combine(_backendDirectory, "runtime"));
        File.WriteAllText(Path.Combine(_backendDirectory, "runtime", "python.exe"), string.Empty);
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(_backendDirectory, true);
        }
        catch (IOException)
        {
            // Temp cleanup is best effort.
        }
    }

    private DesktopSettings Settings() => new()
    {
        BackendDirectory = _backendDirectory,
        Port = 8123,
        StartupTimeoutSeconds = 20,
        AttachToRunningBackend = true,
    };

    private BackendHost Host(DesktopSettings settings, FakeProcessStarter starter, FakeHealth health,
        MemoryLog? log = null, IBackendEnvironmentProbe? environment = null) =>
        new(settings, log ?? new MemoryLog(), starter, health, _backendDirectory,
            TimeSpan.FromHours(1), environment ?? new FakeEnvironment(null));

    [Fact]
    public async Task Attaches_to_a_running_backend_instead_of_starting_a_second_one()
    {
        var starter = new FakeProcessStarter();
        var health = new FakeHealth(new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        await using var host = Host(settings, starter, health);

        var status = await host.StartAsync();

        Assert.Equal(BackendState.Attached, status.State);
        Assert.False(status.Owned);
        Assert.Empty(starter.Started);
        Assert.True(status.IsUsable);
        Assert.Equal("http://127.0.0.1:8123", host.BaseUrl);
    }

    [Fact]
    public async Task Refuses_to_duplicate_a_running_backend_when_attach_is_disabled()
    {
        var starter = new FakeProcessStarter();
        var health = new FakeHealth(new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        settings.AttachToRunningBackend = false;
        await using var host = Host(settings, starter, health);

        var status = await host.StartAsync();

        Assert.Equal(BackendState.Failed, status.State);
        Assert.Contains("already running", status.Detail);
        Assert.Empty(starter.Started);
    }

    [Fact]
    public async Task Starts_the_backend_and_reports_ready_after_health_answers()
    {
        var starter = new FakeProcessStarter();
        var process = new FakeProcess();
        starter.Next = process;
        var health = new FakeHealth(
            new HealthResult(HealthKind.Free, null),
            new HealthResult(HealthKind.Free, null),
            new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        await using var host = Host(settings, starter, health);

        var status = await host.StartAsync();

        Assert.Equal(BackendState.Ready, status.State);
        Assert.True(status.Owned);
        var plan = Assert.Single(starter.Started);
        Assert.Equal(_backendDirectory, plan.BackendDirectory);
        Assert.Contains("--serve", plan.Arguments);
        Assert.Contains("8123", plan.Arguments);
        Assert.Equal(Path.Combine(_backendDirectory, "runtime", "python.exe"), plan.Executable);
    }

    [Fact]
    public async Task Reports_the_exit_code_and_last_output_when_the_backend_dies_on_start()
    {
        var starter = new FakeProcessStarter { Next = new FakeProcess { Tail = "ModuleNotFoundError: fastapi" } };
        starter.Next.Exit(3);
        var health = new FakeHealth(new HealthResult(HealthKind.Free, null));
        var settings = Settings();
        await using var host = Host(settings, starter, health);

        var status = await host.StartAsync();

        Assert.Equal(BackendState.Failed, status.State);
        Assert.Contains("exit code 3", status.Detail);
        Assert.Contains("ModuleNotFoundError", status.Detail);
    }

    [Fact]
    public async Task Reports_a_timeout_when_the_backend_never_answers()
    {
        var starter = new FakeProcessStarter { Next = new FakeProcess() };
        var health = new FakeHealth(new HealthResult(HealthKind.Free, null));
        var settings = Settings();
        settings.StartupTimeoutSeconds = 2;
        await using var host = Host(settings, starter, health);

        var status = await host.StartAsync();

        Assert.Equal(BackendState.Failed, status.State);
        Assert.Contains("did not answer", status.Detail);
    }

    [Fact]
    public async Task Stop_kills_only_the_process_it_started()
    {
        var starter = new FakeProcessStarter();
        var process = new FakeProcess();
        starter.Next = process;
        var health = new FakeHealth(new HealthResult(HealthKind.Free, null),
            new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        await using var host = Host(settings, starter, health);

        await host.StartAsync();
        await host.StopAsync();

        Assert.True(process.Stopped);
        Assert.Equal(BackendState.Stopped, host.Status.State);
    }

    [Fact]
    public async Task Stop_does_not_kill_a_backend_it_attached_to()
    {
        var starter = new FakeProcessStarter();
        var health = new FakeHealth(new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        await using var host = Host(settings, starter, health);

        await host.StartAsync();
        await host.StopAsync();

        Assert.Empty(starter.Started);
        Assert.Equal(BackendState.Stopped, host.Status.State);
    }

    [Fact]
    public async Task Status_changes_are_reported_to_the_shell()
    {
        var starter = new FakeProcessStarter { Next = new FakeProcess() };
        var health = new FakeHealth(new HealthResult(HealthKind.Free, null),
            new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        await using var host = Host(settings, starter, health);
        var states = new List<BackendState>();
        host.StatusChanged += status => states.Add(status.State);

        await host.StartAsync();

        Assert.Contains(BackendState.Starting, states);
        Assert.Contains(BackendState.Ready, states);
    }

    [Fact]
    public async Task A_demo_backend_is_flagged_even_though_it_answers()
    {
        // The dangerous case: a perfectly healthy backend on the expected port
        // that is serving synthetic demo data. The window must say so.
        var environment = new FakeEnvironment(
            new BackendEnvironment("C:\\mc5\\demo", Demo: true, MockMode: true));
        var starter = new FakeProcessStarter();
        var health = new FakeHealth(new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        await using var host = Host(settings, starter, health, environment: environment);

        var status = await host.StartAsync();

        Assert.Equal(BackendState.Attached, status.State);
        Assert.True(status.IsUsable);
        Assert.True(status.Demo);
        Assert.True(status.HasWarning);
        Assert.Contains("DEMO", status.Warning);
        Assert.Contains("synthetic", status.Warning);
    }

    [Fact]
    public async Task Demo_setting_against_a_real_backend_is_flagged()
    {
        var environment = new FakeEnvironment(new BackendEnvironment("C:\\mc5\\data", false, false));
        var starter = new FakeProcessStarter();
        var health = new FakeHealth(new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        settings.EnvironmentVariables["MC5_DEMO"] = "1";
        await using var host = Host(settings, starter, health, environment: environment);

        var status = await host.StartAsync();

        Assert.True(status.HasWarning);
        Assert.Contains("real data", status.Warning);
    }

    [Fact]
    public async Task A_matching_backend_has_no_warning()
    {
        var folder = Path.Combine(_backendDirectory, "data");
        var environment = new FakeEnvironment(new BackendEnvironment(folder, false, false));
        var starter = new FakeProcessStarter();
        var health = new FakeHealth(new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        settings.DataDirectory = folder;
        await using var host = Host(settings, starter, health, environment: environment);

        var status = await host.StartAsync();

        Assert.False(status.HasWarning);
        Assert.Equal(folder, status.DataDirectory);
        Assert.False(status.Demo);
    }

    [Fact]
    public async Task A_different_data_folder_is_flagged()
    {
        var environment = new FakeEnvironment(
            new BackendEnvironment(Path.Combine(_backendDirectory, "other"), false, false));
        var starter = new FakeProcessStarter();
        var health = new FakeHealth(new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        settings.DataDirectory = Path.Combine(_backendDirectory, "data");
        await using var host = Host(settings, starter, health, environment: environment);

        var status = await host.StartAsync();

        Assert.True(status.HasWarning);
        Assert.Contains("different data folder", status.Warning);
    }

    [Fact]
    public async Task A_redacted_user_profile_prefix_is_not_a_mismatch()
    {
        // The backend redacts the home folder as %USERPROFILE%; that must not
        // read as "a different folder" for the real folder it stands for.
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var real = Path.Combine(home, "mc5-desktop-test-data");
        var environment = new FakeEnvironment(
            new BackendEnvironment("%USERPROFILE%\\mc5-desktop-test-data", false, false));
        var starter = new FakeProcessStarter();
        var health = new FakeHealth(new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        settings.DataDirectory = real;
        await using var host = Host(settings, starter, health, environment: environment);

        var status = await host.StartAsync();

        Assert.False(status.HasWarning);
    }

    [Fact]
    public async Task An_unavailable_environment_probe_is_not_guessed()
    {
        var starter = new FakeProcessStarter();
        var health = new FakeHealth(new HealthResult(HealthKind.Ours, null));
        var settings = Settings();
        await using var host = Host(settings, starter, health,
            environment: new FakeEnvironment(null));

        var status = await host.StartAsync();

        Assert.Null(status.Demo);
        Assert.Null(status.DataDirectory);
        Assert.False(status.HasWarning);
    }

    private sealed class FakeEnvironment : IBackendEnvironmentProbe
    {
        private readonly BackendEnvironment? _environment;

        public FakeEnvironment(BackendEnvironment? environment) => _environment = environment;

        public Task<BackendEnvironment?> ReadAsync(int port, CancellationToken cancellationToken) =>
            Task.FromResult(_environment);
    }

    private sealed class FakeHealth : IHealthProbe
    {
        private readonly Queue<HealthResult> _results;
        private HealthResult? _last;

        public FakeHealth(params HealthResult[] results) => _results = new Queue<HealthResult>(results);

        public Task<HealthResult> CheckAsync(int port, CancellationToken cancellationToken)
        {
            if (_results.Count > 0)
            {
                _last = _results.Dequeue();
            }

            return Task.FromResult(_last ?? new HealthResult(HealthKind.Free, null));
        }
    }

    private sealed class FakeProcessStarter : IProcessStarter
    {
        public List<BackendLaunchPlan> Started { get; } = new();
        public FakeProcess? Next { get; set; }

        public IProcessHandle Start(BackendLaunchPlan plan)
        {
            Started.Add(plan);
            return Next ?? new FakeProcess();
        }
    }

    private sealed class FakeProcess : IProcessHandle
    {
        private readonly TaskCompletionSource _exit = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public bool HasExited { get; private set; }
        public int ExitCode { get; private set; }
        public string Tail { get; set; } = string.Empty;
        public bool Stopped { get; private set; }

        public void Exit(int code)
        {
            ExitCode = code;
            HasExited = true;
            _exit.TrySetResult();
        }

        public Task WaitForExitAsync(CancellationToken cancellationToken) =>
            _exit.Task.WaitAsync(cancellationToken);

        public void StopTree()
        {
            Stopped = true;
            Exit(0);
        }

        public void Dispose()
        {
            if (!HasExited)
            {
                Exit(0);
            }
        }
    }
}
