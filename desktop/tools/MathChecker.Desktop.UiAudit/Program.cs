using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.UiAudit;

/// <summary>
/// Drives the real desktop pages the way a teacher does — through the page view
/// models the buttons are bound to — against a real backend, and paints each one
/// offscreen through the application's own DataTemplates.
///
/// Why this exists next to the unit tests: those hand a page a fake HTTP
/// handler, so they prove the wire format but never that a page with real data,
/// real rows and real numbers renders correctly, or that a binding path still
/// exists. And the existing view smoke tests render each view with no data at
/// all, which is exactly when a broken binding renders nothing and looks fine.
///
/// Everything runs headless: no window is created, nothing takes focus, so the
/// user can keep working while it runs.
///
/// Usage: dotnet run --project desktop/tools/MathChecker.Desktop.UiAudit [-- --shots dir]
/// Exit code 0 = every check passed.
/// </summary>
public static class Program
{
    private static readonly (int Window, int Height, string Label)[] Sizes =
    {
        (1440, 900, "1440x900"),
        (1280, 800, "1280x800"),
        (900, 600, "900x600"),
    };

    /// <summary>The navigation rail, expanded.</summary>
    private const int RailWidth = 220;

    /// <summary>The shell's own left and right page margin, 26 DIP each.</summary>
    private const int PageMargins = 52;

    /// <summary>A page is never as wide as the window: the rail and the shell
    /// margins are already gone before it is laid out. Measuring at the window
    /// width hides exactly the overflow a minimum-size window shows.</summary>
    private static int Content(int windowWidth) => windowWidth - RailWidth - PageMargins;

    /// <summary>Pages whose layout switches mode from Loaded. A view measured
    /// outside a window never loads, so their narrow layout cannot be reached
    /// here; the marking desk is covered by its own desk tests instead.</summary>
    private static readonly HashSet<string> LoadedDependent = new(StringComparer.OrdinalIgnoreCase)
    {
        "desk",
    };

    private static string? _only;

    public static async Task<int> Main(string[] args)
    {
        Console.OutputEncoding = Encoding.UTF8;
        _only = ArgumentValue(args, "--only");

        var backendDirectory = ArgumentValue(args, "--backend")
                               ?? BackendLocator.FindBackendDirectory(null, AppContext.BaseDirectory)
                               ?? BackendLocator.FindBackendDirectory(null, Environment.CurrentDirectory);
        if (backendDirectory is null)
        {
            Console.Error.WriteLine("FAIL: the V5 backend folder (containing main.py) was not found.");
            return 1;
        }

        var shots = ArgumentValue(args, "--shots")
                    ?? Path.Combine(backendDirectory, "data", "scratch", "ui-audit", "shots");
        var reportPath = ArgumentValue(args, "--report")
                         ?? Path.Combine(backendDirectory, "data", "scratch", "ui-audit", "report.md");

        var log = new FileLog(Path.Combine(Path.GetTempPath(), "mc5-ui-audit-logs"));
        var dataRoot = Directory.CreateTempSubdirectory("mc5-ui-audit-").FullName;
        var port = BackendHost.FindFreePort(8970);
        Console.WriteLine($"Backend:  {backendDirectory}");
        Console.WriteLine($"Data:     {dataRoot}");
        Console.WriteLine($"Port:     {port}");
        Console.WriteLine($"Shots:    {Path.GetFullPath(shots)}");
        Console.WriteLine();

        var settings = new DesktopSettings
        {
            BackendDirectory = backendDirectory,
            DataDirectory = dataRoot,
            Port = port,
            StartupTimeoutSeconds = 180,
            AttachToRunningBackend = true,
            LastExportDirectory = Path.Combine(dataRoot, "exports"),
        };
        settings.EnvironmentVariables["MC5_DEMO"] = "1";

        await using var host = new BackendHost(settings, log, appBaseDirectory: backendDirectory,
            heartbeatInterval: TimeSpan.FromSeconds(5));
        using var api = new MathCheckerApiClient();

        var audit = new Audit();
        var renders = new List<PageRender>();
        var journeys = new List<Check>();

        try
        {
            var status = await host.StartAsync();
            audit.Check("the backend starts and answers /api/health", status.IsUsable, status.Detail);
            if (!status.IsUsable)
            {
                return Report(audit, renders, journeys, reportPath, shots);
            }

            api.SetBaseUrl(host.BaseUrl);
            var auth = await api.RefreshSessionAsync();
            audit.Check("a local demo launch needs no sign-in",
                !auth.LoginRequired || auth.Authenticated,
                $"login_required={auth.LoginRequired} authenticated={auth.Authenticated}");

            var dashboard = await api.DashboardAsync();
            audit.Check("the backend reports demo mode (no real student data)",
                dashboard.DemoMode, $"demo_mode={dashboard.DemoMode}");

            var expectations = await Expectations.BuildAsync(api);
            audit.Check("the seeded data the audit expects is present",
                expectations.Ready, expectations.Summary);

            var result = await UiHost.RunAsync(() => WalkTheApplicationAsync(audit, renders, journeys,
                settings, log, host, api, expectations, shots, dataRoot));

            Console.WriteLine();
            Console.WriteLine($"final phase: {result}");

            await host.StopAsync();
            var probe = await new HttpHealthProbe().CheckAsync(port, CancellationToken.None);
            audit.Check("the backend stops cleanly and frees its port", probe.Kind == HealthKind.Free,
                probe.Kind.ToString());
        }
        catch (Exception ex)
        {
            audit.Fail("the audit ran to completion",
                ex.GetType().Name + ": " + ex.Message + Environment.NewLine + ex.StackTrace);
        }
        finally
        {
            try
            {
                await host.DisposeAsync();
            }
            catch (Exception)
            {
                // Already stopped.
            }
        }

        return Report(audit, renders, journeys, reportPath, shots);
    }

