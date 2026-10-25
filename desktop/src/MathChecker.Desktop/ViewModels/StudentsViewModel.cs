using System.Collections.ObjectModel;
using System.ComponentModel;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;
using MathChecker.Desktop.Core.Api;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// The Students page. It owns the roster list and, once a student is chosen,
/// the real <see cref="StudentProfileViewModel"/> built from that student's
/// backend record — there is no second student database and no sample data.
/// </summary>
public sealed class StudentsViewModel : PageViewModel
{
    private const string AllClasses = "All classes";

    private readonly List<StudentDto> _all = new();
    private readonly Dictionary<string, StudentProfileDto> _profileCache = new(StringComparer.Ordinal);
    private StudentDto? _selected;
    private string _filter = string.Empty;
    private string _classFilter = AllClasses;
    private string _summary = string.Empty;
    private StudentProfileViewModel? _profile;
    private bool _isProfileLoading;
    private int _openRequest;

    public StudentsViewModel(AppSession session)
        : base(session, "students", "Students")
    {
        OpenProfileCommand = new RelayCommand(() => _ = OpenProfileAsync(Selected),
            () => Selected is not null);
        CloseProfileCommand = new RelayCommand(CloseProfile, () => IsProfileOpen);
        OpenResultsCommand = new RelayCommand(() => Session.GoTo("results"));
    }

    /// <summary>People are the Classroom rosters, so this page syncs them.</summary>
    public override bool SyncsFromClassroom => true;

    public ObservableCollection<StudentDto> Items { get; } = new();
    public ObservableCollection<string> ClassFilters { get; } = new() { AllClasses };

    public RelayCommand OpenProfileCommand { get; }
    public RelayCommand CloseProfileCommand { get; }
    public RelayCommand OpenResultsCommand { get; }

    public string Filter
    {
        get => _filter;
        set
        {
            if (SetProperty(ref _filter, value))
            {
                ApplyFilter();
            }
        }
    }

    public string ClassFilter
    {
        get => _classFilter;
        set
        {
            // Rebuilding ClassFilters empties the ComboBox, and WPF writes null
            // back through the two-way binding. Treat that as "no class filter"
            // instead of a filter that matches nothing.
            var normalized = string.IsNullOrWhiteSpace(value) ? AllClasses : value;
            if (SetProperty(ref _classFilter, normalized))
            {
                ApplyFilter();
            }
        }
    }

    public StudentDto? Selected
    {
        get => _selected;
        set
        {
            if (!SetProperty(ref _selected, value))
            {
                return;
            }

            OpenProfileCommand.RaiseCanExecuteChanged();
            if (value is not null && Profile is null)
            {
                // A click opens the profile, but arrow-keying down a roster must
                // not fire a request per row: a short settle window lets only
                // the last selection through, and the result is cached.
                var request = ++_openRequest;
                _ = OpenProfileAfterDelayAsync(value, request);
            }
        }
    }

    /// <summary>The open student, or null while the roster is showing.</summary>
    public StudentProfileViewModel? Profile
    {
        get => _profile;
        private set
        {
            if (!SetProperty(ref _profile, value))
            {
                return;
            }

            OnPropertyChanged(nameof(IsProfileOpen));
            OnPropertyChanged(nameof(IsProfileClosed));
            OnPropertyChanged(nameof(HeaderEyebrow));
            OnPropertyChanged(nameof(HeaderTitle));
            OnPropertyChanged(nameof(HeaderDescription));
            CloseProfileCommand.RaiseCanExecuteChanged();
        }
    }

    public bool IsProfileOpen => Profile is not null;

    public bool IsProfileClosed => Profile is null;

    public bool IsProfileLoading
    {
        get => _isProfileLoading;
        private set => SetProperty(ref _isProfileLoading, value);
    }

    public string HeaderEyebrow => IsProfileOpen ? "PEOPLE · PROFILE" : "PEOPLE";

    public string HeaderTitle => Profile?.Profile.Name ?? Title;

    public string HeaderDescription => Profile is { } profile
        ? $"{profile.Profile.ClassText} · Google Classroom ID {profile.Profile.StudentId}"
        : Summary;

    public string Summary
    {
        get => _summary;
        private set
        {
            if (SetProperty(ref _summary, value))
            {
                OnPropertyChanged(nameof(HeaderDescription));
            }
        }
    }

