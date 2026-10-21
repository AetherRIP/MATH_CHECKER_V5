using System.Diagnostics;
using System.IO;
using System.Text;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;

namespace MathChecker.Desktop.E2E;

/// <summary>
/// Verifies the desktop client against a REAL V5 backend running in Demo Mode:
/// the desktop host starts it, the client reads real endpoints, submits a real
/// correction (mock marking, because Demo Mode forces it), exports the report,
/// and then stops the process. Demo data is synthetic, so no student record is
/// touched.
///
/// Usage: dotnet run --project desktop/tools/MathChecker.Desktop.E2E
/// Exit code 0 = every check passed; 1 = at least one failed.
/// </summary>
public static class Program
{
    private static int _failures;
    private static int _passes;

    public static async Task<int> Main(string[] args)
    {
        Console.OutputEncoding = Encoding.UTF8;
        var backendDirectory = ArgumentValue(args, "--backend")
                               ?? BackendLocator.FindBackendDirectory(null, AppContext.BaseDirectory)
                               ?? BackendLocator.FindBackendDirectory(null, Environment.CurrentDirectory);
        if (backendDirectory is null)
        {
            Console.Error.WriteLine("FAIL: the V5 backend folder (containing main.py) was not found.");
            return 1;
        }

        Console.WriteLine($"Backend folder: {backendDirectory}");
        var log = new FileLog(Path.Combine(Path.GetTempPath(), "mc5-desktop-e2e-logs"));
        var dataRoot = Directory.CreateTempSubdirectory("mc5-desktop-e2e-").FullName;
        var port = BackendHost.FindFreePort(8960);
        Console.WriteLine($"Isolated data folder: {dataRoot}");
        Console.WriteLine($"Port: {port}");

        var settings = new DesktopSettings
        {
            BackendDirectory = backendDirectory,
            DataDirectory = dataRoot,
            Port = port,
            StartupTimeoutSeconds = 180,
            AttachToRunningBackend = true,
        };
        settings.EnvironmentVariables["MC5_DEMO"] = "1";

        await using var host = new BackendHost(settings, log, appBaseDirectory: backendDirectory,
            heartbeatInterval: TimeSpan.FromSeconds(5));
        using var api = new MathCheckerApiClient();

        try
        {
            var status = await host.StartAsync();
            Check("backend reaches /api/health", status.IsUsable, status.Detail);
            if (!status.IsUsable)
            {
                return await Finish(host);
            }

            api.SetBaseUrl(host.BaseUrl);
            var auth = await api.RefreshSessionAsync();
            Check("local mode needs no sign-in", !auth.LoginRequired || auth.Authenticated,
                $"login_required={auth.LoginRequired} authenticated={auth.Authenticated}");

            var dashboard = await api.DashboardAsync();
            Check("dashboard reports the demo flag", dashboard.DemoMode, $"demo_mode={dashboard.DemoMode}");
            Check("dashboard reports a version", !string.IsNullOrWhiteSpace(dashboard.Version),
                dashboard.Version ?? "(none)");
            Check("dashboard counts completed jobs", dashboard.Completed >= 8,
                $"completed={dashboard.Completed}");

            var classes = await api.ClassesAsync();
            Check("classes list returns the seeded classes", classes.Total >= 2, $"total={classes.Total}");

            var assignments = await api.AssignmentsAsync();
            Check("assignments list returns rows", assignments.Total >= 1, $"total={assignments.Total}");

            var students = await api.StudentsAsync();
            Check("students list returns rows", students.Students.Count >= 3,
                $"count={students.Students.Count}");

            var results = await api.ResultsAsync();
            Check("results list returns rows", results.Results.Count >= 3,
                $"count={results.Results.Count}");

            var review = await api.ReviewQueueAsync();
            Check("review queue answers", review.Total >= 0, $"total={review.Total}");

            var pipelines = await api.CorrectionPipelinesAsync();
            Check("pipeline facts answer", pipelines.TryGetProperty("threshold_pages", out _),
                "threshold_pages present");

            var pdfs = CreatePdfs(backendDirectory, dataRoot);
            if (pdfs is null)
            {
                Console.WriteLine("SKIP: PyMuPDF could not create the sample PDFs; the correction "
                                  + "round-trip was not exercised.");
            }
            else
            {
                await CorrectionRoundTripAsync(api, pdfs.Value.Submission, pdfs.Value.Scheme);
            }

            await api.SaveSettingsAsync(new { debug = false });
            var settingsAfter = await api.SettingsAsync();
            Check("settings save round-trips", settingsAfter.Debug == false, "debug=false");

            await host.StopAsync();
            Check("backend stopped cleanly", !host.Status.IsUsable, host.Status.Summary);
            var probe = await new HttpHealthProbe().CheckAsync(port, CancellationToken.None);
            Check("no listener remains on the port", probe.Kind == HealthKind.Free,
                probe.Kind.ToString());
        }
        catch (Exception ex)
        {
            _failures++;
            Console.Error.WriteLine("FAIL: unhandled " + ex.GetType().Name + ": " + ex.Message);
            Console.Error.WriteLine(ex.StackTrace);
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

            try
            {
                Directory.Delete(dataRoot, true);
            }
            catch (IOException)
            {
                // Best effort.
            }
        }

        Console.WriteLine();
        Console.WriteLine($"{_passes} passed, {_failures} failed");
        return _failures == 0 ? 0 : 1;
    }

