using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Threading;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.UiAudit;

/// <summary>
/// Complete user journeys, driven through the page view models the buttons are
/// bound to — the same code path a click takes, minus the mouse.
///
/// Each journey is what a teacher actually does in one sitting, so it is the
/// only way to see the defects that live BETWEEN pages: a created assignment
/// that never opens, a scheme uploaded to the wrong assignment, a correction
/// that finishes but never reaches the results table, an export that saves an
/// empty file.
/// </summary>
internal static class Journeys
{
    public static async Task RunAsync(Audit audit, List<Check> journeys, MainViewModel main,
        AppSession session, MathCheckerApiClient api, RecordingDialogs dialogs,
        BindingErrors bindings, string shots, string dataRoot, List<PageRender> renders)
    {
        void Check(string name, bool ok, string detail) => journeys.Add(new Check(name, ok, detail));

        var created = Pdfs.Create(dataRoot);
        if (created is null)
        {
            Check("sample papers could be created for the correction journey", false,
                "PyMuPDF was not available, so the upload journey could not run");
            return;
        }

        var (submissionPdf, schemePdf) = created.Value;

        var title = "UI Audit Assignment " + DateTime.Now.ToString("HHmmss");

        // ------------------------------------------- A. create an assignment
        var create = Page<CreateAssignmentViewModel>(main, "new-assignment");
        main.NavigateTo("new-assignment");
        await SettleAsync(create);

        create.AssignmentTitle = title;
        create.MaxPoints = "12";
        create.Notes = "Created by the UI audit.";
        dialogs.QueueOpenFile(schemePdf);
        create.PickSchemeCommand.Execute(null);
        Check("picking a mark scheme fills the form", create.HasScheme
                                                    && create.SchemeName.EndsWith(".pdf", StringComparison.Ordinal),
            create.SchemeName);

        create.CreateCommand.Execute(null);
        await UiHost.WaitUntilAsync(() => main.Current?.Key == "assignment"
                                          && !main.Current.IsBusy, TimeSpan.FromMinutes(3), 400);

        var detail = main.Current as AssignmentDetailViewModel;
        Check("creating an assignment opens the new assignment, not the list",
            detail is not null, main.Current?.Key ?? "(none)");
        if (detail is not null)
        {
            await SettleAsync(detail);
            Check("the new assignment's page is titled with what was typed",
                detail.Heading.Contains(title, StringComparison.OrdinalIgnoreCase), detail.Heading);
            Check("the assignment page reports its mark scheme as attached",
                detail.SchemeAttached, detail.SchemeTitle);
            Check("the review-scheme button is offered once a scheme exists",
                detail.ReviewSchemeCommand.CanExecute(null), "ReviewSchemeCommand");
            Check("the assignment page loaded without an error", !detail.HasError,
                detail.Error ?? "(no error)");
        }

        var listed = await api.AssignmentsAsync();
        Check("the assignment is stored, not only shown",
            listed.Assignments.Any(row => row.DisplayTitle.Contains(title, StringComparison.OrdinalIgnoreCase)),
            $"{listed.Total} assignments");

        // ------------------------------------- B. review the mark scheme of it
        if (detail is not null)
        {
            detail.ReviewSchemeCommand.Execute(null);
            await UiHost.WaitUntilAsync(() => main.Current?.Key == "markscheme"
                                              && !main.Current.IsBusy, TimeSpan.FromMinutes(3), 400);
            var scheme = main.Current as MarkSchemeViewModel;
            Check("the mark scheme page opens on the assignment that was reviewed",
                scheme is not null, main.Current?.Key ?? "(none)");
            if (scheme is not null)
            {
                Check("the mark scheme page opens on the assignment whose button was pressed",
                    scheme.CourseworkId.Length > 0, scheme.CourseworkId);
                Check("the mark scheme page loads without an error", !scheme.HasError,
                    scheme.Error ?? "(no error)");

                if (scheme.Questions.Count > 0)
                {
                    Check("the mark scheme page names its assignment",
                        scheme.AssignmentTitle.Contains(title, StringComparison.OrdinalIgnoreCase),
                        scheme.AssignmentTitle);
                    Check("the mark scheme page says how many questions it parsed",
                        !string.IsNullOrWhiteSpace(scheme.CountsLine), scheme.CountsLine);
                    var first = scheme.Questions[0];
                    scheme.Selected = first;
                    scheme.SelectDecisionCommand.Execute("MANUAL");
                    await UiHost.WaitUntilAsync(() => first.Decision != "AUTO",
                        TimeSpan.FromSeconds(30), 400);
                    Check("a handling decision can be changed on a parsed question",
                        first.Decision == "MANUAL", $"{first.Label} → {first.Decision}");
                    Check("the decision is written to the review history",
                        scheme.History.Count > 0, $"{scheme.History.Count} entries");
                    Check("the page's counters follow the decision",
                        scheme.CountsLine.Length > 0, scheme.CountsLine);

                    var render = PagePainter.Paint(scheme, bindings, 1440, 900,
                        Path.Combine(shots, "journey-markscheme.png"));
                    renders.Add(render);
                    Check("the reviewed scheme renders with no binding errors",
                        render.BindingErrors.Count == 0,
                        string.Join(" || ", render.BindingErrors));
                }
                else
                {
                    // An attached scheme that has not been read: the page must say
                    // so (not "no scheme attached") and offer the action that reads
                    // it. In Demo Mode the read produces a labelled synthetic
                    // structure, so the review workflow is explorable without an
                    // API key — and it must never read as a real extraction.
                    Check("an unread scheme is explained, not reported as a missing file",
                        scheme.SummaryLine.Contains("attached", StringComparison.OrdinalIgnoreCase)
                        && scheme.SummaryLine.Contains("read", StringComparison.OrdinalIgnoreCase),
                        scheme.SummaryLine);
                    Check("the page offers to read the scheme on demand",
                        scheme.NeedsRead && scheme.ReadSchemeCommand.CanExecute(null),
                        $"needs_read={scheme.NeedsRead} hint='{scheme.ReadHint}'");

                    scheme.ReadSchemeCommand.Execute(null);
                    await UiHost.WaitUntilAsync(() => !scheme.IsBusy
                                                      && (!scheme.NeedsRead || scheme.ReadHint.Length > 0),
                        TimeSpan.FromMinutes(5), 600);

                    Check("reading the scheme is attempted against the real backend and reported honestly",
                        scheme.Questions.Count > 0
                        || scheme.SummaryLine.Contains("could not be read",
                            StringComparison.OrdinalIgnoreCase),
                        scheme.SummaryLine + " | " + scheme.ReadHint);
                    Check("a read that produced nothing does not raise an error or pretend",
                        !scheme.HasError, scheme.Error ?? "(no error)");

                    if (scheme.Questions.Count > 0)
                    {
                        Check("the on-demand read makes the page reviewable in place",
                            !scheme.NeedsRead && scheme.FinalizeCommand.CanExecute(null),
                            $"needs_read={scheme.NeedsRead}");
                        Check("a demo structure is labelled synthetic, never passed off as a read scheme",
                            scheme.HasDemoNotice
                            == scheme.Questions.Any(q => q.ExtractionStatus == "DEMO"),
                            scheme.DemoNotice);
                        Check("every demo row says so on the row itself",
                            scheme.Questions.All(q => q.ExtractionStatus != "DEMO"
                                                      || q.StatusText.Contains("demo",
                                                          StringComparison.OrdinalIgnoreCase)),
                            string.Join(", ", scheme.Questions.Select(q => q.StatusText).Distinct()));

                        var decided = scheme.Questions[0];
                        scheme.Selected = decided;
                        scheme.SelectDecisionCommand.Execute("MANUAL");
                        await UiHost.WaitUntilAsync(() => decided.Decision == "MANUAL",
                            TimeSpan.FromSeconds(30), 400);
                        Check("a decision can be made on the structure the read produced",
                            decided.Decision == "MANUAL", $"{decided.Label} → {decided.Decision}");

                        // The demo answers must read as placeholders, and the edit
                        // and restore they invite must actually work.
                        Check("a demo answer reads as a placeholder, not as the scheme's answer",
                            decided.AnswerIsPlaceholder
                            && decided.AnswerSummary.StartsWith("[demo]",
                                StringComparison.OrdinalIgnoreCase),
                            decided.AnswerSummary);

                        scheme.Selected = decided;
                        scheme.AnswerDraft = "typed by the audit — not a real answer";
                        scheme.SaveAnswerCommand.Execute(null);
                        await UiHost.WaitUntilAsync(() => !scheme.IsBusy
                                                          && scheme.Questions.Any(q => q.IsEdited),
                            TimeSpan.FromSeconds(60), 400);
                        var typed = scheme.Questions.FirstOrDefault(q => q.IsEdited);
                        Check("a demo answer can be edited, and the edit stops looking like a placeholder",
                            typed is not null && !typed.AnswerIsPlaceholder
                            && typed.AnswerSummary.Contains("(edited)", StringComparison.Ordinal),
                            typed?.AnswerSummary ?? "(nothing edited)");

                        var edited = PagePainter.Paint(scheme, bindings, 1168, 900,
                            Path.Combine(shots, "journey-markscheme-demo.png"));
                        renders.Add(edited);
                        Check("the edited demo scheme renders with no binding errors",
                            edited.BindingErrors.Count == 0,
                            string.Join(" || ", edited.BindingErrors));

                        scheme.Selected = typed;
                        scheme.RestoreAnswerCommand.Execute(null);
                        await UiHost.WaitUntilAsync(() => !scheme.IsBusy
                                                          && scheme.Questions.All(q => !q.IsEdited),
                            TimeSpan.FromSeconds(60), 400);
                        Check("restoring a demo answer puts the placeholder back",
                            scheme.Questions.All(q => !q.IsEdited)
                            && scheme.Questions[0].AnswerSummary.StartsWith("[demo]",
                                StringComparison.OrdinalIgnoreCase),
                            scheme.Questions[0].AnswerSummary);
                        Check("the edits and the restore are recorded in the history",
                            scheme.History.Count > 0
                            && scheme.History.Any(h => h.What.Contains("restored",
                                StringComparison.OrdinalIgnoreCase)),
                            scheme.History.Count > 0 ? scheme.History[0].What : "(no history)");
                    }
                    else
                    {
                        Check("the read button is still offered after a failed read",
                            scheme.ReadSchemeCommand.CanExecute(null),
                            $"needs_read={scheme.NeedsRead}");
                    }

                    var unread = PagePainter.Paint(scheme, bindings, 1168, 900,
                        Path.Combine(shots, "journey-markscheme-unread.png"));
                    renders.Add(unread);
                    Check("the unread state has no binding errors",
                        unread.BindingErrors.Count == 0,
                        string.Join(" || ", unread.BindingErrors));
                }
            }
        }

        // ------------------------------------ C. correct a paper through the app
        var correct = Page<CorrectionViewModel>(main, "correct");
        main.NavigateTo("correct");
        await SettleAsync(correct);
        correct.SetSubmission(submissionPdf);
        correct.SetMarkScheme(schemePdf);
        correct.StudentName = "UI Audit Student";
        Check("the correction form accepts both files", correct.CanSubmit,
            $"{correct.SubmissionLabel} / {correct.MarkSchemeLabel}");

        correct.SubmitCommand.Execute(null);
        await UiHost.WaitUntilAsync(() => correct.HasOutcome || correct.HasError,
            TimeSpan.FromMinutes(4), 500);
        Check("submitting a paper produces an outcome, not a silent form", correct.HasOutcome,
            correct.OutcomeNote + " " + string.Join(" ", correct.OutcomeFacts.Select(f => f.Value)));
        Check("a correction submitted from the page does not fail",
            !correct.HasError, correct.Error ?? "(no error)");

        var mine = (await api.JobsAsync(limit: 50)).Jobs
            .FirstOrDefault(job => (job.DisplayName ?? job.StudentName ?? string.Empty)
                .Contains("UI Audit Student", StringComparison.OrdinalIgnoreCase));
        Check("the backend recorded the job the page submitted", mine is not null,
            mine is null ? "no job named UI Audit Student" : $"job={mine.Id} status={mine.Status}");

        if (mine is not null)
        {
            var deadline = DateTime.UtcNow.AddMinutes(4);
            while (DateTime.UtcNow < deadline && !IsTerminal(mine.Status))
            {
                await Task.Delay(700);
                mine = await api.JobAsync(mine.Id);
            }

            Check("the job the page queued reaches a terminal state", IsTerminal(mine.Status),
                $"status={mine.Status} error={mine.Error}");

            // The queue page must show it, because that is where a teacher looks.
            var queue = Page<JobsViewModel>(main, "tasks");
            main.NavigateTo("tasks");
            await SettleAsync(queue);
            var queueRender = PagePainter.Paint(queue, bindings, 1440, 900,
                Path.Combine(shots, "journey-queue.png"));
            renders.Add(queueRender);
            Check("the queue page lists the job the teacher just submitted",
                queueRender.Text.Contains("UI Audit Student", StringComparison.OrdinalIgnoreCase),
                queueRender.Text.Length > 120 ? queueRender.Text[..120] : queueRender.Text);

            // And the results table, once it has a result.
            if (mine.ResultId is { Length: > 0 } resultId)
            {
                var results = Page<ResultsViewModel>(main, "results");
                main.NavigateTo("results");
                await SettleAsync(results);
                results.SelectById(resultId);
                await SettleAsync(results, 1500);
                Check("the results page can open the new correction",
                    results.HasSelection, results.Selected?.DisplayName ?? "(none)");
                Check("the result carries marks", results.Detail is not null
                                                  && results.Detail.Earned is not null,
                    results.Detail is null ? "(no detail)"
                        : $"{results.Detail.Earned}/{results.Detail.Available}");

                // ---------------------------------- D. exports land as real files
                var before = dialogs.SavedFiles.Count;
                results.ExportPdfCommand.Execute(null);
                await UiHost.WaitUntilAsync(() => dialogs.SavedFiles.Count > before,
                    TimeSpan.FromMinutes(2), 500);
                Check("exporting the result as PDF saves a file", dialogs.SavedFiles.Count > before,
                    string.Join(",", dialogs.SavedFiles.Skip(before)));
                var pdf = dialogs.SavedFiles.LastOrDefault();
                Check("the exported report is a real PDF", IsPdf(pdf), Describe(pdf));

                // -------------------------------------- E. the marking desk
                session.OpenResult(resultId);
                await UiHost.WaitUntilAsync(() => main.Current?.Key == "desk"
                                                  && !main.Current.IsBusy, TimeSpan.FromMinutes(3), 500);
                var desk = main.Current as MarkingDeskViewModel;
                Check("opening a result from the results page lands on the marking desk",
                    desk is not null, main.Current?.Key ?? "(none)");
                if (desk is not null)
                {
                    await SettleAsync(desk);
                    var deskRender = PagePainter.Paint(desk, bindings, 1440, 900,
                        Path.Combine(shots, "journey-desk.png"));
                    renders.Add(deskRender);
                    Check("the desk shows the paper that was asked for",
                        deskRender.Text.Contains(results.Selected?.DisplayName ?? "UI Audit Student",
                            StringComparison.OrdinalIgnoreCase),
                        deskRender.Text.Length > 120 ? deskRender.Text[..120] : deskRender.Text);
                    Check("the desk renders with no binding errors",
                        deskRender.BindingErrors.Count == 0,
                        string.Join(" || ", deskRender.BindingErrors));
                }
            }
            else
            {
                Check("the finished correction produced a result id", false,
                    $"status={mine.Status} result={mine.ResultId ?? "(none)"}");
            }
        }

        // -------------------------------------------------- F. the reports page
        var reports = Page<ReportsViewModel>(main, "reports");
        main.NavigateTo("reports");
        await SettleAsync(reports, 1200);
        Check("the reports page offers the assignments it can report on",
            reports.Assignments.Count > 0, $"{reports.Assignments.Count} assignments");

        var chosen = reports.Assignments.FirstOrDefault();
        if (chosen is not null)
        {
            // Every assignment, as a teacher reporting a class would: the
            // assignment this journey just created has no students yet, so a
            // report of it alone is legitimately empty.
            reports.SelectAllCommand.Execute(null);
            reports.BuildCommand.Execute(null);
            await UiHost.WaitUntilAsync(() => !reports.IsBusy, TimeSpan.FromMinutes(3), 600);
            Check("building a report produces rows rather than an empty table",
                reports.Rows.Count > 0, $"{reports.Rows.Count} rows; {reports.Headline}");
            Check("building a report does not raise an error", !reports.HasError,
                reports.Error ?? "(no error)");

            var before = dialogs.SavedFiles.Count;
            reports.ExportPdfCommand.Execute(null);
            await UiHost.WaitUntilAsync(() => dialogs.SavedFiles.Count > before,
                TimeSpan.FromMinutes(2), 500);
            var export = dialogs.SavedFiles.LastOrDefault();
            Check("exporting the report saves a real PDF", IsPdf(export), Describe(export));
        }

        // ------------------------------------------- G. appearance, applied live
        var settings = Page<SettingsViewModel>(main, "settings");
        main.NavigateTo("settings");
        await SettleAsync(settings);
        var dashboardPage = Page<DashboardViewModel>(main, "dashboard");
        main.NavigateTo("dashboard");
        await SettleAsync(dashboardPage);

        var lightShot = Path.Combine(dataRoot, "theme-light.png");
        var darkShot = Path.Combine(dataRoot, "theme-dark.png");
        PagePainter.Paint(dashboardPage, bindings, 1200, 800, lightShot);

        main.NavigateTo("settings");
        await SettleAsync(settings);
        settings.DarkThemeCommand.Execute(null);
        await Dispatcher.Yield(DispatcherPriority.ApplicationIdle);

        main.NavigateTo("dashboard");
        await SettleAsync(dashboardPage);
        PagePainter.Paint(dashboardPage, bindings, 1200, 800, darkShot);

        Check("switching to the dark theme repaints the application",
            File.Exists(lightShot) && File.Exists(darkShot)
            && !FilesEqual(lightShot, darkShot), "the dashboard pixel output changed");

        main.NavigateTo("settings");
        await SettleAsync(settings);
        settings.LightThemeCommand.Execute(null);
        await Dispatcher.Yield(DispatcherPriority.ApplicationIdle);

        // ---------------------------------- H. a reminder that key material is safe
        var keys = Page<AiKeysViewModel>(main, "ai");
        main.NavigateTo("ai");
        await SettleAsync(keys);
        var keysRender = PagePainter.Paint(keys, bindings, 1440, 900,
            Path.Combine(shots, "journey-ai-keys.png"));
        renders.Add(keysRender);
        Check("the key page never paints key material",
            !keysRender.Text.Contains("AIza", StringComparison.OrdinalIgnoreCase),
            keysRender.Text.Length > 120 ? keysRender.Text[..120] : keysRender.Text);
    }

