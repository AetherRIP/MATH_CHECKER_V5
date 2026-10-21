using System.Collections.ObjectModel;
using System.Text.Json;
using System.Windows;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One named backend payload shown on a system page.</summary>
public sealed class JsonSectionVm : ObservableObject
{
    private string _text = "Not loaded.";

    public JsonSectionVm(string title, string path)
    {
        Title = title;
        Path = path;
    }

    public string Title { get; }
    public string Path { get; }

    public string Text
    {
        get => _text;
        set => SetProperty(ref _text, value);
    }
}

public sealed class PageAction
{
    public PageAction(string label, Func<Task> run, Func<bool>? canRun = null)
    {
        Label = label;
        Command = new AsyncCommand(run, canRun);
    }

    public string Label { get; }
    public AsyncCommand Command { get; }
}

/// <summary>
/// A real, read-only view of backend payloads plus the actions that belong to
/// that area. Used for the system surfaces whose UI is a list of facts rather
/// than a bespoke table; nothing here fabricates data.
/// </summary>
public class JsonPageViewModel : PageViewModel
{
    private string _description;

    public JsonPageViewModel(AppSession session, string key, string title, string group,
        string description, IEnumerable<(string Title, string Path)> sections)
        : base(session, key, title)
    {
        Group = group;
        _description = description;
        foreach (var (sectionTitle, path) in sections)
        {
            Sections.Add(new JsonSectionVm(sectionTitle, path));
        }
    }

    public ObservableCollection<JsonSectionVm> Sections { get; } = new();
    public ObservableCollection<PageAction> Actions { get; } = new();

    public string Description
    {
        get => _description;
        private set => SetProperty(ref _description, value);
    }

    public RelayCommand CopyCommand => new(() =>
    {
        var text = string.Join(Environment.NewLine + Environment.NewLine,
            Sections.Select(s => $"== {s.Title} =={Environment.NewLine}{s.Text}"));
        try
        {
            Clipboard.SetText(text);
            Status = "Copied to the clipboard.";
        }
        catch (Exception ex)
        {
            Error = "Could not copy: " + ex.Message;
        }
    });

    public void AddAction(string label, Func<Task> run, Func<bool>? canRun = null) =>
        Actions.Add(new PageAction(label, async () =>
        {
            Error = null;
            try
            {
                await run();
                await LoadAsync();
            }
            catch (ApiException ex)
            {
                Error = ex.Display;
            }
            catch (Exception ex)
            {
                Error = ex.Message;
            }
        }, canRun));

    protected override async Task LoadCoreAsync()
    {
        var failures = 0;
        foreach (var section in Sections)
        {
            try
            {
                var payload = await Session.Api.RawAsync(section.Path);
                section.Text = Json.Pretty(payload);
            }
            catch (ApiException ex) when (ex.IsForbidden)
            {
                failures++;
                section.Text = "HTTP 403 — this endpoint is limited to the developer role.\n\n"
                               + ex.Message;
            }
            catch (ApiException ex)
            {
                failures++;
                section.Text = $"HTTP {ex.Status}: {ex.Message} [{ex.Code}]";
            }
            catch (Exception ex)
            {
                failures++;
                section.Text = "Could not load: " + ex.Message;
            }
        }

        if (failures > 0 && failures == Sections.Count)
        {
            Error = "The backend refused every section on this page; the text above carries its reason.";
        }
    }

    // ------------------------------------------------------------- factories

    public static JsonPageViewModel Storage(AppSession session)
    {
        var page = new JsonPageViewModel(session, "storage", "Storage", "System",
            "What is stored, its size, and the versioned student/result export. Secrets are never included.",
            new[] { ("Inventory", "data/inventory") });

        page.AddAction("Export student/result package", async () =>
        {
            var file = await session.Api.DataExportAsync();
            var saved = DownloadSaver.Save(session.Settings, session.Dialogs, file,
                "Math Checker package (*.json)|*.json", "Save data export", openAfter: false);
            if (!string.IsNullOrEmpty(saved))
            {
                session.Notify("Data exported to " + saved);
            }
        });

        page.AddAction("Preview an import", async () =>
        {
            var path = session.Dialogs.PickOpenFile("Select a Math Checker package",
                "Math Checker package (*.json)|*.json|All files (*.*)|*.*");
            if (path is null)
            {
                return;
            }

            var preview = await session.Api.PreviewImportAsync(path);
            page.Sections.Add(new JsonSectionVm("Import preview", "data/import/preview")
            {
                Text = Json.Pretty(preview),
            });
            session.Notify("Import preview loaded; nothing was written.");
        });

        page.AddAction("Import (add new records only)", async () =>
        {
            var path = session.Dialogs.PickOpenFile("Select a Math Checker package",
                "Math Checker package (*.json)|*.json|All files (*.*)|*.*");
            if (path is null)
            {
                return;
            }

            if (!session.Dialogs.Confirm("Import data",
                    "Add new students/results from this package? Existing records are NOT overwritten."))
            {
                return;
            }

            var outcome = await session.Api.ImportAsync(path, "add_only", null);
            page.Sections.Add(new JsonSectionVm("Import result", "data/import") { Text = Json.Pretty(outcome) });
            session.Notify("Import applied.");
        });

        page.AddAction("Open the data folder", () =>
        {
            var folder = session.Host.Status.DataDirectory;
            if (!string.IsNullOrWhiteSpace(folder))
            {
                session.Dialogs.OpenFolder(folder!);
            }
            else
            {
                session.Dialogs.Error("Data folder", "The backend has not reported its data folder yet.");
            }

            return Task.CompletedTask;
        });

        return page;
    }

