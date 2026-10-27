using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class AiKeysViewModel : PageViewModel
{
    /// <summary>How long one key's probe may take. A probe is
    /// <c>models.list</c> plus one tiny generation, each clamped to 30 s in
    /// <c>gemini/tester.py</c>, so 90 s per key leaves room for a slow network
    /// without letting a stuck run hang the page forever.</summary>
    private const int ProbeBudgetSeconds = 90;

    private KeyStatusDto? _selected;
    private string _newKeys = string.Empty;
    private string _resultNote = string.Empty;
    private string _summary = string.Empty;
    private string _workerCount = "2";
    private WorkerStateDto? _workers;
    private bool _testingAll;
    private CancellationTokenSource? _testCts;
    // The thread a "Test all" run was started from (the UI thread). Rows are
    // applied there, never on whatever thread a stream continuation happens to
    // land on.
    private SynchronizationContext? _uiContext;

    // One stable instance per command. Rebuilding them on every property read
    // meant a re-bound page could receive a second "Test all" command while the
    // first was still streaming, and two runs would then fight over this page's
    // state. Every other page asserts the same thing (CorrectionPageTests).
    private AsyncCommand? _addKeysCommand;
    private AsyncCommand? _testSelectedCommand;
    private AsyncCommand? _testAllCommand;
    private AsyncCommand? _enableCommand;
    private AsyncCommand? _disableCommand;
    private AsyncCommand? _removeCommand;
    private AsyncCommand? _resizeWorkersCommand;

    public AiKeysViewModel(AppSession session)
        : base(session, "ai", "AI & keys")
    {
        // Adding or removing a key changes whether "Test all" can run at all.
        Keys.CollectionChanged += (_, _) => OnPropertyChanged(nameof(CanTestAll));
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

    /// <summary>Whether a whole-list test can start: there must be keys, and a
    /// run must not already be in flight.</summary>
    public bool CanTestAll => !TestingAll && Keys.Count > 0;

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
        private set
        {
            if (SetProperty(ref _testingAll, value))
            {
                OnPropertyChanged(nameof(CanTestAll));
            }
        }
    }

    public string WorkerCount
    {
        get => _workerCount;
        set => SetProperty(ref _workerCount, value);
    }

    public string WorkersText => _workers?.Summary ?? "—";

    public AsyncCommand AddKeysCommand => _addKeysCommand ??= Command(async () =>
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
        Status = "Keys submitted. The backend tests them against the real API before activating them.";
    }, after: LoadAsync);

    public AsyncCommand TestSelectedCommand => _testSelectedCommand ??= Command(async () =>
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
    }, after: LoadAsync);

    /// <summary>
    /// Tests every configured key and reports what actually happened.
    ///
    /// The stream carries four kinds of event (<c>start</c>, <c>key_stage</c>,
    /// <c>key_result</c>, <c>done</c>). This page used to treat each one as a
    /// key result, so two keys produced eleven rows — most reading "— · auth
    /// refused · generation failed" — and the completion message reported the
    /// number of EVENTS ("Finished testing 11 keys"). The summary the backend
    /// sends was never read at all. Now: one row per key, progress while it
    /// runs, and the backend's own totals at the end.
    /// </summary>
    public AsyncCommand TestAllCommand => _testAllCommand ??= Command(async () =>
    {
        TestingAll = true;
        _uiContext = SynchronizationContext.Current;
        var planned = Math.Max(1, Keys.Count);
        _testCts = new CancellationTokenSource(TimeSpan.FromSeconds(ProbeBudget(planned)));

        ResultFacts.Clear();
        var rows = new Dictionary<int, int>();          // key index -> row position
        var tally = new KeyTestTally { Total = Keys.Count };
        var problems = new List<string>();
        var stopwatch = Stopwatch.StartNew();

        try
        {
            ShowResult($"Testing {Keys.Count} key(s) against the live API…");

            await Session.Api.StreamKeyTestsAsync(element =>
            {
                switch (Text(element, "type"))
                {
                    case "start":
                        tally.Total = (int)(Number(element, "total") ?? tally.Total);
                        ShowResult($"Testing {tally.Total} key(s) against the live API…");
                        break;

                    case "key_stage":
                        // Live progress for the key being probed.
                        SetRow(rows, Index(element, rows.Count + 1), LabelFor(element),
                            Text(element, "message") ?? "Testing…");
                        break;

                    case "key_result":
                        ApplyResult(element, Index(element, rows.Count + 1), tally, problems, rows);
                        break;

                    case "done":
                        if (element.TryGetProperty("summary", out var summary))
                        {
                            tally.Read(summary);
                        }

                        break;

                    // An event type this build does not know is ignored on
                    // purpose: it is not a result, and guessing would be worse
                    // than saying nothing.
                }

                return Task.CompletedTask;
            }, _testCts.Token);

            stopwatch.Stop();
            if (tally.ElapsedSeconds <= 0)
            {
                tally.ElapsedSeconds = stopwatch.Elapsed.TotalSeconds;
            }

            ShowResult(Describe(tally, problems));
            Status = $"Key test finished — {tally.Passed} healthy, {tally.Failed} failed.";
        }
        catch (OperationCanceledException)
        {
            // The user navigated away, closed the window, or the budget ran out.
            // Saying so is the point: the page must never sit on "Testing…".
            stopwatch.Stop();
            tally.ElapsedSeconds = stopwatch.Elapsed.TotalSeconds;
            ShowResult($"Test cancelled after {tally.Tested} of {tally.Total} keys — "
                       + "the rows above are the results that had arrived.");
        }
        finally
        {
            TestingAll = false;
            _testCts?.Dispose();
            _testCts = null;
        }
    }, after: LoadAsync);

    public AsyncCommand EnableCommand => _enableCommand ??= Command(async () =>
    {
        if (Selected?.Fingerprint is null)
        {
            return;
        }

        await Session.Api.EnableKeyAsync(Selected.Fingerprint);
    }, after: LoadAsync);

    public AsyncCommand DisableCommand => _disableCommand ??= Command(async () =>
    {
        if (Selected?.Fingerprint is null)
        {
            return;
        }

        await Session.Api.DisableKeyAsync(Selected.Fingerprint);
    }, after: LoadAsync);

    public AsyncCommand RemoveCommand => _removeCommand ??= Command(async () =>
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
    }, after: LoadAsync);

    public AsyncCommand ResizeWorkersCommand => _resizeWorkersCommand ??= Command(async () =>
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
        // The listing reports the pool as "size"; "running" is only in the
        // resize reply, so reading it here showed 0 workers on a live pool.
        WorkerCount = _workers.PoolSize.ToString();
        OnPropertyChanged(nameof(WorkersText));
        OnPropertyChanged(nameof(CanTestAll));
    }

    /// <summary>The whole-run budget: room for every key's probe, but always
    /// inside the API client's 10-minute transport timeout, so a slow run ends
    /// with the page's own "Test cancelled after N of M keys" instead of the
    /// transport killing the stream first.</summary>
    private static double ProbeBudget(int keys) =>
        Math.Min(ProbeBudgetSeconds * (double)keys, 9 * 60);

    // ------------------------------------------------------------ test-all rows

    private static int Index(JsonElement element, int fallback) =>
        (int)(Number(element, "index") ?? fallback);

    /// <summary>The label the list already shows for this key, so the result row
    /// and the table row name the same thing. The stream identifies keys by
    /// fingerprint, never by secret.</summary>
    private string LabelFor(JsonElement element)
    {
        var fingerprint = Text(element, "fingerprint");
        var known = Keys.FirstOrDefault(k => k.Fingerprint == fingerprint)?.Label;
        if (!string.IsNullOrWhiteSpace(known))
        {
            return known!;
        }

        var label = Text(element, "label");
        return string.IsNullOrWhiteSpace(label)
            ? $"Key {Index(element, Keys.Count + 1)}"
            : label!;
    }

    private void ApplyResult(JsonElement element, int index, KeyTestTally tally,
        List<string> problems, Dictionary<int, int> rows)
    {
        var status = Text(element, "status") ?? "ERROR";
        var label = LabelFor(element);

        var parts = new List<string> { PayloadFacts.StatusLabel(status) };
        if (Bool(element, "auth_ok") is { } auth)
        {
            parts.Add(auth ? "auth accepted" : "auth refused");
        }

        if (Bool(element, "generate_ok") is { } generated)
        {
            parts.Add(generated ? "generation working" : "generation failed");
        }

        if (Number(element, "response_time_ms") is { } ms)
        {
            parts.Add($"{ms:0} ms");
        }

        var kind = Text(element, "error_kind");
        if (!string.IsNullOrWhiteSpace(kind))
        {
            parts.Add(PayloadFacts.StatusLabel(kind));
        }

        var message = Text(element, "error_message");
        if (!string.IsNullOrWhiteSpace(message))
        {
            parts.Add(message!);
        }

        SetRow(rows, index, label, string.Join(" · ", parts));

        tally.Count(status);
        if (element.TryGetProperty("summary", out var summary))
        {
            tally.Read(summary);
        }

        if (!string.Equals(status, "HEALTHY", StringComparison.OrdinalIgnoreCase))
        {
            // Why it failed, in the page's own words where the backend gave us
            // a sentence, otherwise its short category.
            var reason = string.IsNullOrWhiteSpace(message)
                ? PayloadFacts.StatusLabel(string.IsNullOrWhiteSpace(kind) ? status : kind)
                : message!;
            problems.Add($"{label}: {reason.TrimEnd('.')}.");
        }
    }

    private void SetRow(Dictionary<int, int> rows, int index, string label, string value)
    {
        OnUiThread(() =>
        {
            if (rows.TryGetValue(index, out var position) && position < ResultFacts.Count)
            {
                ResultFacts[position] = new FactRow(label, value);
                return;
            }

            rows[index] = ResultFacts.Count;
            ResultFacts.Add(new FactRow(label, value));
            OnPropertyChanged(nameof(HasResult));
        });
    }

    /// <summary>
    /// Applies a change to the bound collection on the thread that started the
    /// run. The streaming continuation already resumes there, which is the fast
    /// path; anything else is posted back to it.
    ///
    /// This used to ask <c>Application.Current</c> for a dispatcher instead. Two
    /// things were wrong with that: with no application attached it DROPPED
    /// every row and then reported "No keys were tested: add a key first" for a
    /// run that had tested every key, and when an application existed it queued
    /// into that application's dispatcher even when the page was running on a
    /// different thread — so the rows went to a queue nothing was pumping.
    /// </summary>
    private void OnUiThread(Action action)
    {
        var context = _uiContext;
        if (context is null || context == SynchronizationContext.Current)
        {
            action();
            return;
        }

        context.Post(_ => action(), null);
    }

    private static string Describe(KeyTestTally tally, IReadOnlyList<string> problems)
    {
        if (tally.Total == 0)
        {
            return "No keys are configured, so there was nothing to test.";
        }

        var scope = tally.Tested == tally.Total
            ? $"{tally.Total} keys"
            : $"{tally.Tested} of {tally.Total} keys";
        var note = $"Tested {scope} in {tally.ElapsedSeconds:0.#} s — "
                   + $"{tally.Passed} healthy · {tally.Failed} failed · {tally.Skipped} skipped";

        if (problems.Count == 0)
        {
            return note + ".";
        }

        var shown = problems.Take(3).ToList();
        var more = problems.Count - shown.Count;
        return note + ". " + string.Join(" ", shown)
               + (more > 0 ? $" (+{more} more)" : string.Empty);
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

    private static double? Number(JsonElement element, string property) =>
        element.TryGetProperty(property, out var value)
        && value.ValueKind == JsonValueKind.Number
        && value.TryGetDouble(out var parsed)
            ? parsed
            : null;

    private static bool? Bool(JsonElement element, string property)
    {
        if (!element.TryGetProperty(property, out var value))
        {
            return null;
        }

        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => null,
        };
    }

    /// <summary>Totals for one "Test all" run. The backend sends the same
    /// arithmetic in every summary, which is what the page reports; the local
    /// count is only a fallback for a stream that ends without one.</summary>
    private sealed class KeyTestTally
    {
        public int Total { get; set; }
        public int Tested { get; private set; }
        public int Passed { get; private set; }
        public int Failed { get; private set; }
        public int Skipped { get; private set; }
        public double ElapsedSeconds { get; set; }

        public void Count(string status)
        {
            Tested++;
            if (string.Equals(status, "HEALTHY", StringComparison.OrdinalIgnoreCase))
            {
                Passed++;
            }
            else if (string.Equals(status, "SKIPPED", StringComparison.OrdinalIgnoreCase))
            {
                Skipped++;
            }
            else
            {
                Failed++;
            }
        }

        public void Read(JsonElement summary)
        {
            if (summary.ValueKind != JsonValueKind.Object)
            {
                return;
            }

            Total = (int)(Number(summary, "total") ?? Total);
            Tested = (int)(Number(summary, "tested") ?? Tested);
            Passed = (int)(Number(summary, "passed") ?? Passed);
            Failed = (int)(Number(summary, "failed") ?? Failed);
            Skipped = (int)(Number(summary, "skipped") ?? Skipped);
            if (Number(summary, "elapsed_s") is { } elapsed)
            {
                ElapsedSeconds = elapsed;
            }
        }
    }
}