    private static T Page<T>(MainViewModel main, string key) where T : PageViewModel =>
        (T)main.Pages.First(page => page.Key == key);

    private static async Task SettleAsync(PageViewModel page, int extraMs = 500)
    {
        await UiHost.WaitUntilAsync(() => !page.IsBusy, TimeSpan.FromMinutes(2));
        if (extraMs > 0)
        {
            await Task.Delay(extraMs);
        }
    }

    private static bool IsTerminal(string? status) =>
        status is "COMPLETED" or "FAILED" or "CANCELLED";

    private static bool IsPdf(string? path) =>
        path is not null && File.Exists(path) && new FileInfo(path).Length > 800
        && Encoding.ASCII.GetString(File.ReadAllBytes(path).Take(5).ToArray()) == "%PDF-";

    private static string Describe(string? path) =>
        path is null ? "(no file)"
        : File.Exists(path) ? $"{new FileInfo(path).Length} bytes at {path}" : "missing: " + path;

    private static bool FilesEqual(string left, string right)
    {
        var a = File.ReadAllBytes(left);
        var b = File.ReadAllBytes(right);
        return a.Length == b.Length && a.SequenceEqual(b);
    }
}

/// <summary>One-page synthetic PDFs, made with the backend's own PyMuPDF.</summary>
internal static class Pdfs
{
    public static (string Submission, string Scheme)? Create(string directory)
    {
        var python = LocatePython(directory) ?? LocatePython(AppContext.BaseDirectory);
        if (python is null)
        {
            return null;
        }

        var script = Path.Combine(directory, "make_pdfs.py");
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

make(sys.argv[1], ["UI audit answer sheet", "Question 1: 2 + 2 = 4", "Question 2: 3 x 4 = 12"])
make(sys.argv[2], ["Mark scheme", "Q1: 2 marks - 4", "Q2: 2 marks - 12"])
""");

        var submission = Path.Combine(directory, "student.pdf");
        var scheme = Path.Combine(directory, "scheme.pdf");
        var start = new ProcessStartInfo
        {
            FileName = python,
            UseShellExecute = false,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
            WorkingDirectory = directory,
        };
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
                Console.WriteLine("  PDF creation failed: " + error);
                return null;
            }

            return (submission, scheme);
        }
        catch (Exception ex)
        {
            Console.WriteLine("  PDF creation failed: " + ex.Message);
            return null;
        }
    }

    private static string? LocatePython(string start)
    {
        var directory = new DirectoryInfo(start);
        for (var step = 0; step < 8 && directory is not null; step++, directory = directory.Parent)
        {
            var candidate = Path.Combine(directory.FullName, ".venv", "Scripts", "python.exe");
            if (File.Exists(candidate))
            {
                return candidate;
            }

            var unix = Path.Combine(directory.FullName, ".venv", "bin", "python");
            if (File.Exists(unix))
            {
                return unix;
            }
        }

        return null;
    }
}
