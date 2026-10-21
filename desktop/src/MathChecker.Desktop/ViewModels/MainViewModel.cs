using System.Collections.ObjectModel;
using System.Windows;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Mvvm;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// The window: navigation, backend supervision and the status line. It owns no
/// feature logic; each page view model owns its own workflow.
/// </summary>
public sealed class MainViewModel : ObservableObject
{
    private readonly AppSession _session;
    private readonly Dictionary<string, PageViewModel> _pages = new(StringComparer.OrdinalIgnoreCase);
    private PageViewModel? _current;
    private string _backendSummary = "Starting…";
    private string _backendDetail = string.Empty;
    private bool _backendAvailable;
    private string? _toast;
    private bool _initialized;

    public MainViewModel(AppSession session)
    {
        _session = session;
        _session.Navigate = NavigateTo;
        _session.Toast = message => Toast = message;
        _session.Host.StatusChanged += OnBackendStatusChanged;

        RestartBackendCommand = new AsyncCommand(RestartBackendAsync,
            onError: ex => MessageBox.Show(ex.Message, "Backend"));
        OpenDataFolderCommand = new RelayCommand(() =>
        {
            var folder = _session.Host.Status.DataDirectory;
            if (string.IsNullOrWhiteSpace(folder))
            {
                MessageBox.Show("The backend has not reported its data folder yet.", "Math Checker");
                return;
            }

            _session.Dialogs.OpenFolder(folder!);
        });
        OpenLogsCommand = new RelayCommand(() => _session.Dialogs.OpenFolder(
            Path.GetDirectoryName(_session.Log.LogPath) ?? _session.Log.LogPath));
        OpenSettingsFileCommand = new RelayCommand(() =>
        {
            var path = Core.Configuration.DesktopSettings.DefaultPath;
            if (File.Exists(path))
            {
                _session.Dialogs.OpenExternal(path);
            }
            else
            {
                MessageBox.Show("The desktop settings file is written when the app closes.", "Math Checker");
            }
        });
    }

    public ObservableCollection<PageViewModel> Pages { get; } = new();

    public AsyncCommand RestartBackendCommand { get; }
    public RelayCommand OpenDataFolderCommand { get; }
    public RelayCommand OpenLogsCommand { get; }
    public RelayCommand OpenSettingsFileCommand { get; }

    public PageViewModel? Current
    {
        get => _current;
        set
        {
            var previous = _current;
            if (!SetProperty(ref _current, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasPage));
            previous?.OnDeactivated();
            if (value is not null)
            {
                _session.Settings.LastPage = value.Key;
                value.OnActivated();
                _ = value.LoadAsync();
            }
        }
    }

    public bool HasPage => Current is not null;

    public string BackendSummary
    {
        get => _backendSummary;
        private set => SetProperty(ref _backendSummary, value);
    }

    public string BackendDetail
    {
        get => _backendDetail;
        private set => SetProperty(ref _backendDetail, value);
    }

    public bool BackendAvailable
    {
        get => _backendAvailable;
        private set
        {
            if (SetProperty(ref _backendAvailable, value))
            {
                OnPropertyChanged(nameof(BackendUnavailable));
                OnPropertyChanged(nameof(BackendShort));
            }
        }
    }

    public bool BackendUnavailable => !BackendAvailable;

    public string BackendShort => BackendAvailable ? "Backend ready" : "Backend offline";

    public string? Toast
    {
        get => _toast;
        private set => SetProperty(ref _toast, value);
    }

    public string DataFolder => _session.Host.Status.DataDirectory ?? "resolved by the backend";
    public string LogPath => _session.Log.LogPath;
    public string AppVersion => "1.0.0";

    public async Task InitializeAsync()
    {
        if (_initialized)
        {
            return;
        }

        _initialized = true;
        BuildNavigation();
        UpdateBackend(_session.Host.Status);
        await ConnectAsync().ConfigureAwait(true);
    }

    private async Task ConnectAsync()
    {
        var status = await _session.Host.StartAsync().ConfigureAwait(true);
        UpdateBackend(status);
        if (!status.IsUsable)
        {
            return;
        }

        _session.Api.SetBaseUrl(_session.Host.BaseUrl);
        try
        {
            var auth = await _session.Api.RefreshSessionAsync().ConfigureAwait(true);
            if (auth.LoginRequired && !auth.Authenticated)
            {
                var signedIn = Views.SignInWindow.Prompt(_session, auth);
                if (!signedIn)
                {
                    BackendSummary = "Sign-in required";
                    BackendDetail = "Sign in from the toolbar to use the application.";
                    return;
                }
            }

            await OfferFirstRunSetupAsync().ConfigureAwait(true);
        }
        catch (Exception ex)
        {
            _session.Log.Warn("session check failed: " + ex.Message);
        }

        var target = Pages.FirstOrDefault(p =>
                         string.Equals(p.Key, _session.Settings.LastPage, StringComparison.OrdinalIgnoreCase))
                     ?? Pages.First();
        Current = target;
    }

