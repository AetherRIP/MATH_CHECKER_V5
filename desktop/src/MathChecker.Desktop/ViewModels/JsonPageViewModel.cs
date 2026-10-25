using System.Collections.ObjectModel;
using System.Text;
using System.Text.Json;
using System.Windows;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One named backend payload shown on a system page: a sentence,
/// labelled facts, and the raw payload kept for technical details only.</summary>
public sealed class JsonSectionVm : ObservableObject
{
    private string _notice = "Not loaded yet.";
    private string _raw = string.Empty;
    private bool _isUnavailable;

    public JsonSectionVm(string title, string path)
    {
        Title = title;
        Path = path;
    }

    public string Title { get; }

    /// <summary>The API route. Only ever rendered inside technical details.</summary>
    public string Path { get; }

    public ObservableCollection<FactRow> Facts { get; } = new();

    /// <summary>A plain sentence: what this section says, or why it is closed.</summary>
    public string Notice
    {
        get => _notice;
        set
        {
            if (SetProperty(ref _notice, value))
            {
                OnPropertyChanged(nameof(HasNotice));
            }
        }
    }

    /// <summary>The raw response, shown only when the user opens technical details.</summary>
    public string Raw
    {
        get => _raw;
        set
        {
            if (SetProperty(ref _raw, value))
            {
                OnPropertyChanged(nameof(HasRaw));
            }
        }
    }

    /// <summary>True when the backend refuses this area for the signed-in
    /// role; that is a normal state, not a failure of the page.</summary>
    public bool IsUnavailable
    {
        get => _isUnavailable;
        set
        {
            if (SetProperty(ref _isUnavailable, value))
            {
                OnPropertyChanged(nameof(IsAvailable));
            }
        }
    }

    public bool IsAvailable => !IsUnavailable;

    public bool HasFacts => Facts.Count > 0;

    public bool HasRaw => !string.IsNullOrWhiteSpace(Raw);

    public bool HasNotice => !string.IsNullOrWhiteSpace(Notice);

    public void Fill(JsonElement payload)
    {
        Facts.Clear();
        foreach (var fact in PayloadFacts.Of(payload))
        {
            Facts.Add(fact);
        }

        Raw = PayloadFacts.Raw(payload);
        var summary = PayloadFacts.Summarize(payload);
        Notice = string.IsNullOrWhiteSpace(summary)
            ? (Facts.Count == 1 ? "1 value reported." : $"{Facts.Count} values reported.")
            : summary;
        IsUnavailable = false;
        OnPropertyChanged(nameof(HasFacts));
    }

    public void Close(string reason, string? technical = null)
    {
        Facts.Clear();
        OnPropertyChanged(nameof(HasFacts));
        IsUnavailable = true;
        Notice = reason;
        Raw = technical ?? string.Empty;
    }

    public void Fail(string reason, string? technical = null)
    {
        Facts.Clear();
        OnPropertyChanged(nameof(HasFacts));
        IsUnavailable = false;
        Notice = reason;
        Raw = technical ?? string.Empty;
    }