    public static JsonPageViewModel Privacy(AppSession session)
    {
        var page = new JsonPageViewModel(session, "privacy", "Privacy & data", "System",
            "Where the data lives, and the reset/export controls the backend exposes.",
            new[] { ("Privacy", "privacy/info") });

        page.AddAction("Download diagnostics bundle", async () =>
        {
            var file = await session.Api.DiagnosticExportAsync();
            var saved = DownloadSaver.Save(session.Settings, session.Dialogs, file,
                "Text (*.txt)|*.txt", "Save diagnostics", openAfter: true);
            if (!string.IsNullOrEmpty(saved))
            {
                session.Notify("Diagnostics saved: " + saved);
            }
        });

        page.AddAction("Factory reset (removes local data)", async () =>
        {
            var typed = session.Dialogs.AskText("Factory reset",
                "This removes the local database, reports and settings held by the backend. "
                + "Type RESET to confirm:");
            if (typed is null)
            {
                return;
            }

            if (typed != "RESET")
            {
                session.Dialogs.Error("Factory reset", "Nothing was deleted: the confirmation did not say RESET.");
                return;
            }

            var payload = await session.Api.FactoryResetAsync("RESET");
            page.Sections.Add(new JsonSectionVm("Factory reset", "privacy/factory-reset")
            {
                Text = Json.Pretty(payload),
            });
            session.Notify("Factory reset completed.");
        });

        return page;
    }

    public static JsonPageViewModel Diagnostics(AppSession session)
    {
        var page = new JsonPageViewModel(session, "diagnostics", "Diagnostics", "System",
            "Developer-only endpoints. A 403 here means the signed-in role is not a developer.",
            new[]
            {
                ("Diagnostics", "diagnostics"),
                ("Logs", "diagnostics/logs"),
                ("Database", "diagnostics/db"),
                ("Self check", "diagnostics/selfcheck"),
            });

        page.AddAction("Clear backend logs", async () =>
        {
            if (!session.Dialogs.Confirm("Clear logs", "Clear the backend's log file?"))
            {
                return;
            }

            await session.Api.ClearDiagnosticLogsAsync();
            session.Notify("Backend logs cleared.");
        });

        return page;
    }

    public static JsonPageViewModel Audit(AppSession session) =>
        new(session, "audit", "Accuracy audit", "System",
            "Read-only audit of recorded corrections. Nothing here changes a result.",
            new[]
            {
                ("Audit summary", "audit/summary"),
                ("Audit reports", "audit/reports"),
            });

    public static JsonPageViewModel MultiSchool(AppSession session) =>
        new(session, "distributed", "Multi-school", "System",
            "The distributed layer: accounts, schools, devices, teams and workers.",
            new[]
            {
                ("Capabilities", "distributed/capabilities"),
                ("Schools", "distributed/schools"),
                ("Accounts", "distributed/accounts"),
                ("Teams", "distributed/teams"),
                ("Devices", "distributed/devices"),
                ("Workers", "distributed/workers"),
            });

    public static JsonPageViewModel RemoteAccess(AppSession session)
    {
        var page = new JsonPageViewModel(session, "remote", "Remote access", "System",
            "Whether this desk is reachable from outside the machine, and the address the backend publishes.",
            new[]
            {
                ("Remote status", "remote/status"),
                ("Access state", "access/state"),
                ("Public address", "access/public-address"),
            });

        page.AddAction("Enable public address", async () =>
        {
            if (!session.Dialogs.Confirm("Publish the backend",
                    "Make this backend reachable through the configured tunnel? Anyone with the address "
                    + "can reach the sign-in page while it is enabled."))
            {
                return;
            }

            var payload = await session.Api.SetPublicAddressAsync(true);
            page.Sections.Add(new JsonSectionVm("Change result", "access/public-address")
            {
                Text = Json.Pretty(payload),
            });
            session.Notify("Public address enabled.");
        });

        page.AddAction("Disable public address", async () =>
        {
            var payload = await session.Api.SetPublicAddressAsync(false);
            page.Sections.Add(new JsonSectionVm("Change result", "access/public-address")
            {
                Text = Json.Pretty(payload),
            });
            session.Notify("Public address disabled.");
        });

        return page;
    }

    public static JsonPageViewModel Updates(AppSession session) =>
        new(session, "updates", "Updates", "System",
            "Release records and installations from the distributed layer (developer view).",
            new[]
            {
                ("Overview", "distributed/releases/overview"),
                ("Releases", "distributed/releases"),
            });
}
