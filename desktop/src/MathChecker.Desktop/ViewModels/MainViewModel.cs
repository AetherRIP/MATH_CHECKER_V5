using System.Collections.ObjectModel;
using System.Windows;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Core.Sync;
using MathChecker.Desktop.Core.UI;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One navigation space: a named group of pages with its own rail
/// entry and tab strip. Spaces are derived from the pages themselves, so a new
/// page never needs a second navigation registration.</summary>
public sealed class SpaceVm
{
    public SpaceVm(string name)
    {
        Name = name;
        Pages = new ObservableCollection<PageViewModel>();
    }

    public string Name { get; }
    public string Glyph => PageIcons.ForSpace(Name);
    public ObservableCollection<PageViewModel> Pages { get; }
    public int Count => Pages.Count;

    // The shared rail template binds Title/NavGlyph; spaces expose the same
    // shape so one template serves every navigation level.
    public string Title => Name;
    public string NavGlyph => Glyph;

    public override string ToString() => Name;
}

/// <summary>
/// The window: navigation, backend supervision and the status line. It owns no
/// feature logic; each page view model owns its own workflow.
/// </summary>
public sealed class MainViewModel : ObservableObject
{
    private readonly AppSession _session;
    private readonly Dictionary<string, PageViewModel> _pages = new(StringComparer.OrdinalIgnoreCase);
    private PageViewModel? _current;
    private SpaceVm? _currentSpace;
    private string _backendSummary = "Starting…";
    private string _backendDetail = string.Empty;
    private bool _backendAvailable;
    private string _backendWarning = string.Empty;
    private string? _toast;
    private bool _initialized;
    private bool _railExpanded;
    private bool _paletteOpen;
    private string _paletteQuery = string.Empty;
    private PageViewModel? _paletteSelection;