    private static async Task<string> WalkTheApplicationAsync(Audit audit, List<PageRender> renders,
        List<Check> journeys, DesktopSettings settings, IFileLog log, BackendHost host,
        MathCheckerApiClient api, Expectations expectations, string shots, string dataRoot)
    {
        // The application itself: App.xaml's resources and DataTemplates.
        if (Application.Current is not MathChecker.Desktop.App)
        {
            var app = new MathChecker.Desktop.App();
            app.InitializeComponent();
        }

        var bindings = BindingErrors.Install();
        var dialogs = new RecordingDialogs { SaveDirectory = Path.Combine(dataRoot, "exports") };

        // Rendered views leave the app's light/dark choice alone; the theme
        // journey below uses it deliberately.
        var session = new AppSession(settings, log, host, api, dialogs);
        var main = new MainViewModel(session);
        var toasts = new List<string>();
        session.Toast = message => toasts.Add(message);

        var started = Stopwatch.StartNew();
        await main.InitializeAsync();
        await UiHost.WaitUntilAsync(() => main.Current is not null, TimeSpan.FromMinutes(1), 500);
        started.Stop();

        audit.Check("the window opens on a page", main.Current is not null,
            main.Current?.Key ?? "(none)");
        audit.Check("the application builds every page", main.Pages.Count == 28,
            $"pages={main.Pages.Count}");
        audit.Check("the navigation rail has its spaces", main.Spaces.Count == 7,
            string.Join(", ", main.Spaces.Select(space => $"{space.Name}({space.Count})")));
        audit.Check("the backend is reported ready in the status bar", main.BackendAvailable,
            main.BackendShort + " / " + main.BackendDetail);

        Console.WriteLine($"startup: {started.ElapsedMilliseconds} ms to an interactive first page");

        // ------------------------------------------------------- 1. navigation
        var reachable = 0;
        foreach (var page in main.Pages.ToList())
        {
            var key = page.Key;
            main.NavigateTo(key);
            await UiHost.WaitUntilAsync(() => !page.IsBusy, TimeSpan.FromMinutes(2), 120)
                .WaitAsync(TimeSpan.FromMinutes(2));
            if (ReferenceEquals(main.Current, page))
            {
                reachable++;
            }
            else
            {
                audit.Fail($"navigation reaches '{key}'", $"landed on {main.Current?.Key ?? "(none)"}");
            }
        }

        audit.Check("every page is reachable by its navigation key", reachable == main.Pages.Count,
            $"{reachable} of {main.Pages.Count}");

        // A space stands for a destination: its rail entry opens its first page.
        var spaceHits = 0;
        foreach (var space in main.Spaces)
        {
            main.CurrentSpace = space;
            if (ReferenceEquals(main.Current, space.Pages[0]))
            {
                spaceHits++;
            }
            else
            {
                audit.Fail($"the '{space.Name}' rail entry opens its hub",
                    $"landed on {main.Current?.Key ?? "(none)"}");
            }
        }

        audit.Check("every rail entry opens its space's hub page", spaceHits == main.Spaces.Count,
            $"{spaceHits} of {main.Spaces.Count}");

        // The palette is the keyboard path to every page.
        var paletteHits = 0;
        foreach (var page in main.Pages)
        {
            main.OpenPalette();
            main.PaletteQuery = page.Title;
            if (main.PaletteResults.Contains(page))
            {
                paletteHits++;
            }
            else
            {
                audit.Fail($"Ctrl+K finds '{page.Title}'", $"hint={main.PaletteHint}");
            }
        }

        main.ClosePalette();
        audit.Check("Ctrl+K finds every page by name", paletteHits == main.Pages.Count,
            $"{paletteHits} of {main.Pages.Count}");

        main.OpenPalette();
        main.PaletteQuery = "zzzz-no-such-page";
        audit.Check("a palette query with no match says so instead of showing everything",
            main.PaletteResults.Count == 0 && main.PaletteEmpty, main.PaletteHint);
        main.PaletteQuery = string.Empty;
        main.ClosePalette();

        // ------------------------------------ 2. every page, with real backend data
        foreach (var page in main.Pages)
        {
            if (_only is not null && !page.Key.Equals(_only, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            main.NavigateTo(page.Key);
            await UiHost.WaitUntilAsync(() => !page.IsBusy && page.LastLoaded is not null,
                TimeSpan.FromMinutes(2), 250);

            audit.Check($"'{page.Key}' loads without an error", !page.HasError,
                page.Error ?? "(no error)");

            if (page.Key == "dashboard")
            {
                // Demo Mode is the one state a teacher must never mistake for
                // real marking, so the page itself has to say so.
                var demo = PagePainter.Paint(page, bindings, Content(1440), 900, null);
                audit.Check("the dashboard says it is showing demo data, not real marks",
                    demo.Text.Contains("DEMO", StringComparison.OrdinalIgnoreCase),
                    demo.Text.Length > 160 ? demo.Text[..160] : demo.Text);
            }

            // Data the page claims to show must be on screen, compared against
            // the backend rather than against the page's own memory of it.
            var expected = expectations.For(page.Key);
            if (expected.Count > 0)
            {
                var render = PagePainter.Paint(page, bindings, Content(1440), 900, null);
                var missing = expected.Where(fact => !render.Text.Contains(fact,
                    StringComparison.OrdinalIgnoreCase)).ToList();
                audit.Check($"'{page.Key}' shows the rows the backend returned", missing.Count == 0,
                    missing.Count == 0
                        ? string.Join(" | ", expected)
                        : "missing from the page: " + string.Join(" | ", missing));
            }

            foreach (var size in Sizes)
            {
                var first = size.Label == Sizes[0].Label;
                var shot = first
                    ? Path.Combine(shots, $"{page.Key}.png")
                    : Path.Combine(shots, $"{page.Key}-{size.Label}.png");
                var render = PagePainter.Paint(page, bindings, Content(size.Window), size.Height,
                    first || size.Label == "900x600" ? shot : null);
                renders.Add(render);

                var label = $"{page.Key}@{size.Window}x{size.Height}";
                audit.Check($"{label} resolves a view through its DataTemplate",
                    render.TemplateResolved, render.ViewType);
                audit.Check($"{label} has no data-binding errors", render.BindingErrors.Count == 0,
                    string.Join(" || ", render.BindingErrors));
                audit.Check($"{label} fits its width above its own minimum control",
                    LoadedDependent.Contains(page.Key) || render.PastRightEdge.Count == 0,
                    string.Join(" || ", render.PastRightEdge)
                    + (render.PastRightEdge.Count > 0
                        ? "\n      " + string.Join("\n      ", render.HeaderLayout)
                        : string.Empty));
                audit.Check($"{label} shows its text in full", render.ClippedText.Count == 0,
                    string.Join(" || ", render.ClippedText));
                if (render.CanExecuteFailures.Count > 0)
                {
                    audit.Fail($"{label} evaluates every enabled button's command",
                        string.Join(" || ", render.CanExecuteFailures));
                }

                audit.Check($"{label} paints something", render.Text.Length > 20,
                    $"{render.Text.Length} characters of text");
            }
        }

        audit.Check("no enabled button is left without a command", renders
                .SelectMany(render => render.CommandlessButtons.Select(name => $"{render.Key}: {name}"))
                .Distinct().Count() == 0,
            string.Join(" | ", renders.SelectMany(r => r.CommandlessButtons.Select(n => $"{r.Key}: {n}"))
                .Distinct().Take(12)));

        audit.Check("the keyboard reaches a page's controls",
            renders.Any(render => render.Focusable), "tab stops found");

        // ------------------------------------------------ 3. the real workflows
        if (_only is null)
        {
            await Journeys.RunAsync(audit, journeys, main, session, api, dialogs, bindings, shots,
                dataRoot, renders);
        }

        if (_only is not null)
        {
            // Stop here, while the dispatcher is still running: the host reports
            // its status on the calling thread, and a status arriving after this
            // thread has ended used to block it for ever.
            await host.StopAsync();
            return "focused run: " + _only;
        }

        // -------------------------------------------- 4. memory after navigation
        var before = Memory();
        for (var cycle = 0; cycle < 3; cycle++)
        {
            foreach (var page in main.Pages)
            {
                main.NavigateTo(page.Key);
                await UiHost.WaitUntilAsync(() => !page.IsBusy, TimeSpan.FromMinutes(1), 20);
            }
        }

        var after = Memory();
        audit.Check("navigating the whole application three times does not duplicate pages",
            main.Pages.Count == 28, $"pages={main.Pages.Count}");
        audit.Check("repeated navigation does not grow the heap without bound",
            after.ManagedMb - before.ManagedMb < 120,
            $"managed {before.ManagedMb:F1} MB → {after.ManagedMb:F1} MB " +
            $"(+{after.ManagedMb - before.ManagedMb:F1} MB), " +
            $"working set {before.WorkingSetMb:F0} → {after.WorkingSetMb:F0} MB");

        // --------------------------------------- 5. the backend goes away mid-session
        await host.StopAsync();
        var offline = 0;
        var stuck = new List<string>();
        foreach (var page in main.Pages)
        {
            main.NavigateTo(page.Key);
            await UiHost.WaitUntilAsync(() => !page.IsBusy, TimeSpan.FromSeconds(20), 100);
            if (page.HasError && !page.IsBusy)
            {
                offline++;
            }
            else
            {
                stuck.Add(page.Key);
            }
        }

        audit.Check("every page says it cannot reach the backend instead of spinning",
            offline == main.Pages.Count, $"{offline} of {main.Pages.Count}" +
                                         (stuck.Count > 0 ? " silent: " + string.Join(",", stuck) : string.Empty));

        // The page must PAINT the reason, not merely store it: the check looks for
        // the exact sentence the page is holding, so a strip bound to the wrong
        // property (or not bound at all) fails here.
        var offlinePage = main.Pages.First(page => page.Key == "dashboard");
        var offlineRender = PagePainter.Paint(offlinePage, bindings, Content(1440), 900,
            Path.Combine(shots, "offline-dashboard.png"));
        var reason = offlinePage.Error ?? string.Empty;
        var shown = reason.Length > 0 && offlineRender.Text.Contains(
            reason[..Math.Min(40, reason.Length)], StringComparison.OrdinalIgnoreCase);
        audit.Check("the offline reason is painted on the page, not just held in the model",
            shown, $"error='{reason}' text='{offlineRender.Text[..Math.Min(160, offlineRender.Text.Length)]}'");

        return $"stopped ({offline} of {main.Pages.Count} pages reported it)";
    }

    private static (double ManagedMb, double WorkingSetMb) Memory()
    {
        GC.Collect();
        GC.WaitForPendingFinalizers();
        GC.Collect();
        var managed = GC.GetTotalMemory(true) / (1024d * 1024d);
        using var process = Process.GetCurrentProcess();
        return (managed, process.WorkingSet64 / (1024d * 1024d));
    }

    private static int Report(Audit audit, List<PageRender> renders, List<Check> journeys,
        string reportPath, string shots)
    {
        Console.WriteLine();
        foreach (var check in audit.Checks.Where(check => !check.Ok))
        {
            Console.WriteLine($"FAIL  {check.Name}  {check.Detail}");
        }

        Console.WriteLine();
        Console.WriteLine($"{audit.Passed} passed, {audit.Failed} failed"
                          + (journeys.Count > 0
                              ? $"; journeys {journeys.Count(j => j.Ok)} passed, {journeys.Count(j => !j.Ok)} failed"
                              : string.Empty));

        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(reportPath))!);
            File.WriteAllText(reportPath, Markdown(audit, renders, journeys, shots));
            Console.WriteLine("report: " + Path.GetFullPath(reportPath));
            Console.WriteLine("shots:  " + Path.GetFullPath(shots));
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("could not write the report: " + ex.Message);
        }

