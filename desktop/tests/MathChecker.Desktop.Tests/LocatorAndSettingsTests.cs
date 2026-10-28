using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;

namespace MathChecker.Desktop.Tests;

public class LocatorAndSettingsTests : IDisposable
{
    private readonly string _root;

    public LocatorAndSettingsTests()
    {
        _root = Directory.CreateTempSubdirectory("mc5-locator-").FullName;
    }

    public void Dispose()
    {
        try
        {
            Directory.Delete(_root, true);
        }
        catch (IOException)
        {
        }
    }

    private string MakeCheckout()
    {
        var checkout = Path.Combine(_root, "checkout");
        File.WriteAllText(Path.Combine(Directory.CreateDirectory(checkout).FullName, "main.py"), "# stub");
        Directory.CreateDirectory(Path.Combine(checkout, "backend", "app"));
        return checkout;
    }

    [Fact]
    public void Recognises_only_a_folder_with_main_and_backend_app()
    {
        var checkout = MakeCheckout();
        Assert.True(BackendLocator.LooksLikeBackend(checkout));
        Assert.False(BackendLocator.LooksLikeBackend(_root));
        Assert.False(BackendLocator.LooksLikeBackend(null));
    }

    [Fact]
    public void Finds_the_checkout_from_a_nested_build_output_folder()
    {
        var checkout = MakeCheckout();
        var nested = Path.Combine(checkout, "desktop", "src", "MathChecker.Desktop", "bin", "Debug",
            "net10.0-windows");
        Directory.CreateDirectory(nested);

        var found = BackendLocator.FindBackendDirectory(null, nested);

        Assert.Equal(Path.GetFullPath(checkout), found);
    }

    [Fact]
    public void Reports_nothing_when_the_install_has_no_backend_folder()
    {
        // The installed-but-broken case: discovery must come back empty so the
        // shell can say "the backend was not found" (BackendHost failure
        // status) instead of guessing a path and starting something else.
        var empty = Directory.CreateTempSubdirectory("mc5-nobackend-").FullName;
        try
        {
            Assert.Null(BackendLocator.FindBackendDirectory("", empty, empty));
        }
        finally
        {
            try { Directory.Delete(empty, true); } catch (IOException) { }
        }
    }

    [Fact]
    public void An_explicit_setting_wins_and_an_invalid_one_falls_back()
    {
        var checkout = MakeCheckout();
        var other = Path.Combine(_root, "other");
        Directory.CreateDirectory(other);

        // An invalid explicit path falls back to discovery instead of failing.
        Assert.Equal(Path.GetFullPath(checkout), BackendLocator.FindBackendDirectory(other, checkout));
        // A valid explicit path wins over a different checkout found nearby.
        Assert.Equal(Path.GetFullPath(checkout), BackendLocator.FindBackendDirectory(checkout, _root));
    }

    [Fact]
    public void Prefers_the_bundled_runtime_then_the_venv()
    {
        var checkout = MakeCheckout();
        Directory.CreateDirectory(Path.Combine(checkout, "runtime"));
        File.WriteAllText(Path.Combine(checkout, "runtime", "python.exe"), string.Empty);
        var venv = Directory.CreateDirectory(Path.Combine(checkout, ".venv", "Scripts")).FullName;
        File.WriteAllText(Path.Combine(venv, "python.exe"), string.Empty);

        var launch = BackendLocator.ResolvePython(null, checkout);

        Assert.NotNull(launch);
        Assert.Equal(Path.Combine(checkout, "runtime", "python.exe"), launch!.Executable);
    }

    [Fact]
    public void Falls_back_to_the_venv_when_no_runtime_is_bundled()
    {
        var checkout = MakeCheckout();
        var venv = Directory.CreateDirectory(Path.Combine(checkout, ".venv", "Scripts")).FullName;
        File.WriteAllText(Path.Combine(venv, "python.exe"), string.Empty);

        var launch = BackendLocator.ResolvePython(null, checkout);

        Assert.Equal(Path.Combine(checkout, ".venv", "Scripts", "python.exe"), launch!.Executable);
        Assert.Empty(launch.PrefixArguments);
    }

    [Fact]
    public void An_explicit_python_path_is_used_as_given()
    {
        var checkout = MakeCheckout();
        var python = Path.Combine(_root, "custom-python.exe");
        File.WriteAllText(python, string.Empty);

        var launch = BackendLocator.ResolvePython(python, checkout);

        Assert.Equal(python, launch!.Executable);
    }