    public MainViewModel(AppSession session)
    {
        _session = session;
        _session.Navigate = NavigateTo;
        _session.Toast = message => Toast = message;
        _session.Host.StatusChanged += OnBackendStatusChanged;
        _session.Sync.Completed += OnSyncCompleted;

        _railExpanded = !session.Settings.RailCollapsed;

        RestartBackendCommand = new AsyncCommand(RestartBackendAsync,
            onError: ex => MessageBox.Show(ex.Message, "Backend"));
        ToggleRailCommand = new RelayCommand(ToggleRail);
        TogglePaletteCommand = new RelayCommand(TogglePalette);
        ClosePaletteCommand = new RelayCommand(ClosePalette);
        ExitCommand = new RelayCommand(() => ExitRequested?.Invoke());
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

    /// <summary>The seven spaces in navigation order.</summary>
    public ObservableCollection<SpaceVm> Spaces { get; } = new();

    /// <summary>Raised by the File menu; the window owns the shutdown sequence.</summary>
    public event Action? ExitRequested;

    /// <summary>A sync reached its final state: say what happened, then reload
    /// the page whose rows came from Classroom. Only the visible page is
    /// reloaded — every other page re-reads on navigation anyway.</summary>
    private void OnSyncCompleted(SyncResult result)
    {
        _session.Notify("Google Classroom — " + result.Summary);
        if (Current is { SyncsFromClassroom: true } page)
        {
            _ = page.LoadAsync();
        }
    }

    public AsyncCommand RestartBackendCommand { get; }
    public RelayCommand ToggleRailCommand { get; }
    public RelayCommand TogglePaletteCommand { get; }
    public RelayCommand ClosePaletteCommand { get; }
    public RelayCommand OpenDataFolderCommand { get; }
    public RelayCommand OpenLogsCommand { get; }
    public RelayCommand OpenSettingsFileCommand { get; }
    public RelayCommand ExitCommand { get; }

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
            if (value is null)
            {
                return;
            }

            _session.Settings.LastPage = value.Key;
            SyncSpace(value.Group);
            value.OnActivated();
            _ = value.LoadAsync();
        }
    }

    /// <summary>The space that owns the visible page.</summary>
    public SpaceVm? CurrentSpace
    {
        get => _currentSpace;
        set
        {
            if (!SetProperty(ref _currentSpace, value))
            {
                return;
            }

            OnPropertyChanged(nameof(SpacePages));
            OnPropertyChanged(nameof(SpaceName));

            if (value is null || value.Count == 0)
            {
                return;
            }

            // A rail entry stands for a destination, not a memory: clicking
            // People must open Classroom, never whichever tab happened to be
            // last used in that space (that read as "it opens Classes").
            if (Current is null || Current.Group != value.Name)
            {
                Current = value.Pages[0];
            }
        }
    }

    /// <summary>Pages shown in the space tab strip.</summary>
    public ObservableCollection<PageViewModel>? SpacePages => _currentSpace?.Pages;

    public string SpaceName => _currentSpace?.Name ?? string.Empty;

    public bool HasPage => Current is not null;

    public bool HasSpaceTabs => _currentSpace is not null && _currentSpace.Count > 1;

    public bool IsRailExpanded
    {
        get => _railExpanded;
        private set
        {
            if (!SetProperty(ref _railExpanded, value))
            {
                return;
            }

            OnPropertyChanged(nameof(IsRailCollapsed));
            OnPropertyChanged(nameof(RailWidth));
            _session.Settings.RailCollapsed = !value;
        }
    }

    public bool IsRailCollapsed => !_railExpanded;

    /// <summary>220 DIP expanded, 68 DIP icon rail.</summary>
    public double RailWidth => _railExpanded ? 220 : 68;

    public string RailToggleGlyph => _railExpanded ? "\uE76B" : "\uE76C";

    public bool IsPaletteOpen
    {
        get => _paletteOpen;
        private set
        {
            if (!SetProperty(ref _paletteOpen, value))
            {
                return;
            }

            OnPropertyChanged(nameof(PaletteHint));
        }
    }

    public string PaletteQuery
    {
        get => _paletteQuery;
        set
        {
            if (SetProperty(ref _paletteQuery, value))
            {
                RefreshPalette();
            }
        }
    }

    public ObservableCollection<PageViewModel> PaletteResults { get; } = new();

    public PageViewModel? PaletteSelection
    {
        get => _paletteSelection;
        set => SetProperty(ref _paletteSelection, value);
    }

    public bool PaletteEmpty => PaletteResults.Count == 0;

    public string PaletteHint => $"{PaletteResults.Count} page{(PaletteResults.Count == 1 ? string.Empty : "s")}";

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

    /// <summary>A visible environment mismatch: attached to a DEMO backend, to
    /// real data while configured for demo, or to another data folder.</summary>
    public string BackendWarning
    {
        get => _backendWarning;
        private set
        {
            if (SetProperty(ref _backendWarning, value))
            {
                OnPropertyChanged(nameof(HasBackendWarning));
                OnPropertyChanged(nameof(BackendShort));
            }
        }
    }

    public bool HasBackendWarning => !string.IsNullOrWhiteSpace(BackendWarning);

    public string BackendShort => !BackendAvailable ? "Backend offline"
        : HasBackendWarning ? "Backend — check data source"
        : "Backend ready";

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

        ClosePalette();
    }

    // ------------------------------------------------------------- navigation

    private void BuildNavigation()
    {
        Add(new DashboardViewModel(_session) { Group = "Desk" });
        Add(new SearchViewModel(_session) { Group = "Desk" });

        Add(new CorrectionViewModel(_session) { Group = "Correct" });
        Add(new WorkersViewModel(_session) { Group = "Correct" });
        Add(new JobsViewModel(_session, "tasks", "Tasks", showWorkers: true) { Group = "Correct" });
        Add(new JobsViewModel(_session, "jobs", "Job log", showWorkers: false) { Group = "Correct" });
        Add(new AutomationViewModel(_session) { Group = "Correct" });

        Add(new ReviewViewModel(_session) { Group = "Review" });
        Add(new SchemeReviewViewModel(_session) { Group = "Review" });
        Add(new MarkingDeskViewModel(_session) { Group = "Review" });

        Add(new ResultsViewModel(_session) { Group = "Results" });
        Add(new ReportsViewModel(_session) { Group = "Results" });
        Add(new UsageViewModel(_session) { Group = "Results" });

        Add(new ClassroomViewModel(_session) { Group = "People" });
        Add(new ClassesViewModel(_session) { Group = "People" });
        Add(new StudentsViewModel(_session) { Group = "People" });
        Add(new AssignmentsViewModel(_session) { Group = "People" });

        Add(new SettingsViewModel(_session) { Group = "Config" });
        Add(new AiKeysViewModel(_session) { Group = "Config" });
        Add(new PromptsViewModel(_session) { Group = "Config" });

        Add(JsonPageViewModel.Storage(_session));
        Add(JsonPageViewModel.Privacy(_session));
        Add(JsonPageViewModel.Diagnostics(_session));
        Add(JsonPageViewModel.Audit(_session));
        Add(JsonPageViewModel.MultiSchool(_session));
        Add(JsonPageViewModel.RemoteAccess(_session));
        Add(JsonPageViewModel.Updates(_session));

        BuildSpaces();
    }

    private void BuildSpaces()
    {
        Spaces.Clear();
        foreach (var group in Pages.GroupBy(p => p.Group))
        {
            var space = new SpaceVm(group.Key);
            foreach (var page in group)
            {
                space.Pages.Add(page);
            }

            Spaces.Add(space);
        }
    }

    private void Add(PageViewModel page)
    {
        _pages[page.Key] = page;
        Pages.Add(page);
    }

    private void SyncSpace(string group)
    {
        if (_currentSpace is not null && string.Equals(_currentSpace.Name, group, StringComparison.Ordinal))
        {
            return;
        }

        var space = Spaces.FirstOrDefault(s => string.Equals(s.Name, group, StringComparison.Ordinal));
        if (space is null)
        {
            return;
        }

        _currentSpace = space;
        OnPropertyChanged(nameof(CurrentSpace));
        OnPropertyChanged(nameof(SpacePages));
        OnPropertyChanged(nameof(SpaceName));
        OnPropertyChanged(nameof(HasSpaceTabs));
    }

    private void ToggleRail() => IsRailExpanded = !IsRailExpanded;

    // ------------------------------------------------------------- palette

    private void TogglePalette()
    {
        if (IsPaletteOpen)
        {
            ClosePalette();
            return;
        }

        PaletteQuery = string.Empty;
        RefreshPalette();
        IsPaletteOpen = true;
    }

    public void OpenPalette() => TogglePalette();

    public void ClosePalette()
    {
        if (!IsPaletteOpen)
        {
            return;
        }

        IsPaletteOpen = false;
        PaletteQuery = string.Empty;
    }

    private void RefreshPalette()
    {
        var query = PaletteQuery?.Trim() ?? string.Empty;
        var matches = Pages.Where(p =>
            query.Length == 0
            || p.Title.Contains(query, StringComparison.OrdinalIgnoreCase)
            || p.Group.Contains(query, StringComparison.OrdinalIgnoreCase)
            || p.Key.Contains(query, StringComparison.OrdinalIgnoreCase));

        PaletteResults.Clear();
        // Every match is listed; the palette itself scrolls, so a query is
        // never silently truncated.
        foreach (var page in matches)
        {
            PaletteResults.Add(page);
        }

        PaletteSelection = PaletteResults.FirstOrDefault();
        OnPropertyChanged(nameof(PaletteEmpty));
        OnPropertyChanged(nameof(PaletteHint));
    }

    /// <summary>Moves the highlighted palette row, clamped to the result list.</summary>
    public void MovePaletteSelection(int delta)
    {
        if (PaletteResults.Count == 0)
        {
            return;
        }

        var index = PaletteSelection is null ? -1 : PaletteResults.IndexOf(PaletteSelection);
        index = Math.Clamp(index + delta, 0, PaletteResults.Count - 1);
        PaletteSelection = PaletteResults[index];
    }

    public void CommitPalette()
    {
        var page = PaletteSelection ?? PaletteResults.FirstOrDefault();
        if (page is null)
        {
            ClosePalette();
            return;
        }

        Current = page;
        ClosePalette();
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
        BackendWarning = status.Warning ?? string.Empty;
        OnPropertyChanged(nameof(DataFolder));
    }

    public async Task ShutdownAsync()
    {
        _session.Settings.WindowMaximized = Application.Current.MainWindow?.WindowState == WindowState.Maximized;
        _session.Settings.RailCollapsed = !IsRailExpanded;
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