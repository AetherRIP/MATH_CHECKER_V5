using System.Collections.ObjectModel;
using System.Text.Json;
using System.Windows;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class AiKeysViewModel : PageViewModel
{
    private KeyStatusDto? _selected;
    private string _newKeys = string.Empty;
    private string _resultNote = string.Empty;
    private string _summary = string.Empty;
    private string _workerCount = "2";
    private WorkerStateDto? _workers;
    private bool _testingAll;
    private CancellationTokenSource? _testCts;

    public AiKeysViewModel(AppSession session)
        : base(session, "ai", "AI & keys")
    {
    }

    public ObservableCollection<KeyStatusDto> Keys { get; } = new();

    public KeyStatusDto? Selected
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

    /// <summary>Test, Disable, Re-enable and Remove all need one key; each one
    /// returns early without it, so they stay disabled instead.</summary>
    public bool HasSelection => _selected?.Fingerprint is not null;

    public string NewKeys
    {
        get => _newKeys;
        set => SetProperty(ref _newKeys, value);
    }

    /// <summary>What the backend did with the keys, as labelled facts instead
    /// of a JSON blob in a monospace box.</summary>
    public ObservableCollection<FactRow> ResultFacts { get; } = new();

    public string ResultNote
    {
        get => _resultNote;
        private set
        {
            if (SetProperty(ref _resultNote, value))
            {
                OnPropertyChanged(nameof(HasResult));
            }
        }
    }

    public bool HasResult => ResultFacts.Count > 0 || !string.IsNullOrWhiteSpace(ResultNote);

    private void ShowResult(string note)
    {
        ResultNote = note;
        OnPropertyChanged(nameof(HasResult));
    }

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    public bool TestingAll
    {
        get => _testingAll;
        private set => SetProperty(ref _testingAll, value);
    }

    public string WorkerCount
    {
        get => _workerCount;
        set => SetProperty(ref _workerCount, value);
    }

    public string WorkersText => _workers?.Summary ?? "—";

    public AsyncCommand AddKeysCommand => Command(async () =>
    {
        var keys = NewKeys
            .Split(new[] { '\r', '\n', ',', ';' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Where(k => k.Length > 0)
            .ToArray();
        if (keys.Length == 0)
        {
            Error = "Paste at least one key.";
            return;
        }

        var payload = await Session.Api.AddKeysAsync(keys);
        NewKeys = string.Empty;
        ResultFacts.Clear();
        foreach (var fact in PayloadFacts.Of(payload))
        {
            ResultFacts.Add(fact);
        }

        var summary = PayloadFacts.Summarize(payload);
        ShowResult(string.IsNullOrWhiteSpace(summary)
            ? "Keys submitted. The backend tested them against the real API before storing them."
            : summary);
        await LoadAsync();
        Status = "Keys submitted. The backend tests them against the real API before activating them.";
    });

    public AsyncCommand TestSelectedCommand => Command(async () =>
    {
        if (Selected?.Fingerprint is null)
        {
            return;
        }

        var result = await Session.Api.TestKeyAsync(Selected.Fingerprint);
        ResultFacts.Clear();
        ResultFacts.Add(new FactRow("Key", Selected.Label ?? Selected.Fingerprint ?? "Selected key"));
        ResultFacts.Add(new FactRow("Status", PayloadFacts.StatusLabel(result.Status)));
        ResultFacts.Add(new FactRow("Authentication", result.AuthOk ? "Accepted" : "Refused"));
        ResultFacts.Add(new FactRow("Generation", result.GenerateOk ? "Working" : "Failed"));
        if (result.ResponseTimeMs is not null)
        {
            ResultFacts.Add(new FactRow("Response time", $"{result.ResponseTimeMs:0} ms"));
        }

        if (!string.IsNullOrWhiteSpace(result.ErrorKind))
        {
            ResultFacts.Add(new FactRow("Problem", PayloadFacts.StatusLabel(result.ErrorKind)));
        }

        ShowResult(string.IsNullOrWhiteSpace(result.Message)
            ? "The backend finished testing this key."
            : result.Message!);
        await LoadAsync();
    });

    public AsyncCommand TestAllCommand => Command(async () =>
    {
        TestingAll = true;
        _testCts = new CancellationTokenSource(TimeSpan.FromMinutes(2));
        ResultFacts.Clear();
        ShowResult("Testing each key against the live API…");
        var index = 0;
        try
        {
            var dispatcher = Application.Current?.Dispatcher;
            await Session.Api.StreamKeyTestsAsync(async element =>
            {
                index++;
                var label = Text(element, "label") ?? Text(element, "fingerprint") ?? $"Key {index}";
                var status = PayloadFacts.StatusLabel(Text(element, "status"));
                var auth = element.TryGetProperty("auth_ok", out var a) && a.ValueKind == JsonValueKind.True;
                var generate = element.TryGetProperty("generate_ok", out var g) && g.ValueKind == JsonValueKind.True;
                var ms = element.TryGetProperty("response_time_ms", out var t) && t.TryGetDouble(out var parsed)
                    ? $" · {parsed:0} ms"
                    : string.Empty;
                var value = $"{status} · auth {(auth ? "accepted" : "refused")} · "
                            + $"generation {(generate ? "working" : "failed")}{ms}";

                if (dispatcher is null)
                {
                    return;
                }

                await dispatcher.InvokeAsync(() =>
                {
                    ResultFacts.Add(new FactRow(label!, value));
                    OnPropertyChanged(nameof(HasResult));
                });
            }, _testCts.Token);
            ShowResult(ResultFacts.Count == 0
                ? "No keys were tested: add a key first."
                : $"Finished testing {ResultFacts.Count} keys against the live API.");
            Status = "Key test finished.";
        }
        finally
        {
            TestingAll = false;
            _testCts?.Dispose();
            _testCts = null;
            await LoadAsync();
        }
    });

    public AsyncCommand EnableCommand => Command(async () =>
    {
        if (Selected?.Fingerprint is null)
        {
            return;
        }

        await Session.Api.EnableKeyAsync(Selected.Fingerprint);
        await LoadAsync();
    });

    public AsyncCommand DisableCommand => Command(async () =>
    {
        if (Selected?.Fingerprint is null)
        {
            return;
        }

        await Session.Api.DisableKeyAsync(Selected.Fingerprint);
        await LoadAsync();
    });

    public AsyncCommand RemoveCommand => Command(async () =>
    {
        if (Selected?.Fingerprint is null)
        {
            return;
        }

        if (!Session.Dialogs.Confirm("Remove key",
                $"Remove {Selected.Label ?? Selected.Fingerprint}? Corrections will no longer use it."))
        {
            return;
        }

        await Session.Api.RemoveKeyAsync(Selected.Fingerprint);
        await LoadAsync();
    });

    public AsyncCommand ResizeWorkersCommand => Command(async () =>
    {
        if (!int.TryParse(WorkerCount, out var count) || count is < 1 or > 8)
        {
            Error = "The worker count must be between 1 and 8.";
            return;
        }

        _workers = await Session.Api.ResizeWorkersAsync(count);
        OnPropertyChanged(nameof(WorkersText));
        Status = "Worker pool resized.";
    });

    public override void OnDeactivated()
    {
        _testCts?.Cancel();
        base.OnDeactivated();
    }

    protected override async Task LoadCoreAsync()
    {
        var keys = await Session.Api.KeysAsync();
        Keys.Clear();
        foreach (var key in keys.Keys)
        {
            Keys.Add(key);
        }

        Summary = $"{keys.Total} keys · {keys.Healthy} healthy · {keys.Cooldown} cooling · "
                  + $"{keys.Invalid} invalid · {keys.Disabled} disabled";
        _workers = await Session.Api.WorkersAsync();
        WorkerCount = _workers.Running.ToString();
        OnPropertyChanged(nameof(WorkersText));
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
            JsonValueKind.Number => value.ToString(),
            _ => null,
        };
    }
}
