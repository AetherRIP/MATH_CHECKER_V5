using System.Windows.Input;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Sync;
using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Core.UI;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// Shared page behaviour: one loading flag, one honest error string, one
/// refresh command. Every page that can fail shows the backend's message
/// instead of pretending the call succeeded.
/// </summary>
public abstract class PageViewModel : ObservableObject
{
    private bool _isBusy;
    private string? _error;
    private string? _status;

    protected PageViewModel(AppSession session, string key, string title)
    {
        Session = session;
        Key = key;
        Title = title;
        RefreshCommand = new AsyncCommand(LoadAsync, onError: HandleError);
    }

    public AppSession Session { get; }
    public string Key { get; }
    public string Title { get; }

    /// <summary>Segoe MDL2 glyph shown in the navigation rail.</summary>
    public string NavGlyph => PageIcons.For(Key);

    /// <summary>Screen readers and UI Automation take a list item's name from
    /// ToString(); without this the accessibility tree exposes the class name.</summary>
    public override string ToString() => Title;

    /// <summary>Navigation group shown in the sidebar (Desk, Correct, …).</summary>
    public string Group { get; set; } = "Desk";

    /// <summary>Space label printed above the page title.</summary>
    public string Eyebrow => Group.ToUpperInvariant();

    public AsyncCommand RefreshCommand { get; }

    /// <summary>True for pages whose rows are read from Google Classroom. Their
    /// view passes <c>Sync="{Binding SyncService}"</c> to the page frame, which
    /// renders the ONE shared Sync control; pages with no synchronized data
    /// leave it false and show no button at all.</summary>
    public virtual bool SyncsFromClassroom => false;

    /// <summary>The shared synchronization service, or null on pages that have
    /// nothing to synchronize.</summary>
    public ClassroomSyncService? SyncService => SyncsFromClassroom ? Session.Sync : null;

    public bool IsBusy
    {
        get => _isBusy;
        private set
        {
            if (SetProperty(ref _isBusy, value))
            {
                OnPropertyChanged(nameof(IsIdle));
            }
        }
    }

    public bool IsIdle => !IsBusy;

    public string? Error
    {
        get => _error;
        protected set
        {
            if (SetProperty(ref _error, value))
            {
                OnPropertyChanged(nameof(HasError));
            }
        }
    }

    public bool HasError => !string.IsNullOrWhiteSpace(Error);

    /// <summary>Short, honest progress or completion text. Settable so a view's
    /// code-behind can report a drag-and-drop decision.</summary>
    public string? Status
    {
        get => _status;
        set => SetProperty(ref _status, value);
    }

    public DateTime? LastLoaded { get; private set; }

    /// <summary>Runs the page load. Navigation calls this; the Refresh button
    /// calls the same method, so there is one code path.</summary>
    public async Task LoadAsync()
    {
        if (IsBusy)
        {
            return;
        }

        IsBusy = true;
        Error = null;
        try
        {
            if (!Session.Host.Status.IsUsable)
            {
                Error = Session.Host.Status.Detail;
                return;
            }

            await LoadCoreAsync().ConfigureAwait(true);
            LastLoaded = DateTime.Now;
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
        catch (HttpRequestException ex)
        {
            Session.Log.Warn($"page {Key} could not reach the backend: {ex.Message}");
            Error = UserMessage.For(ex);
        }
        catch (TaskCanceledException)
        {
            Error = UserMessage.For(new TimeoutException());
        }
        catch (Exception ex)
        {
            Session.Log.Error($"page {Key} failed to load", ex);
            Error = UserMessage.For(ex);
        }
        finally
        {
            IsBusy = false;
        }
    }

    protected abstract Task LoadCoreAsync();

    /// <summary>Called by the shell when this page becomes visible. Pages that
    /// show live state start their timer/stream here.</summary>
    public virtual void OnActivated()
    {
    }

    /// <summary>Called when the user navigates away; stop timers and streams.</summary>
    public virtual void OnDeactivated()
    {
    }

    /// <summary>Runs a user action with the same busy/error treatment as a load.
    ///
    /// <paramref name="after"/> runs when the action SUCCEEDED, once the busy
    /// window has CLOSED. A page cannot reload itself from inside its own action:
    /// <see cref="LoadAsync"/> returns immediately while the page is busy, so an
    /// action that ended with <c>await LoadAsync()</c> silently kept showing the
    /// state from before it ran (a key test left the health counters stale until
    /// Refresh was pressed).
    ///
    /// A failed action does not refresh: <see cref="LoadAsync"/> clears
    /// <see cref="Error"/>, so reloading after a refusal wiped the backend's
    /// explanation straight back off the screen and left the failure looking like
    /// a success.</summary>
    protected async Task RunAsync(Func<Task> action, string? successMessage = null,
        Func<Task>? after = null)
    {
        if (IsBusy)
        {
            return;
        }

        IsBusy = true;
        Error = null;
        var succeeded = false;
        try
        {
            await action().ConfigureAwait(true);
            succeeded = true;
            if (!string.IsNullOrWhiteSpace(successMessage))
            {
                Status = successMessage;
                Session.Notify(successMessage!);
            }
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
        catch (HttpRequestException ex)
        {
            Session.Log.Warn($"action on {Key} could not reach the backend: {ex.Message}");
            Error = UserMessage.For(ex);
        }
        catch (Exception ex)
        {
            Session.Log.Error($"action on {Key} failed", ex);
            Error = UserMessage.For(ex);
        }
        finally
        {
            IsBusy = false;
        }

        if (succeeded && after is not null)
        {
            await after().ConfigureAwait(true);
        }
    }

    private void HandleError(Exception ex)
    {
        Session.Log.Error($"action on {Key} failed", ex);
        Error = UserMessage.For(ex);
    }

    protected AsyncCommand Command(Func<Task> action, Func<bool>? canExecute = null,
        string? success = null, Func<Task>? after = null) =>
        new(() => RunAsync(action, success, after), canExecute, HandleError);

    /// <summary>An action that changes backend state and then reloads the page.
    ///
    /// A command body CANNOT reload itself: <see cref="RunAsync"/> holds
    /// <see cref="IsBusy"/> for the whole action, and <see cref="LoadAsync"/> returns
    /// immediately while a page is busy. So <c>await LoadAsync()</c> inside a
    /// command was dead code, and every page built that way kept showing the rows
    /// from before the action — a cancelled job still read "running", an accepted
    /// result still sat in the review queue, an uploaded mark scheme still showed
    /// "not attached", all until Refresh was pressed by hand.
    ///
    /// The reload belongs here, after the busy window closes. <paramref name="status"/>
    /// is set AFTER the reload so the page's own load-time status cannot overwrite
    /// the confirmation the user needs to see.
    /// </summary>
    protected AsyncCommand CommandWithReload(Func<Task> action, string? status = null,
        Func<Task>? also = null, Func<bool>? canExecute = null) =>
        Command(action, canExecute, after: async () =>
        {
            if (also is not null)
            {
                await also().ConfigureAwait(true);
            }

            await LoadAsync().ConfigureAwait(true);
            if (!string.IsNullOrWhiteSpace(status))
            {
                Status = status;
            }
        });

    protected AsyncCommand Command(Func<object?, Task> action, Func<object?, bool>? canExecute = null,
        string? success = null) =>
        new(async parameter => await RunAsync(() => action(parameter), success), canExecute, HandleError);

    protected RelayCommand Sync(Action action, Func<bool>? canExecute = null) => new(action, canExecute);

    /// <summary>Convenience for command bindings that need to invoke a page.</summary>
    public ICommand Refresh => RefreshCommand;
}
