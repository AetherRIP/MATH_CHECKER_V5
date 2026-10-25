using System.Collections.ObjectModel;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One review row, described the way a teacher reads it. The backend
/// id stays on the row because every action needs it, but it is never the
/// column a person has to read: question, assignment and student say which
/// case this is.</summary>
public sealed class SchemeRow
{
    public string Id { get; init; } = string.Empty;
    public string Question { get; init; } = string.Empty;
    public string Assignment { get; init; } = string.Empty;
    public string Student { get; init; } = string.Empty;
    public string Status { get; init; } = string.Empty;
    public string Detail { get; init; } = string.Empty;

    /// <summary>One line that names the case in a dialog, without a uuid.</summary>
    public string Mention => string.IsNullOrWhiteSpace(Question)
        ? Assignment
        : $"question {Question}" + (string.IsNullOrWhiteSpace(Assignment) ? string.Empty : $" of {Assignment}");

    public override string ToString() => $"{Mention} ({Status})";
}

public sealed class SchemeReviewViewModel : PageViewModel
{
    /// <summary>The decisions the backend accepts, each with the wording a
    /// teacher is shown. The code travels to the API; the label never does.</summary>
    private static readonly (string Code, string Label)[] _decisions =
    {
        ("CONFIRM_SCHEME_CORRECT", "The mark scheme answer is correct"),
        ("CONFIRM_SCHEME_INCORRECT", "The mark scheme answer is wrong"),
        ("ACCEPT_ALTERNATIVE_ANSWER", "Accept the student's alternative answer"),
        ("AWARD_PARTIAL_CREDIT", "Award partial credit"),
        ("REJECT_BULK_CORRECTION", "Reject the bulk correction"),
        ("REQUEST_FURTHER_INVESTIGATION", "Ask for further investigation"),
    };

    private SchemeRow? _selectedDiscrepancy;
    private SchemeRow? _selectedProposal;
    private string _detailNote = "Select a discrepancy to see its record.";
    private string _detailRaw = string.Empty;
    private string _summary = string.Empty;

    public SchemeReviewViewModel(AppSession session)
        : base(session, "scheme", "Mark scheme review")
    {
        Group = "Review";
    }

    public ObservableCollection<SchemeRow> Discrepancies { get; } = new();
    public ObservableCollection<SchemeRow> Proposals { get; } = new();

    public SchemeRow? SelectedDiscrepancy
    {
        get => _selectedDiscrepancy;
        set
        {
            if (!SetProperty(ref _selectedDiscrepancy, value))
            {
                return;
            }

            OnPropertyChanged(nameof(HasDiscrepancy));
            if (value is not null)
            {
                _ = LoadDetailAsync(value.Id);
            }
        }
    }

    public SchemeRow? SelectedProposal
    {
        get => _selectedProposal;
        set
        {
            if (SetProperty(ref _selectedProposal, value))
            {
                OnPropertyChanged(nameof(HasProposal));
            }
        }
    }

    /// <summary>A discrepancy drives staging and the decision record; a proposal
    /// drives approve and reject. Both commands return early without one.</summary>
    public bool HasDiscrepancy => _selectedDiscrepancy is not null;

    public bool HasProposal => _selectedProposal is not null;

    /// <summary>The discrepancy record read as a sentence and labelled facts;
    /// the raw payload stays inside technical details.</summary>
    public ObservableCollection<FactRow> DetailFacts { get; } = new();

    public string DetailNote
    {
        get => _detailNote;
        private set
        {
            if (SetProperty(ref _detailNote, value))
            {
                OnPropertyChanged(nameof(HasDetailNote));
            }
        }
    }

    public string DetailRaw
    {
        get => _detailRaw;
        private set
        {
            if (SetProperty(ref _detailRaw, value))
            {
                OnPropertyChanged(nameof(HasDetailRaw));
            }
        }
    }

    public bool HasDetailNote => !string.IsNullOrWhiteSpace(DetailNote);

    public bool HasDetailFacts => DetailFacts.Count > 0;

    public bool HasDetailRaw => !string.IsNullOrWhiteSpace(DetailRaw);

