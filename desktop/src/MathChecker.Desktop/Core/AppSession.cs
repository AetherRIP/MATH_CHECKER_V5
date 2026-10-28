using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Core.Sync;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Core;

/// <summary>
/// Everything a page needs, passed explicitly instead of reached through
/// statics. Views never touch HTTP or the process: their view models use the
/// API client and the backend host from here.
/// </summary>
public sealed class AppSession
{
    public AppSession(DesktopSettings settings, IFileLog log, BackendHost host,
        MathCheckerApiClient api, IDialogService dialogs)
    {
        Settings = settings;
        Log = log;
        Host = host;
        Api = api;
        Dialogs = dialogs;
        Sync = new ClassroomSyncService(this);
    }

    public DesktopSettings Settings { get; }
    public IFileLog Log { get; }
    public BackendHost Host { get; }
    public MathCheckerApiClient Api { get; }
    public IDialogService Dialogs { get; }

    /// <summary>The one synchronization path every page's Sync button uses.</summary>
    public ClassroomSyncService Sync { get; }

    /// <summary>Set by the shell: navigates to a page key.</summary>
    public Action<string>? Navigate { get; set; }

    /// <summary>Set by the shell: shown in the status bar.</summary>
    public Action<string>? Toast { get; set; }

    private string? _pendingResultId;
    private string? _pendingAssignmentId;
    private string? _pendingSchemeAssignmentId;

    /// <summary>
    /// Opens one correction in the marking desk. Navigation is a page key, so
    /// the identity travels beside it and is consumed by the page's load —
    /// that is what stops "open this paper" from landing on an unrelated one.
    /// </summary>
    public void OpenResult(string resultId)
    {
        _pendingResultId = resultId;
        GoTo("desk");
    }

    /// <summary>The result the next desk load should show, if any.</summary>
    public string? TakePendingResultId()
    {
        var resultId = _pendingResultId;
        _pendingResultId = null;
        return resultId;
    }

    /// <summary>
    /// Opens one assignment's details page. The identity travels the same way
    /// as a result id: the page key is the destination and the identity is
    /// consumed by that page's load, so "open this assignment" can never land
    /// on another.
    /// </summary>
    public void OpenAssignment(string courseworkId)
    {
        _pendingAssignmentId = courseworkId;
        GoTo("assignment");
    }

    /// <summary>
    /// Opens one assignment's Mark scheme page. Kept separate from the details
    /// page's identity: both can be in flight, and the scheme page must open on
    /// the assignment the teacher clicked even if the details page has already
    /// consumed its own id.
    /// </summary>
    public void OpenMarkScheme(string courseworkId)
    {
        _pendingSchemeAssignmentId = courseworkId;
        GoTo("markscheme");
    }

    /// <summary>The assignment the next Mark scheme load should show, if any.</summary>
    public string? TakePendingSchemeAssignment()
    {
        var id = _pendingSchemeAssignmentId;
        _pendingSchemeAssignmentId = null;
        return id;
    }

    /// <summary>The assignment the next details load should show, if any.</summary>
    public string? TakePendingAssignmentId()
    {
        var id = _pendingAssignmentId;
        _pendingAssignmentId = null;
        return id;
    }

    public void GoTo(string pageKey) => Navigate?.Invoke(pageKey);

    public void Notify(string message)
    {
        Log.Info("ui: " + message);
        Toast?.Invoke(message);
    }
}

public interface IDialogService
{
    bool Confirm(string title, string message);
    void Info(string title, string message);
    void Error(string title, string message);
    string? PickOpenFile(string title, string filter);
    string? PickSaveFile(string title, string filter, string defaultName);
    string? AskText(string title, string message, string initial = "");
    void OpenExternal(string pathOrUrl);
    void OpenFolder(string directory);
}

/// <summary>WPF implementation of the dialog surface used by view models.</summary>
public sealed class DialogService : IDialogService
{
    public bool Confirm(string title, string message) =>
        Views.MessageWindow.Confirm(title, message);

    public void Info(string title, string message) =>
        Views.MessageWindow.Show(title, message, Views.MessageKind.Info);

    public void Error(string title, string message) =>
        Views.MessageWindow.Show(title, message, Views.MessageKind.Error);

    public string? PickOpenFile(string title, string filter)
    {
        var dialog = new Microsoft.Win32.OpenFileDialog { Title = title, Filter = filter, CheckFileExists = true };
        return dialog.ShowDialog() == true ? dialog.FileName : null;
    }

    public string? PickSaveFile(string title, string filter, string defaultName)
    {
        var dialog = new Microsoft.Win32.SaveFileDialog
        {
            Title = title,
            Filter = filter,
            FileName = defaultName,
            OverwritePrompt = true,
        };
        return dialog.ShowDialog() == true ? dialog.FileName : null;
    }

    public string? AskText(string title, string message, string initial = "")
    {
        var window = new Views.TextPromptWindow(title, message, initial);
        return window.ShowDialog() == true ? window.Value : null;
    }

    public void OpenExternal(string pathOrUrl)
    {
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = pathOrUrl,
                UseShellExecute = true,
            });
        }
        catch (Exception ex)
        {
            Error("Could not open", ex.Message);
        }
    }

    public void OpenFolder(string directory)
    {
        if (Directory.Exists(directory))
        {
            OpenExternal(directory);
        }
        else
        {
            Error("Folder not found", directory);
        }
    }
}

/// <summary>Writes a downloaded file where the user asked and optionally opens it.</summary>
public static class DownloadSaver
{
    public static string Save(DesktopSettings settings, IDialogService dialogs, DownloadFile file,
        string filter, string title, bool openAfter)
    {
        var directory = settings.LastExportDirectory;
        if (string.IsNullOrWhiteSpace(directory) || !Directory.Exists(directory))
        {
            directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "MathChecker");
        }

        Directory.CreateDirectory(directory);
        var target = dialogs.PickSaveFile(title, filter, Path.Combine(directory, file.FileName));
        if (target is null)
        {
            return string.Empty;
        }

        File.WriteAllBytes(target, file.Bytes);
        settings.LastExportDirectory = Path.GetDirectoryName(target);
        settings.Save();
        if (openAfter)
        {
            dialogs.OpenExternal(target);
        }

        return target;
    }
}