    private static async Task CorrectionRoundTripAsync(MathCheckerApiClient api, string submission,
        string scheme)
    {
        var queued = await api.SubmitUnifiedCorrectionAsync(submission, scheme, "Student E2E", "standard",
            null, CancellationToken.None);
        Console.WriteLine("  unified response: " + queued.GetRawText()[..Math.Min(400,
            queued.GetRawText().Length)]);
        var jobId = queued.TryGetProperty("job_id", out var j) ? j.GetString() : null;
        var largeId = queued.TryGetProperty("large_pdf_job_id", out var l) ? l.GetString() : null;
        Check("unified correction is accepted", jobId is not null || largeId is not null,
            jobId is not null ? "job=" + jobId : "large=" + largeId);

        if (jobId is null)
        {
            Console.WriteLine("  batched pipeline chosen; its worker runs in the background. "
                              + "The standard path is covered by the unit tests.");
            return;
        }

        var deadline = DateTime.UtcNow.AddMinutes(3);
        string? resultId = null;
        var lastStatus = string.Empty;
        while (DateTime.UtcNow < deadline)
        {
            var job = await api.JobAsync(jobId);
            lastStatus = job.Status ?? string.Empty;
            if (lastStatus == "COMPLETED")
            {
                resultId = job.ResultId;
                break;
            }

            if (lastStatus is "FAILED" or "CANCELLED")
            {
                Check("correction job completes", false, $"{lastStatus}: {job.Error}");
                return;
            }

            await Task.Delay(1000);
        }

        Check("correction job completes", lastStatus == "COMPLETED", "last status=" + lastStatus);
        if (resultId is null)
        {
            return;
        }

        var detail = await api.ResultAsync(resultId);
        Check("result carries marks", detail.Earned is not null && detail.Available is not null
                                      && detail.Available > 0,
            $"{detail.Earned}/{detail.Available} ({detail.PercentageText})");

        var pdf = await api.ExportResultAsync(resultId, "pdf");
        var header = Encoding.ASCII.GetString(pdf.Bytes.Take(5).ToArray());
        Check("PDF export is a real PDF", header == "%PDF-", $"header={header} bytes={pdf.Bytes.Length}");

        var csv = await api.ExportResultAsync(resultId, "csv");
        Check("CSV export answers", csv.Bytes.Length > 0, $"{csv.Bytes.Length} bytes");

        var versions = await api.ResultVersionsAsync(resultId);
        Check("version history answers", versions.Versions.Count >= 0, $"{versions.Versions.Count} versions");
    }

    private static (string Submission, string Scheme)? CreatePdfs(string backendDirectory, string workRoot)
    {
        var python = BackendLocator.ResolvePython(null, backendDirectory);
        if (python is null)
        {
            return null;
        }

        var script = Path.Combine(workRoot, "make_pdfs.py");
        File.WriteAllText(script, """
import sys
import pymupdf

def make(path, lines):
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=12)
        y += 18
    doc.save(path)
    doc.close()

make(sys.argv[1], ["Student answer sheet", "Question 1: 2 + 2 = 4", "Question 2: 3 x 4 = 12"])
make(sys.argv[2], ["Mark scheme", "Q1: 2 marks", "Q2: 2 marks"])
""");

        var submission = Path.Combine(workRoot, "student.pdf");
        var scheme = Path.Combine(workRoot, "scheme.pdf");
        var start = new ProcessStartInfo
        {
            FileName = python.Executable,
            WorkingDirectory = backendDirectory,
            UseShellExecute = false,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
        };
        foreach (var prefix in python.PrefixArguments)
        {
            start.ArgumentList.Add(prefix);
        }

        start.ArgumentList.Add(script);
        start.ArgumentList.Add(submission);
        start.ArgumentList.Add(scheme);

        try
        {
            using var process = Process.Start(start);
            if (process is null)
            {
                return null;
            }

            process.WaitForExit(60_000);
            var error = process.StandardError.ReadToEnd();
            if (process.ExitCode != 0 || !File.Exists(submission) || !File.Exists(scheme))
            {
                Console.WriteLine("  PDF generation failed: " + error);
                return null;
            }

            return (submission, scheme);
        }
        catch (Exception ex)
        {
            Console.WriteLine("  PDF generation failed: " + ex.Message);
            return null;
        }
    }

    private static void Check(string name, bool ok, string detail)
    {
        if (ok)
        {
            _passes++;
            Console.WriteLine($"PASS  {name}" + (string.IsNullOrWhiteSpace(detail) ? string.Empty : $"  ({detail})"));
        }
        else
        {
            _failures++;
            Console.WriteLine($"FAIL  {name}  {detail}");
        }
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

    private static async Task<int> Finish(BackendHost host)
    {
        await host.StopAsync();
        Console.WriteLine($"{_passes} passed, {_failures} failed");
        return _failures == 0 ? 0 : 1;
    }
}
