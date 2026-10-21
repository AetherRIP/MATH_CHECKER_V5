using System.Collections.ObjectModel;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class ReviewViewModel : PageViewModel
{
    private ReviewItemDto? _selected;
    private string _summary = string.Empty;

    public ReviewViewModel(AppSession session)
        : base(session, "review", "Queue")
    {
    }

    public ObservableCollection<ReviewItemDto> Items { get; } = new();

    public ReviewItemDto? Selected
    {
        get => _selected;
        set
        {
            if (SetProperty(ref _selected, value))
            {
                OnPropertyChanged(nameof(HasSelection));
            }
        }
    }

    public bool HasSelection => Selected is not null;

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    public AsyncCommand AcceptCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Accept result",
                $"Accept the engine's result for {Selected.StudentName ?? Selected.StudentId} "
                + $"({Selected.ScoreText})? This records a teacher decision and cannot be undone by a later "
                + "automated run."))
        {
            return;
        }

        await Session.Api.OverrideResultAsync(Selected.Id, null, "Accepted from the desktop review queue");
        await LoadAsync();
        Status = "Result accepted.";
    });

    public AsyncCommand OverrideCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        var available = Selected.Available?.ToString("0") ?? "?";
        var entered = Session.Dialogs.AskText("Enter the mark",
            $"Mark out of {available} for {Selected.StudentName ?? Selected.StudentId}:",
            Selected.Earned?.ToString("0") ?? string.Empty);
        if (entered is null)
        {
            return;
        }

        // The backend casts the mark to an integer; 12.5 would be a 400.
        if (!int.TryParse(entered, out var mark))
        {
            Error = "The mark must be a whole number.";
            return;
        }

        await Session.Api.OverrideResultAsync(Selected.Id, mark, "Overridden from the desktop review queue");
        await LoadAsync();
        Status = "Result overridden.";
    });

    public AsyncCommand OpenResultCommand => new(() =>
    {
        if (Selected is not null)
        {
            Session.GoTo("results");
        }

        return Task.CompletedTask;
    });

    public AsyncCommand OpenReportsCommand => new(() =>
    {
        Session.GoTo("reports");
        return Task.CompletedTask;
    });

    protected override async Task LoadCoreAsync()
    {
        var review = await Session.Api.ReviewQueueAsync();
        Items.Clear();
        foreach (var item in review.Items)
        {
            Items.Add(item);
        }

        Summary = review.Total == 0
            ? "Nothing is waiting for review."
            : $"{review.Total} results need a teacher decision.";
    }
}
