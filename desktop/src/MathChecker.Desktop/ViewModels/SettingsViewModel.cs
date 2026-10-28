using System.Collections.ObjectModel;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class SettingsViewModel : PageViewModel
{
    private string _teacherName = string.Empty;
    private string _schoolName = string.Empty;
    private string _centerName = string.Empty;
    private string _teacherTitle = string.Empty;
    private string _academicYear = string.Empty;
    private string _documentLabel = string.Empty;
    private int _concurrency = 2;
    private int _maxRetries = 2;
    private double _hourlyMb;
    private double _dailyMb;
    private int _keepDays = 30;
    private bool _mockMode;
    private bool _debug;
    private bool _classroomEnabled;
    private bool _writeDraftGrades;

    public SettingsViewModel(AppSession session)
        : base(session, "settings", "Settings")
    {
    }

    public string TeacherName { get => _teacherName; set => SetProperty(ref _teacherName, value); }
    public string SchoolName { get => _schoolName; set => SetProperty(ref _schoolName, value); }
    public string CenterName { get => _centerName; set => SetProperty(ref _centerName, value); }
    public string TeacherTitle { get => _teacherTitle; set => SetProperty(ref _teacherTitle, value); }
    public string AcademicYear { get => _academicYear; set => SetProperty(ref _academicYear, value); }
    public string DocumentLabel { get => _documentLabel; set => SetProperty(ref _documentLabel, value); }
    public int Concurrency { get => _concurrency; set => SetProperty(ref _concurrency, value); }
    public int MaxRetries { get => _maxRetries; set => SetProperty(ref _maxRetries, value); }
    public double HourlyMb { get => _hourlyMb; set => SetProperty(ref _hourlyMb, value); }
    public double DailyMb { get => _dailyMb; set => SetProperty(ref _dailyMb, value); }
    public int KeepDays { get => _keepDays; set => SetProperty(ref _keepDays, value); }
    public bool MockMode { get => _mockMode; set => SetProperty(ref _mockMode, value); }
    public bool Debug { get => _debug; set => SetProperty(ref _debug, value); }
    public bool ClassroomEnabled { get => _classroomEnabled; set => SetProperty(ref _classroomEnabled, value); }
    public bool WriteDraftGrades { get => _writeDraftGrades; set => SetProperty(ref _writeDraftGrades, value); }

    /// <summary>Facts the backend owns (role, key count, access mode) shown as
    /// labelled values instead of a sentence with slashes and colons.</summary>
    public ObservableCollection<FactRow> ReadOnlyFacts { get; } = new();

    /// <summary>Which appearance pill is active. Switching is immediate: every
    /// colour in the shell is a DynamicResource, so no page is rebuilt.</summary>
    public bool IsLightTheme => ThemeManager.Current == AppAppearance.Light;

    public bool IsDarkTheme => ThemeManager.Current == AppAppearance.Dark;

    public RelayCommand LightThemeCommand => Sync(() => SetAppearance(AppAppearance.Light));

    public RelayCommand DarkThemeCommand => Sync(() => SetAppearance(AppAppearance.Dark));

    private void SetAppearance(AppAppearance appearance)
    {
        if (ThemeManager.Current == appearance)
        {
            return;
        }

        ThemeManager.Apply(appearance);
        Session.Settings.Appearance = ThemeManager.Serialize(appearance);
        Session.Settings.Save();
        OnPropertyChanged(nameof(IsLightTheme));
        OnPropertyChanged(nameof(IsDarkTheme));
        Status = appearance == AppAppearance.Dark
            ? "Dark appearance applied and saved."
            : "Light appearance applied and saved.";
    }

    public AsyncCommand SaveCommand => CommandWithReload(async () =>
    {
        if (Concurrency is < 1 or > 8)
        {
            Error = "Worker concurrency must be between 1 and 8.";
            return;
        }

        var payload = new
        {
            mock_mode = MockMode,
            debug = Debug,
            worker = new { concurrency = Concurrency, max_retries = MaxRetries },
            usage = new { hourly_mb = (int)HourlyMb, daily_mb = (int)DailyMb },
            classroom = new { enabled = ClassroomEnabled, write_draft_grades = WriteDraftGrades },
            teacher = new { teacher_name = TeacherName, school_name = SchoolName },
            reports = new
            {
                center_name = CenterName,
                teacher_title = TeacherTitle,
                academic_year = AcademicYear,
                document_label = DocumentLabel,
            },
            retention = new { keep_days = KeepDays },
        };

        await Session.Api.SaveSettingsAsync(payload);
    }, "Settings saved. Values shown are what the backend now reports.");

    protected override async Task LoadCoreAsync()
    {
        var settings = await Session.Api.SettingsAsync();
        TeacherName = settings.Teacher?.TeacherName ?? string.Empty;
        SchoolName = settings.Teacher?.SchoolName ?? string.Empty;
        CenterName = settings.Reports?.CenterName ?? string.Empty;
        TeacherTitle = settings.Reports?.TeacherTitle ?? string.Empty;
        AcademicYear = settings.Reports?.AcademicYear ?? string.Empty;
        DocumentLabel = settings.Reports?.DocumentLabel ?? string.Empty;
        Concurrency = settings.Worker?.Concurrency ?? 2;
        MaxRetries = settings.Worker?.MaxRetries ?? 2;
        HourlyMb = settings.Usage?.HourlyMb ?? 0;
        DailyMb = settings.Usage?.DailyMb ?? 0;
        KeepDays = settings.Retention?.KeepDays ?? 30;
        MockMode = settings.MockMode;
        Debug = settings.Debug;
        ClassroomEnabled = settings.Classroom?.Enabled ?? false;
        WriteDraftGrades = settings.Classroom?.WriteDraftGrades ?? false;

        var models = settings.Gemini?.PrimaryModels ?? new List<string>();
        ReadOnlyFacts.Clear();
        ReadOnlyFacts.Add(new FactRow("Signed-in role", PayloadFacts.StatusLabel(settings.Role)));
        ReadOnlyFacts.Add(new FactRow("AI keys", settings.GeminiKeyCount.ToString()));
        ReadOnlyFacts.Add(new FactRow("Google OAuth client",
            settings.HasOauthClient ? "Configured" : "Not configured"));
        ReadOnlyFacts.Add(new FactRow("Models", models.Count == 0 ? "None reported" : string.Join(", ", models)));
        ReadOnlyFacts.Add(new FactRow("Access", $"{PayloadFacts.StatusLabel(settings.Network?.AccessMode)} · "
                                                   + $"port {settings.Network?.Port ?? 0}"));
        ReadOnlyFacts.Add(new FactRow("Lifecycle supervisor",
            settings.Lifecycle?.Supervisor == true ? "On" : "Off"));
    }
}