    [Fact]
    public void Launch_plan_runs_main_with_serve_and_the_port()
    {
        var checkout = MakeCheckout();
        var plan = BackendLocator.BuildPlan(checkout, new PythonLaunch("python.exe", Array.Empty<string>()),
            9001, @"C:\data");

        Assert.Equal("python.exe", plan.Executable);
        Assert.Equal(new[] { "main.py", "--serve", "9001" }, plan.Arguments);
        Assert.Equal(@"C:\data", plan.DataDirectory);
        Assert.Contains("--serve 9001", plan.Describe());
    }

    [Fact]
    public void Py_launcher_keeps_its_version_argument_first()
    {
        var plan = BackendLocator.BuildPlan(_root, new PythonLaunch("py", new[] { "-3" }), 8642, null);

        Assert.Equal(new[] { "-3", "main.py", "--serve", "8642" }, plan.Arguments);
    }

    [Fact]
    public void Settings_round_trip_without_secrets()
    {
        var path = Path.Combine(_root, "settings.json");
        var settings = new DesktopSettings
        {
            Port = 9000,
            BackendDirectory = @"E:\MathChecker",
            LastExportDirectory = @"E:\Reports",
            StartupTimeoutSeconds = 45,
        };

        settings.Save(path);
        var loaded = DesktopSettings.Load(path);

        Assert.Equal(9000, loaded.Port);
        Assert.Equal(@"E:\MathChecker", loaded.BackendDirectory);
        Assert.Equal(45, loaded.StartupTimeoutSeconds);
        var text = File.ReadAllText(path);
        Assert.DoesNotContain("key", text, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("password", text, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// A settings object built in memory belongs to no file. Saving it used to
    /// fall back to the user's real settings.json, so the UI audit — which drives
    /// the pages against a demo backend — left its demo configuration behind and
    /// the teacher's next launch opened Demo Mode with a temporary data folder.
    /// </summary>
    [Fact]
    public void Settings_built_in_memory_have_no_file_to_save_to()
    {
        var settings = new DesktopSettings { Port = 9100 };

        Assert.Null(settings.SourcePath);
        Assert.False(settings.CanSave);

        // No exception, and still nothing to write to: Save() must not invent a path.
        settings.Save();
        Assert.Null(settings.SourcePath);
        Assert.False(settings.CanSave);
    }

    [Fact]
    public void Settings_loaded_from_a_file_save_back_to_that_file()
    {
        // A first launch: the file does not exist yet, and Save() must create it
        // where the settings were loaded from rather than somewhere else.
        var path = Path.Combine(_root, "fresh", "settings.json");
        var settings = DesktopSettings.Load(path);

        Assert.True(settings.CanSave);
        settings.Appearance = "dark";
        settings.Save();

        Assert.True(File.Exists(path));
        Assert.Equal("dark", DesktopSettings.Load(path).Appearance);
    }

    [Fact]
    public void Corrupt_settings_fall_back_to_defaults()
    {
        var path = Path.Combine(_root, "broken.json");
        File.WriteAllText(path, "{ this is not json");

        var loaded = DesktopSettings.Load(path);

        Assert.Equal(8642, loaded.Port);
        Assert.True(loaded.StartBackendAutomatically);
    }

    [Fact]
    public void Settings_are_clamped_to_usable_ranges()
    {
        var settings = new DesktopSettings
        {
            Port = 70000,
            StartupTimeoutSeconds = 1,
            WindowWidth = 10,
            WindowHeight = 10,
        }.Normalize();

        Assert.Equal(8642, settings.Port);
        Assert.Equal(5, settings.StartupTimeoutSeconds);
        Assert.Equal(900, settings.WindowWidth);
        Assert.Equal(600, settings.WindowHeight);
    }

    [Fact]
    public void Log_redacts_credentials_and_emails()
    {
        var line = SecretRedactor.Redact(
            "key AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA request Bearer abc.def.ghi "
            + "api_key=SUPERSECRET student.a@school.example job 0123456789abcdef0123456789abcdef");

        Assert.DoesNotContain("AIzaSy", line);
        Assert.DoesNotContain("SUPERSECRET", line);
        Assert.DoesNotContain("abc.def.ghi", line);
        Assert.DoesNotContain("student.a@school.example", line);
        // Job ids are diagnostics, not secrets: they must survive.
        Assert.Contains("0123456789abcdef0123456789abcdef", line);
    }

    [Fact]
    public void Memory_log_redacts_too()
    {
        var log = new MemoryLog();
        log.Error("failed with AIzaSyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB");

        Assert.Contains("<google-key>", log.All[0]);
    }
}
