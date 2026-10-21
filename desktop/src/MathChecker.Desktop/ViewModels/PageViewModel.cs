using System.Windows.Input;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
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

    public AsyncCommand RefreshCommand { get; }

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
            Error = "The backend is not reachable: " + ex.Message;
        }
        catch (TaskCanceledException)
        {
            Error = "The backend did not answer in time. It may still be working; refresh to check.";
        }
        catch (Exception ex)
        {
            Session.Log.Error($"page {Key} failed to load", ex);
            Error = ex.Message;
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

    /// <summary>Runs a user action with the same busy/error treatment as a load.</summary>
    protected async Task RunAsync(Func<Task> action, string? successMessage = null)
    {
        if (IsBusy)
        {
            return;
        }

        IsBusy = true;
        Error = null;
        try
        {
            await action().ConfigureAwait(true);
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
            Error = "The backend is not reachable: " + ex.Message;
        }
        catch (Exception ex)
        {
            Session.Log.Error($"action on {Key} failed", ex);
            Error = ex.Message;
        }
        finally
        {
            IsBusy = false;
        }
    }

    private void HandleError(Exception ex) => Error = ex is ApiException api ? api.Display : ex.Message;

    protected AsyncCommand Command(Func<Task> action, Func<bool>? canExecute = null, string? success = null) =>
        new(() => RunAsync(action, success), canExecute, HandleError);

    protected AsyncCommand Command(Func<object?, Task> action, Func<object?, bool>? canExecute = null,
        string? success = null) =>
        new(async parameter => await RunAsync(() => action(parameter), success), canExecute, HandleError);

    protected RelayCommand Sync(Action action, Func<bool>? canExecute = null) => new(action, canExecute);

    /// <summary>Convenience for command bindings that need to invoke a page.</summary>
    public ICommand Refresh => RefreshCommand;
}
