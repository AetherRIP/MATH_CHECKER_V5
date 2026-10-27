using System.Collections.ObjectModel;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class UsageViewModel : PageViewModel
{
    private UsageDto? _usage;

    public UsageViewModel(AppSession session)
        : base(session, "usage", "Data usage")
    {
    }

    public ObservableCollection<UsageHistoryDto> History { get; } = new();

    public string HourText => _usage?.HourText ?? "—";
    public string DayText => _usage?.DayText ?? "—";
    public string HourlyLimitText => _usage?.Limits?.HourlyMb is null or 0
        ? "unlimited" : $"{_usage.Limits.HourlyMb:0.#} MB";
    public string DailyLimitText => _usage?.Limits?.DailyMb is null or 0
        ? "unlimited" : $"{_usage.Limits.DailyMb:0.#} MB";
    public string RemainingText => _usage is null
        ? "—"
        : $"{_usage.HourRemainingMb:0.#} MB this hour · {_usage.DayRemainingMb:0.#} MB today";
    public string ApiText => _usage?.Api is null
        ? "—"
        : $"{_usage.Api.Requests} requests · {_usage.Api.Successes} ok · {_usage.Api.Failures} failed · "
          + $"{_usage.Api.Retries} retries · {_usage.Api.Failovers} failovers";
    /// <summary>Measured AI requests per correction, from the per-job ledger.
    /// Explicitly says "not measured" rather than showing 0.</summary>
    public string AiText => _usage?.Api?.Ai?.Text ?? "No correction has recorded its AI requests yet.";
    public bool AnyLimitReached => (_usage?.HourlyOver ?? false) || (_usage?.DailyOver ?? false);
    public string LimitWarning => AnyLimitReached
        ? "A configured traffic limit has been reached; corrections that need the internet will wait."
        : string.Empty;

    protected override async Task LoadCoreAsync()
    {
        _usage = await Session.Api.UsageAsync();
        History.Clear();
        foreach (var point in _usage.History)
        {
            History.Add(point);
        }

        OnPropertyChanged(nameof(HourText));
        OnPropertyChanged(nameof(DayText));
        OnPropertyChanged(nameof(HourlyLimitText));
        OnPropertyChanged(nameof(DailyLimitText));
        OnPropertyChanged(nameof(RemainingText));
        OnPropertyChanged(nameof(ApiText));
        OnPropertyChanged(nameof(AiText));
        OnPropertyChanged(nameof(AnyLimitReached));
        OnPropertyChanged(nameof(LimitWarning));
    }
}