    protected override async Task LoadCoreAsync()
    {
        // An explicit page load means "show me the current roster", so any
        // cached profile is refreshed rather than replayed.
        _profileCache.Clear();

        var students = await Session.Api.StudentsAsync();
        _all.Clear();
        _all.AddRange(students.Students);

        LastSync = students.LastSync;
        EmailSource = students.Source?.EmailText ?? "source not reported";

        var previousClassFilter = ClassFilter;
        RebuildClassFilters(previousClassFilter);
        ApplyFilter();
        RaiseFacts();
        Summary = $"{_all.Count} students known from Classroom, local corrections and results.";
    }

    /// <summary>The headline facts are computed from the loaded roster, so they
    /// are refreshed explicitly once a load (or a filter) changes it.</summary>
    private void RaiseFacts()
    {
        OnPropertyChanged(nameof(StudentsCountText));
        OnPropertyChanged(nameof(ClassesCountText));
        OnPropertyChanged(nameof(CorrectionsCountText));
        OnPropertyChanged(nameof(InProgressCountText));
        OnPropertyChanged(nameof(LastSyncText));
        OnPropertyChanged(nameof(EmailSource));
        OnPropertyChanged(nameof(ShownText));
    }

    /// <summary>When Classroom last delivered this roster, as the backend
    /// recorded it. Stated in the page header so a stale list is obvious.</summary>
    public double? LastSync { get; private set; }

    public string EmailSource { get; private set; } = "source not reported";

    public string StudentsCountText => _all.Count.ToString();

    public string ClassesCountText => _all
        .Select(s => s.ClassLabel ?? string.Empty)
        .Where(label => label.Length > 0)
        .Distinct(StringComparer.OrdinalIgnoreCase)
        .Count()
        .ToString();

    public string CorrectionsCountText => _all.Sum(s => s.Corrections).ToString();

    public string InProgressCountText => _all.Sum(s => s.Pending).ToString();

    public string ShownText => Items.Count == 1 ? "1 shown" : $"{Items.Count} shown";

    public string LastSyncText => LastSync is > 0
        ? "Roster synced " + EpochTime.Format(LastSync, "never")
        : "Roster never synced";

    /// <summary>Opens (or re-opens) a student's profile. Safe to await in tests;
    /// the roster selection calls it after a short settle window.</summary>
    public async Task OpenProfileAsync(StudentDto? student)
    {
        if (student is null)
        {
            return;
        }

        // Ignore a late response for a student the user has already left.
        var request = ++_openRequest;
        IsProfileLoading = true;
        Error = null;
        try
        {
            if (!_profileCache.TryGetValue(student.StudentId, out var dto))
            {
                dto = await Session.Api.StudentAsync(student.StudentId).ConfigureAwait(true);
                _profileCache[student.StudentId] = dto;
            }

            if (request != _openRequest)
            {
                return;
            }

            ShowProfile(dto);
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
            Session.Notify("Could not open " + student.Name + ": " + ex.Display);
        }
        catch (Exception ex)
        {
            Error = ex.Message;
            Session.Log.Error("student profile failed to load", ex);
        }
        finally
        {
            IsProfileLoading = false;
        }
    }

    private async Task OpenProfileAfterDelayAsync(StudentDto student, int request)
    {
        try
        {
            await Task.Delay(180).ConfigureAwait(true);
        }
        catch (TaskCanceledException)
        {
            return;
        }

        if (request != _openRequest || Selected?.StudentId != student.StudentId)
        {
            return;
        }

        await OpenProfileAsync(student).ConfigureAwait(true);
    }

    private void ShowProfile(StudentProfileDto dto)
    {
        if (Profile is not null)
        {
            Profile.PropertyChanged -= OnProfilePropertyChanged;
        }

        var profile = new StudentProfileViewModel(Session, dto);
        profile.BackRequested += CloseProfile;
        profile.ReloadRequested += ReloadProfileAsync;
        profile.ProfileSaved += () => ApplySavedProfile(profile.Profile);
        profile.PropertyChanged += OnProfilePropertyChanged;
        Profile = profile;
        _ = profile.LoadAsync();
    }

