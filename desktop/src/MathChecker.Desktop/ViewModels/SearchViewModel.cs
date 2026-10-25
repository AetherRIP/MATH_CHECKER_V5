using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class SearchHit
{
    public string Group { get; init; } = string.Empty;
    public string Title { get; init; } = string.Empty;
    public string Subtitle { get; init; } = string.Empty;
    public string? Route { get; init; }
}

public sealed class SearchViewModel : PageViewModel
{
    private string _query = string.Empty;
    private string _summary = "Type a student, class, assignment or result id.";
    private bool _hasSearched;

    public SearchViewModel(AppSession session)
        : base(session, "search", "Search")
    {
    }

    public string Query
    {
        get => _query;
        set => SetProperty(ref _query, value);
    }

    public ObservableCollection<SearchHit> Hits { get; } = new();

    /// <summary>False before the first search. The page must not open on
    /// "Nothing found", which reads like a search that failed.</summary>
    public bool HasSearched
    {
        get => _hasSearched;
        private set
        {
            if (SetProperty(ref _hasSearched, value))
            {
                OnPropertyChanged(nameof(ShowNoResults));
                OnPropertyChanged(nameof(ShowInitialHint));
            }
        }
    }

    public bool ShowNoResults => HasSearched && Hits.Count == 0;

    public bool ShowInitialHint => !HasSearched;

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    public AsyncCommand SearchCommand => Command(async () =>
    {
        Hits.Clear();
        OnPropertyChanged(nameof(ShowNoResults));
        if (string.IsNullOrWhiteSpace(Query))
        {
            Summary = "Type a student, class, assignment or result id.";
            return;
        }

        HasSearched = true;

        var payload = await Session.Api.SearchAsync(Query);
        foreach (var group in payload.GetProperty("groups").EnumerateArray())
        {
            var groupLabel = group.TryGetProperty("label", out var label) ? label.GetString() ?? "Results" : "Results";
            if (!group.TryGetProperty("items", out var items) || items.ValueKind != JsonValueKind.Array)
            {
                continue;
            }

            foreach (var item in items.EnumerateArray())
            {
                Hits.Add(new SearchHit
                {
                    Group = groupLabel,
                    Title = item.TryGetProperty("title", out var title) ? title.GetString() ?? "—" : "—",
                    Subtitle = item.TryGetProperty("subtitle", out var subtitle)
                        ? subtitle.GetString() ?? string.Empty
                        : string.Empty,
                    Route = item.TryGetProperty("route", out var route) ? route.GetString() : null,
                });
            }
        }

        Summary = payload.TryGetProperty("total", out var total)
            ? $"{total.GetInt32()} matches"
            : $"{Hits.Count} matches";
        OnPropertyChanged(nameof(ShowNoResults));
    });

    protected override Task LoadCoreAsync() => Task.CompletedTask;
}