        return audit.Failed == 0 && journeys.All(check => check.Ok) ? 0 : 1;
    }

    private static string Markdown(Audit audit, List<PageRender> renders, List<Check> journeys,
        string shots)
    {
        var builder = new StringBuilder();
        builder.AppendLine("# Desktop UI audit");
        builder.AppendLine();
        builder.AppendLine($"{audit.Passed} passed, {audit.Failed} failed; journeys "
                           + $"{journeys.Count(check => check.Ok)} passed, "
                           + $"{journeys.Count(check => !check.Ok)} failed.");
        builder.AppendLine();

        builder.AppendLine("## Pages");
        builder.AppendLine();
        builder.AppendLine("| Page | View | Size | Binding errors | Clipped | Past edge | Buttons | Inputs |");
        builder.AppendLine("|---|---|---|---|---|---|---|---|");
        foreach (var render in renders)
        {
            builder.AppendLine($"| {render.Key} | {render.ViewType} | {render.Width}x{render.Height} "
                               + $"| {render.BindingErrors.Count} | {render.ClippedText.Count} "
                               + $"| {render.PastRightEdge.Count} | {render.EnabledButtons}/{render.Buttons} "
                               + $"| {render.Inputs} |");
        }

        builder.AppendLine();
        builder.AppendLine("## Checks");
        builder.AppendLine();
        foreach (var check in audit.Checks)
        {
            builder.AppendLine($"- {(check.Ok ? "PASS" : "FAIL")} — {check.Name}"
                               + (string.IsNullOrWhiteSpace(check.Detail)
                                   ? string.Empty
                                   : $"  ({check.Detail.Replace("|", "\\|")})"));
        }

        if (journeys.Count > 0)
        {
            builder.AppendLine();
            builder.AppendLine("## Journeys");
            builder.AppendLine();
            foreach (var check in journeys)
            {
                builder.AppendLine($"- {(check.Ok ? "PASS" : "FAIL")} — {check.Name}"
                                   + (string.IsNullOrWhiteSpace(check.Detail)
                                       ? string.Empty
                                       : $"  ({check.Detail.Replace("|", "\\|")})"));
            }
        }

        builder.AppendLine();
        builder.AppendLine($"Screenshots: `{shots}`");
        return builder.ToString();
    }

    private static string? ArgumentValue(string[] args, string name)
    {
        for (var index = 0; index < args.Length - 1; index++)
        {
            if (args[index] == name)
            {
                return args[index + 1];
            }
        }

        return null;
    }

    internal static string Format(double value) =>
        Math.Round(value, 1).ToString(CultureInfo.InvariantCulture);
}