    /// <summary>Builds a section from a payload the page just produced (an
    /// import preview or a change result), so it renders like every other one.</summary>
    public static JsonSectionVm FromPayload(string title, string path, JsonElement payload)
    {
        var section = new JsonSectionVm(title, path);
        section.Fill(payload);
        return section;
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
    private string _notice = string.Empty;
    private readonly string _unavailableHelp;

    public JsonPageViewModel(AppSession session, string key, string title, string group,
        string description, IEnumerable<(string Title, string Path)> sections,
        string? unavailableHelp = null)
        : base(session, key, title)
    {
        Group = group;
        _description = description;
        _unavailableHelp = unavailableHelp
                           ?? "This area is available to the developer role only on this installation. "
                           + "Nothing else in the application is affected.";
        foreach (var (sectionTitle, path) in sections)
        {
            Sections.Add(new JsonSectionVm(sectionTitle, path));
        }
    }

    /// <summary>A page-level sentence shown when the whole page is closed to
    /// the signed-in role. It replaces the old red "refused every section"
    /// banner, which explained nothing to a teacher.</summary>
    public string Notice
    {
        get => _notice;
        private set
        {
            if (SetProperty(ref _notice, value))
            {
                OnPropertyChanged(nameof(HasNotice));
            }
        }
    }

    public bool HasNotice => !string.IsNullOrWhiteSpace(Notice);

    public ObservableCollection<JsonSectionVm> Sections { get; } = new();
    public ObservableCollection<PageAction> Actions { get; } = new();

    public string Description
    {
        get => _description;
        private set => SetProperty(ref _description, value);
    }

    public RelayCommand CopyCommand => new(() =>
    {
        var builder = new StringBuilder();
        foreach (var section in Sections)
        {
            builder.AppendLine($"== {section.Title} ==");
            if (!string.IsNullOrWhiteSpace(section.Notice))
            {
                builder.AppendLine(section.Notice);
            }

            foreach (var fact in section.Facts)
            {
                builder.AppendLine($"{fact.Label}: {fact.Value}");
            }

            if (section.HasRaw)
            {
                builder.AppendLine();
                builder.AppendLine(section.Raw);
            }

            builder.AppendLine();
        }

        try
        {
            Clipboard.SetText(builder.ToString().TrimEnd());
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
                Session.Log.Error("page action failed", ex);
                Error = UserMessage.For(ex);
            }
        }, canRun));

    protected override async Task LoadCoreAsync()
    {
        var closed = 0;
        var failed = 0;
        Notice = string.Empty;
        foreach (var section in Sections)
        {
            try
            {
                var payload = await Session.Api.RawAsync(section.Path);
                section.Fill(payload);
            }
            catch (ApiException ex) when (ex.IsForbidden || ex.IsUnauthorized)
            {
                // A closed area is a state, not an error: the message below
                // says what it means instead of printing an HTTP code.
                closed++;
                section.Close(WhyClosed(ex), ex.Technical);
            }
            catch (ApiException ex)
            {
                failed++;
                section.Fail(ex.Display, ex.Technical);
            }
            catch (Exception ex)
            {
                failed++;
                Session.Log.Error($"section {section.Title} failed to load", ex);
                section.Fail("This information could not be read from the backend. Refresh to try again.",
                    ex.GetType().Name + ": " + ex.Message);
            }
        }

        if (closed == Sections.Count)
        {
            Notice = _unavailableHelp;
        }
        else if (failed == Sections.Count)
        {
            Error = "The backend could not answer this page. Use Backend → Restart backend, then refresh.";
        }
    }

    private static string WhyClosed(ApiException ex) => ex.IsForbidden
        ? "Not available for the signed-in role."
        : "Not enabled on this installation.";

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
            page.Sections.Add(JsonSectionVm.FromPayload("Import preview", "data/import/preview", preview));
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
            page.Sections.Add(JsonSectionVm.FromPayload("Import result", "data/import", outcome));
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
            page.Sections.Add(JsonSectionVm.FromPayload("Factory reset", "privacy/factory-reset", payload));
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
            },
            "Diagnostics are open to the developer role only. Teachers do not need them: "
            + "the rest of the application keeps working exactly as it does now.");

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
            },
            "The multi-school layer is not enabled on this installation, so accounts, teams and "
            + "sync across schools are switched off. Corrections, Classroom and reports are unaffected.");

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
            page.Sections.Add(JsonSectionVm.FromPayload("Change result", "access/public-address", payload));
            session.Notify("Public address enabled.");
        });

        page.AddAction("Disable public address", async () =>
        {
            var payload = await session.Api.SetPublicAddressAsync(false);
            page.Sections.Add(JsonSectionVm.FromPayload("Change result", "access/public-address", payload));
            session.Notify("Public address disabled.");
        });

        return page;
    }

    public static JsonPageViewModel Updates(AppSession session) =>
        new(session, "updates", "Updates", "System",
            "Release records and installations for the distributed layer.",
            new[]
            {
                ("Overview", "distributed/releases/overview"),
                ("Releases", "distributed/releases"),
            },
            "Automatic updates need the multi-school layer, which is not enabled on this installation. "
            + "The application itself stays on the version shown in the rail.");
}