    private async Task OfferFirstRunSetupAsync()
    {
        try
        {
            // Demo Mode is the "explore without setup" path (it has no real
            // account, keys or student data), exactly as in the web UI.
            var dashboard = await _session.Api.DashboardAsync().ConfigureAwait(true);
            if (dashboard.DemoMode)
            {
                return;
            }

            var setup = await _session.Api.SetupStatusAsync().ConfigureAwait(true);
            if (!setup.FirstRun)
            {
                return;
            }

            var window = new Views.SetupWindow(_session, setup) { Owner = Application.Current.MainWindow };
            window.ShowDialog();
            if (window.KeysRequested)
            {
                NavigateTo("ai");
            }
        }
        catch (Exception ex)
        {
            _session.Log.Warn("setup status unavailable: " + ex.Message);
        }
    }

    private async Task RestartBackendAsync()
    {
        Toast = "Restarting backend…";
        await _session.Host.StopAsync().ConfigureAwait(true);
        await ConnectAsync().ConfigureAwait(true);
    }

    public void NavigateTo(string key)
    {
        var page = Pages.FirstOrDefault(p => string.Equals(p.Key, key, StringComparison.OrdinalIgnoreCase));
        if (page is not null)
        {
            Current = page;
        }
    }

    private void BuildNavigation()
    {
        Add(new DashboardViewModel(_session) { Group = "Desk" });
        Add(new SearchViewModel(_session) { Group = "Desk" });

        Add(new CorrectionViewModel(_session) { Group = "Correct" });
        Add(new JobsViewModel(_session, "tasks", "Tasks", showWorkers: true) { Group = "Correct" });
        Add(new JobsViewModel(_session, "jobs", "Job log", showWorkers: false) { Group = "Correct" });
        Add(new LargePdfViewModel(_session) { Group = "Correct" });
        Add(new AutomationViewModel(_session) { Group = "Correct" });

        Add(new ReviewViewModel(_session) { Group = "Review" });
        Add(new SchemeReviewViewModel(_session));
        Add(new MarkingDeskViewModel(_session) { Group = "Review" });

        Add(new ResultsViewModel(_session) { Group = "Results" });
        Add(new ReportsViewModel(_session) { Group = "Results" });
        Add(new UsageViewModel(_session) { Group = "Results" });

        Add(new ClassroomViewModel(_session) { Group = "Classes" });
        Add(new ClassesViewModel(_session) { Group = "Classes" });
        Add(new StudentsViewModel(_session) { Group = "Classes" });
        Add(new AssignmentsViewModel(_session) { Group = "Classes" });

        Add(new SettingsViewModel(_session) { Group = "System" });
        Add(new AiKeysViewModel(_session) { Group = "System" });
        Add(JsonPageViewModel.Storage(_session));
        Add(JsonPageViewModel.Privacy(_session));
        Add(JsonPageViewModel.Diagnostics(_session));
        Add(new PromptsViewModel(_session) { Group = "System" });
        Add(JsonPageViewModel.Audit(_session));
        Add(JsonPageViewModel.MultiSchool(_session));
        Add(JsonPageViewModel.RemoteAccess(_session));
        Add(JsonPageViewModel.Updates(_session));
    }

    private void Add(PageViewModel page)
    {
        _pages[page.Key] = page;
        Pages.Add(page);
    }

    private void OnBackendStatusChanged(BackendStatus status)
    {
        var dispatcher = Application.Current?.Dispatcher;
        if (dispatcher is not null && !dispatcher.CheckAccess())
        {
            dispatcher.Invoke(() => UpdateBackend(status));
            return;
        }

        UpdateBackend(status);
    }

    private void UpdateBackend(BackendStatus status)
    {
        BackendAvailable = status.IsUsable;
        BackendSummary = status.Summary;
        BackendDetail = string.IsNullOrWhiteSpace(status.Detail) ? status.Summary : status.Detail;
        OnPropertyChanged(nameof(DataFolder));
    }

    public async Task ShutdownAsync()
    {
        _session.Settings.WindowMaximized = Application.Current.MainWindow?.WindowState == WindowState.Maximized;
        try
        {
            _session.Settings.Save();
        }
        catch (Exception ex)
        {
            _session.Log.Warn("settings could not be saved: " + ex.Message);
        }

        await _session.Host.StopAsync().ConfigureAwait(false);
    }
}
