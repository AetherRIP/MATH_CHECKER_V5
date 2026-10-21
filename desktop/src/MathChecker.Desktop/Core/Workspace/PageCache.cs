using MathChecker.Desktop.Core.Api;

namespace MathChecker.Desktop.Core.Workspace;

/// <summary>
/// Rendered page PNGs, keyed by result, page and dpi. Pages are immutable
/// server-side (the backend caches and ETags them), so a byte cache removes
/// the network and the render cost when a teacher flips back and forth.
///
/// Budgeted by bytes rather than count: a 300 dpi page is several times a
/// 150 dpi one, and an unbounded dictionary of them would quietly cost
/// hundreds of megabytes. In-flight requests for the same key share one fetch.
/// </summary>
public sealed class PageCache
{
    private readonly MathCheckerApiClient _api;
    private readonly long _byteBudget;
    private readonly int _entryBudget;
    private readonly object _gate = new();
    private readonly Dictionary<string, Entry> _entries = new(StringComparer.Ordinal);
    private readonly Dictionary<string, Task<byte[]>> _inFlight = new(StringComparer.Ordinal);

    private long _tick;

    public PageCache(MathCheckerApiClient api, long byteBudget = 120L * 1024 * 1024,
        int entryBudget = 60)
    {
        _api = api;
        _byteBudget = Math.Max(1, byteBudget);
        _entryBudget = Math.Max(1, entryBudget);
    }

    public long Bytes
    {
        get
        {
            lock (_gate)
            {
                return _entries.Values.Sum(entry => (long)entry.Bytes.Length);
            }
        }
    }

    public int Count
    {
        get
        {
            lock (_gate)
            {
                return _entries.Count;
            }
        }
    }

    /// <summary>
    /// The page's PNG bytes, from memory or the backend. A cancelled caller
    /// gets <see cref="OperationCanceledException"/> and never stores a partial
    /// result.
    /// </summary>
    public async Task<byte[]> GetAsync(string resultId, int page, int dpi,
        CancellationToken cancellationToken = default)
    {
        var key = Key(resultId, page, dpi);
        Task<byte[]> shared;
        lock (_gate)
        {
            if (_entries.TryGetValue(key, out var hit))
            {
                hit.Tick = ++_tick;
                return hit.Bytes;
            }

            if (_inFlight.TryGetValue(key, out var running))
            {
                shared = running;
            }
            else
            {
                shared = _api.ResultPagePngAsync(resultId, page, dpi, cancellationToken);
                _inFlight[key] = shared;
            }
        }

        try
        {
            var bytes = await shared.WaitAsync(cancellationToken).ConfigureAwait(false);
            lock (_gate)
            {
                if (!_entries.ContainsKey(key))
                {
                    _entries[key] = new Entry(resultId, page, dpi, bytes, ++_tick);
                    TrimLocked();
                }
            }

            return bytes;
        }
        finally
        {
            lock (_gate)
            {
                if (_inFlight.TryGetValue(key, out var running) && running.IsCompleted)
                {
                    _inFlight.Remove(key);
                }
            }
        }
    }

    /// <summary>
    /// Drops every page that is not the current one or its immediate
    /// neighbours. Called when the pane moves to another paper.
    /// </summary>
    public void TrimToResult(string resultId, int currentPage, int radius = 1)
    {
        lock (_gate)
        {
            var stale = _entries
                .Where(pair => !string.Equals(pair.Value.ResultId, resultId, StringComparison.Ordinal)
                               || Math.Abs(pair.Value.Page - currentPage) > radius)
                .Select(pair => pair.Key)
                .ToList();
            foreach (var key in stale)
            {
                _entries.Remove(key);
            }
        }
    }

    public void Clear()
    {
        lock (_gate)
        {
            _entries.Clear();
        }
    }

    private void TrimLocked()
    {
        while (_entries.Count > _entryBudget
               || _entries.Values.Sum(entry => (long)entry.Bytes.Length) > _byteBudget)
        {
            var victim = _entries.MinBy(pair => pair.Value.Tick);
            if (string.IsNullOrEmpty(victim.Key))
            {
                break;
            }

            _entries.Remove(victim.Key);
        }
    }

    private static string Key(string resultId, int page, int dpi) => $"{resultId}\u001f{page}\u001f{dpi}";

    private sealed class Entry(string resultId, int page, int dpi, byte[] bytes, long tick)
    {
        public string ResultId { get; } = resultId;
        public int Page { get; } = page;
        public int Dpi { get; } = dpi;
        public byte[] Bytes { get; } = bytes;
        public long Tick { get; set; } = tick;
    }
}
