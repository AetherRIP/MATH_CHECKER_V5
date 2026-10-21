using System.Collections.ObjectModel;
using System.Text;
using System.Windows;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class AiKeysViewModel : PageViewModel
{
    private KeyStatusDto? _selected;
    private string _newKeys = string.Empty;
    private string _resultText = string.Empty;
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
        set => SetProperty(ref _selected, value);
    }

    public string NewKeys
    {
        get => _newKeys;
        set => SetProperty(ref _newKeys, value);
    }

    public string ResultText
    {
        get => _resultText;
        private set => SetProperty(ref _resultText, value);
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
        ResultText = Json.Pretty(payload);
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
        ResultText = $"{result.Fingerprint}\nstatus: {result.Status}\nauth: {result.AuthOk}\n"
                     + $"generate: {result.GenerateOk}\nresponse: {result.ResponseTimeMs:0} ms\n"
                     + $"error: {result.ErrorKind ?? "—"}\n{result.Message}";
        await LoadAsync();
    });

    public AsyncCommand TestAllCommand => Command(async () =>
    {
        TestingAll = true;
        _testCts = new CancellationTokenSource(TimeSpan.FromMinutes(2));
        var builder = new StringBuilder();
        try
        {
            var dispatcher = Application.Current?.Dispatcher;
            await Session.Api.StreamKeyTestsAsync(async element =>
            {
                var line = Json.Pretty(element);
                if (dispatcher is null)
                {
                    builder.AppendLine(line);
                    return;
                }

                await dispatcher.InvokeAsync(() => ResultText = builder.AppendLine(line).ToString());
            }, _testCts.Token);
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
}
