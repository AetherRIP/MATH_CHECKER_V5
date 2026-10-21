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
        MemoryLog? log = null) =>
        new(settings, log ?? new MemoryLog(), starter, health, _backendDirectory,
            TimeSpan.FromHours(1));

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