    private void ShowPayload(JsonElement payload, string fallbackNote)
    {
        DetailFacts.Clear();
        foreach (var fact in PayloadFacts.Of(payload))
        {
            DetailFacts.Add(fact);
        }

        DetailRaw = PayloadFacts.Raw(payload);
        var summary = PayloadFacts.Summarize(payload);
        DetailNote = string.IsNullOrWhiteSpace(summary) ? fallbackNote : summary;
        OnPropertyChanged(nameof(HasDetailFacts));
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
        ShowPayload(payload, "A staged proposal was created. Nothing changes until it is approved.");
        await LoadAsync();
        Status = "Proposal staged. Approving it is a separate, explicit step.";
    });

    public AsyncCommand DecideCommand => Command(async () =>
    {
        if (SelectedDiscrepancy is null)
        {
            return;
        }

        // The dialog lists what each decision means and accepts either its
        // number or its wording, so a teacher never has to type an enum value.
        var menu = string.Join("\n", _decisions.Select((d, index) => $"{index + 1}. {d.Label}"));
        var answer = Session.Dialogs.AskText("Record a decision",
            $"What is the outcome for {SelectedDiscrepancy.Mention}?\n\n{menu}\n\nType a number or the wording of one decision:",
            "1");
        if (answer is null)
        {
            return;
        }

        var chosen = ResolveDecision(answer);
        if (chosen is null)
        {
            Error = "That is not one of the listed decisions. Pick a number from 1 to "
                    + _decisions.Length + ".";
            return;
        }

        var payload = await Session.Api.DecideDiscrepancyAsync(SelectedDiscrepancy.Id, chosen.Value.Code);
        ShowPayload(payload, $"Decision recorded: {chosen.Value.Label.ToLowerInvariant()}.");
        await LoadAsync();
    });

    public AsyncCommand ApproveProposalCommand => Command(async () =>
    {
        if (SelectedProposal is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Approve bulk correction",
                $"Apply the staged correction for {SelectedProposal.Mention}? "
                + "This can change previously stored marks. The backend requires an explicit "
                + "confirmation and records the approval."))
        {
            return;
        }

        ShowPayload(await Session.Api.DecideProposalAsync(SelectedProposal.Id, "approve"),
            "Proposal approved; the staged corrections are applied.");
        await LoadAsync();
        Status = "Proposal approved.";
    });

    public AsyncCommand RejectProposalCommand => Command(async () =>
    {
        if (SelectedProposal is null)
        {
            return;
        }

        ShowPayload(await Session.Api.DecideProposalAsync(SelectedProposal.Id, "reject"),
            "Proposal rejected; no marks changed.");
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

        var open = Number(summary, "open_discrepancies");
        var awaiting = Number(summary, "awaiting_approval");
        Summary = $"{Plural(open, "discrepancy", "discrepancies")} awaiting review · "
                  + $"{Plural(awaiting, "proposal", "proposals")} awaiting approval";
    }

    /// <summary>Accepts "3", "3." or the wording of a listed decision.</summary>
    private static (string Code, string Label)? ResolveDecision(string answer)
    {
        var text = answer.Trim();
        if (int.TryParse(text.TrimEnd('.', ')'), out var index)
            && index >= 1 && index <= _decisions.Length)
        {
            return _decisions[index - 1];
        }

        var match = _decisions.FirstOrDefault(d =>
            string.Equals(d.Label, text, StringComparison.OrdinalIgnoreCase)
            || string.Equals(d.Code, text, StringComparison.OrdinalIgnoreCase));
        return match == default ? null : match;
    }

    private static int Number(JsonElement payload, string property) =>
        payload.TryGetProperty(property, out var value) && value.ValueKind == JsonValueKind.Number
            ? value.GetInt32()
            : 0;

    private static string Plural(int count, string one, string many) =>
        $"{count} {(count == 1 ? one : many)}";

    private async Task LoadDetailAsync(string id)
    {
        try
        {
            ShowPayload(await Session.Api.RawAsync($"scheme/discrepancies/{id}"),
                "This discrepancy's record is shown below.");
        }
        catch (ApiException ex)
        {
            DetailFacts.Clear();
            OnPropertyChanged(nameof(HasDetailFacts));
            DetailNote = ex.Display;
            DetailRaw = ex.Technical;
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
            var question = Text(item, "question_number") ?? Text(item, "question_id");
            var assignment = Text(item, "title") ?? Text(item, "assignment_title")
                             ?? Text(item, "coursework_id");
            var student = Text(item, "student_name");
            var status = PayloadFacts.StatusLabel(
                Text(item, "review_status") ?? Text(item, "status") ?? Text(item, "state"));
            var detail = Text(item, "suspicion") ?? Text(item, "detail")
                         ?? Text(item, "message") ?? Text(item, "reason") ?? string.Empty;

            // A proposal describes a batch rather than one student, so it says
            // how many papers it touches instead of repeating a student name.
            if (string.IsNullOrWhiteSpace(detail)
                && item.TryGetProperty("affected_count", out var affected)
                && affected.ValueKind == JsonValueKind.Number)
            {
                var changed = Text(item, "changed_count") ?? "0";
                detail = $"{affected.GetInt32()} papers checked · {changed} marks would change";
            }

            yield return new SchemeRow
            {
                Id = id,
                Question = string.IsNullOrWhiteSpace(question) ? "—" : $"Q{question!.TrimStart('Q', 'q')}",
                Assignment = string.IsNullOrWhiteSpace(assignment) ? "—" : assignment!,
                Student = student ?? string.Empty,
                Status = status,
                Detail = detail,
            };
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