/// <summary>
/// What each page must show, taken from the backend rather than from the page.
/// A page that renders its title and an empty body passes a screenshot review;
/// it fails this.
/// </summary>
internal sealed class Expectations
{
    private readonly Dictionary<string, List<string>> _facts = new(StringComparer.OrdinalIgnoreCase);

    public bool Ready { get; private set; }

    public string Summary { get; private set; } = string.Empty;

    public IReadOnlyList<string> For(string key) =>
        _facts.TryGetValue(key, out var facts) ? facts : Array.Empty<string>();

    public static async Task<Expectations> BuildAsync(MathCheckerApiClient api)
    {
        var expectations = new Expectations();
        var parts = new List<string>();
        try
        {
            var dashboard = await api.DashboardAsync();
            var students = await api.StudentsAsync();
            var assignments = await api.AssignmentsAsync();
            var results = await api.ResultsAsync();
            var jobs = await api.JobsAsync(limit: 20);
            var classes = await api.ClassesAsync();

            var student = students.Students.FirstOrDefault();
            var assignment = assignments.Assignments.FirstOrDefault();
            var resultRow = results.Results.FirstOrDefault();
            var job = jobs.Jobs.FirstOrDefault();

            if (student?.DisplayName is { Length: > 0 } studentName)
            {
                expectations._facts["people"] = new List<string> { studentName };
                parts.Add($"student '{studentName}'");
            }

            if (assignment?.DisplayTitle is { Length: > 0 } title)
            {
                expectations._facts["assignments"] = new List<string> { title };
                expectations._facts["automation"] = new List<string> { title };
                parts.Add($"assignment '{title}'");
            }

            if (resultRow?.DisplayName is { Length: > 0 } resultName)
            {
                expectations._facts["results"] = new List<string> { resultName };
                parts.Add($"result for '{resultName}'");
            }

            if (job?.DisplayName is { Length: > 0 } jobName)
            {
                parts.Add($"job for '{jobName}'");
            }

            if (dashboard.Version is { Length: > 0 } version)
            {
                expectations._facts["dashboard"] = new List<string> { version };
                parts.Add($"version {version}");
            }

            var classRow = classes.Classes.FirstOrDefault();
            if (classRow?.Name is { Length: > 0 } className)
            {
                parts.Add($"class '{className}'");
            }

            expectations.Ready = parts.Count >= 4;
            expectations.Summary = string.Join(", ", parts);
        }
        catch (Exception ex)
        {
            expectations.Summary = "could not read the demo data: " + ex.Message;
        }

        return expectations;
    }
}
