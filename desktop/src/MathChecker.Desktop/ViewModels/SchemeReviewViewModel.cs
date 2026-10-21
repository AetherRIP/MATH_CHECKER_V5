using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class SchemeRow
{
    public string Id { get; init; } = string.Empty;
    public string Title { get; init; } = string.Empty;
    public string Status { get; init; } = string.Empty;
    public string Detail { get; init; } = string.Empty;

    public override string ToString() => $"{Title} ({Status})";
}

public sealed class SchemeReviewViewModel : PageViewModel
{
    private readonly string[] _decisions =
    {
        "CONFIRM_SCHEME_CORRECT",
        "CONFIRM_SCHEME_INCORRECT",
        "ACCEPT_ALTERNATIVE_ANSWER",
        "AWARD_PARTIAL_CREDIT",
        "REJECT_BULK_CORRECTION",
        "REQUEST_FURTHER_INVESTIGATION",
    };

    private SchemeRow? _selectedDiscrepancy;
    private SchemeRow? _selectedProposal;
    private string _detailText = "Select a discrepancy to see its record.";
    private string _summary = string.Empty;

    public SchemeReviewViewModel(AppSession session)
        : base(session, "scheme", "Scheme mismatches")
    {
        Group = "Review";
    }

    public ObservableCollection<SchemeRow> Discrepancies { get; } = new();
    public ObservableCollection<SchemeRow> Proposals { get; } = new();
    public ObservableCollection<string> Decisions => new(_decisions);

    public SchemeRow? SelectedDiscrepancy
    {
        get => _selectedDiscrepancy;
        set
        {
            if (SetProperty(ref _selectedDiscrepancy, value) && value is not null)
            {
                _ = LoadDetailAsync(value.Id);
            }
        }
    }

    public SchemeRow? SelectedProposal
    {
        get => _selectedProposal;
        set => SetProperty(ref _selectedProposal, value);
    }

    public string DetailText
    {
        get => _detailText;
        private set => SetProperty(ref _detailText, value);
    }

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    public AsyncCommand ProposeCommand => Command(async () =>
    {
        if (SelectedDiscrepancy is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Stage a correction",
                "Build a staged correction for this discrepancy? This changes nothing until a proposal "
                + "is explicitly approved."))
        {
            return;
        }

        var payload = await Session.Api.ProposeDiscrepancyAsync(SelectedDiscrepancy.Id);
        DetailText = Json.Pretty(payload);
        await LoadAsync();
        Status = "Proposal staged. Approving it is a separate, explicit step.";
    });

    public AsyncCommand DecideCommand => Command(async () =>
    {
        if (SelectedDiscrepancy is null)
        {
            return;
        }

        var decision = Session.Dialogs.AskText("Record a decision",
            "Decision for this discrepancy:\n" + string.Join("\n", _decisions), _decisions[0]);
        if (decision is null)
        {
            return;
        }

        if (!_decisions.Contains(decision.Trim()))
        {
            Error = "That is not one of the backend's accepted decisions.";
            return;
        }

        var payload = await Session.Api.DecideDiscrepancyAsync(SelectedDiscrepancy.Id, decision.Trim().ToUpperInvariant());
        DetailText = Json.Pretty(payload);
        await LoadAsync();
    });

    public AsyncCommand ApproveProposalCommand => Command(async () =>
    {
        if (SelectedProposal is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Approve bulk correction",
                $"Apply proposal {SelectedProposal.Id}? This can change previously stored marks. "
                + "The backend requires an explicit confirmation and records the approval."))
        {
            return;
        }

        DetailText = Json.Pretty(await Session.Api.DecideProposalAsync(SelectedProposal.Id, "approve"));
        await LoadAsync();
        Status = "Proposal approved.";
    });

    public AsyncCommand RejectProposalCommand => Command(async () =>
    {
        if (SelectedProposal is null)
        {
            return;
        }

        DetailText = Json.Pretty(await Session.Api.DecideProposalAsync(SelectedProposal.Id, "reject"));
        await LoadAsync();
        Status = "Proposal rejected.";
    });

    protected override async Task LoadCoreAsync()
    {
        var summary = await Session.Api.SchemeSummaryAsync();
        var discrepancies = await Session.Api.DiscrepanciesAsync();
        var proposals = await Session.Api.ProposalsAsync();

        var previousDiscrepancy = SelectedDiscrepancy?.Id;
        var previousProposal = SelectedProposal?.Id;

        Discrepancies.Clear();
        foreach (var row in ReadItems(discrepancies))
        {
            Discrepancies.Add(row);
        }

        Proposals.Clear();
        foreach (var row in ReadItems(proposals))
        {
            Proposals.Add(row);
        }

        SelectedDiscrepancy = Discrepancies.FirstOrDefault(r => r.Id == previousDiscrepancy)
                              ?? Discrepancies.FirstOrDefault();
        SelectedProposal = Proposals.FirstOrDefault(r => r.Id == previousProposal) ?? Proposals.FirstOrDefault();

        var open = summary.TryGetProperty("open_discrepancies", out var o) ? o.ToString() : "0";
        var awaiting = summary.TryGetProperty("awaiting_approval", out var a) ? a.ToString() : "0";
        Summary = $"{open} discrepancies awaiting review · {awaiting} proposals awaiting approval";
    }

    private async Task LoadDetailAsync(string id)
    {
        try
        {
            DetailText = Json.Pretty(await Session.Api.RawAsync($"scheme/discrepancies/{id}"));
        }
        catch (ApiException ex)
        {
            DetailText = ex.Display;
        }
    }

    private static IEnumerable<SchemeRow> ReadItems(JsonElement payload)
    {
        if (!payload.TryGetProperty("items", out var items) || items.ValueKind != JsonValueKind.Array)
        {
            yield break;
        }

        foreach (var item in items.EnumerateArray())
        {
            var id = Text(item, "id") ?? string.Empty;
            var title = Text(item, "question_id") ?? Text(item, "title") ?? Text(item, "kind")
                        ?? Text(item, "coursework_id") ?? id;
            var status = Text(item, "review_status") ?? Text(item, "status") ?? Text(item, "state") ?? string.Empty;
            var detail = Text(item, "detail") ?? Text(item, "message") ?? Text(item, "reason") ?? string.Empty;
            yield return new SchemeRow { Id = id, Title = title, Status = status, Detail = detail };
        }
    }

    private static string? Text(JsonElement element, string property)
    {
        if (!element.TryGetProperty(property, out var value))
        {
            return null;
        }

        return value.ValueKind switch
        {
            JsonValueKind.String => value.GetString(),
            JsonValueKind.Null or JsonValueKind.Undefined => null,
            _ => value.ToString(),
        };
    }
}