    private void OnProfilePropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(StudentProfileViewModel.Profile))
        {
            OnPropertyChanged(nameof(HeaderTitle));
            OnPropertyChanged(nameof(HeaderDescription));
        }
    }

    /// <summary>Refetches the open profile from the backend (not from the
    /// cache) and replaces it in place, keeping the current tab.</summary>
    private async Task ReloadProfileAsync()
    {
        var id = Profile?.Profile.StudentId ?? Selected?.StudentId;
        if (string.IsNullOrWhiteSpace(id))
        {
            return;
        }

        var request = ++_openRequest;
        IsProfileLoading = true;
        Error = null;
        try
        {
            var dto = await Session.Api.StudentAsync(id).ConfigureAwait(true);
            _profileCache[id] = dto;
            if (request != _openRequest)
            {
                return;
            }

            if (Profile is { } profile && profile.Profile.StudentId == id)
            {
                profile.Apply(dto);
            }
            else
            {
                ShowProfile(dto);
            }

            await LoadRosterQuietlyAsync().ConfigureAwait(true);
        }
        catch (ApiException ex)
        {
            Error = ex.Display;
        }
        finally
        {
            IsProfileLoading = false;
        }
    }

    /// <summary>Refreshes just the row behind the open profile after a save, so
    /// the list's class label agrees with the panel without closing it.</summary>
    private void ApplySavedProfile(StudentProfileDto updated)
    {
        _profileCache[updated.StudentId] = updated;
        var row = _all.FirstOrDefault(s => s.StudentId == updated.StudentId);
        if (row is not null)
        {
            row.OfficialName = updated.OfficialName;
            row.Nickname = updated.Nickname;
            row.ClassName = updated.ClassName;
            row.ClassLabel = updated.ClassLabel;
            row.DisplayName = updated.DisplayName;
        }

        RebuildClassFilters(ClassFilter);
        ApplyFilter();
    }

    private async Task LoadRosterQuietlyAsync()
    {
        try
        {
            var students = await Session.Api.StudentsAsync().ConfigureAwait(true);
            _all.Clear();
            _all.AddRange(students.Students);
            LastSync = students.LastSync;
            RebuildClassFilters(ClassFilter);
            ApplyFilter();
            RaiseFacts();
        }
        catch (Exception ex)
        {
            Session.Log.Warn("student roster refresh failed: " + ex.Message);
        }
    }

    public void CloseProfile()
    {
        if (Profile is not null)
        {
            Profile.PropertyChanged -= OnProfilePropertyChanged;
        }

        ++_openRequest;
        Profile = null;
        Error = null;
        if (_selected is not null)
        {
            _selected = null;
            OnPropertyChanged(nameof(Selected));
            OpenProfileCommand.RaiseCanExecuteChanged();
        }
    }

    private void RebuildClassFilters(string previous)
    {
        var wanted = new List<string> { AllClasses };
        wanted.AddRange(_all.Select(s => s.ClassLabel ?? "Not assigned")
            .Distinct()
            .OrderBy(x => x, StringComparer.OrdinalIgnoreCase));

        // Reconcile in place instead of Clear + Add. Emptying the list makes a
        // bound ComboBox drop its own selection, and WPF then leaves the box
        // blank even after the same value is restored.
        for (var i = ClassFilters.Count - 1; i >= 0; i--)
        {
            if (!wanted.Contains(ClassFilters[i]))
            {
                ClassFilters.RemoveAt(i);
            }
        }

        for (var i = 0; i < wanted.Count; i++)
        {
            if (i >= ClassFilters.Count)
            {
                ClassFilters.Add(wanted[i]);
            }
            else if (!string.Equals(ClassFilters[i], wanted[i], StringComparison.Ordinal))
            {
                ClassFilters.Insert(i, wanted[i]);
            }
        }

        // Restore the selection explicitly. The notification matters when the
        // restored value already equals the current one — otherwise the
        // ComboBox is never told to show it.
        RestoreClassFilterSelection(previous);
    }

    private void ApplyFilter()
    {
        var needle = Filter.Trim();
        Items.Clear();
        foreach (var student in _all)
        {
            if (!string.Equals(ClassFilter, AllClasses, StringComparison.Ordinal)
                && (student.ClassLabel ?? "Not assigned") != ClassFilter)
            {
                continue;
            }

            if (needle.Length > 0
                && (student.Name.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
                && (student.StudentId.IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
                && (student.Email ?? string.Empty).IndexOf(needle, StringComparison.OrdinalIgnoreCase) < 0)
            {
                continue;
            }

            Items.Add(student);
        }

        OnPropertyChanged(nameof(ShownText));
    }

    /// <summary>The class filter is a plain string list. WPF clears its own
    /// selection when that list is rebuilt, and a null write comes back through
    /// the two-way binding; re-announcing the restored value puts the selected
    /// class back on screen instead of leaving an empty box.</summary>
    private void RestoreClassFilterSelection(string previous)
    {
        var restored = ClassFilters.Contains(previous) ? previous : AllClasses;
        if (!string.Equals(_classFilter, restored, StringComparison.Ordinal))
        {
            _classFilter = restored;
        }

        OnPropertyChanged(nameof(ClassFilter));
    }

}
