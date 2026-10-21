using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Media;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Core.Workspace;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public enum PaperPaneState
{
    Idle,
    Checking,
    Available,
    Missing,
    PageFailed,
    Failed,
}

/// <summary>One chip in the page strip; the current page is visually distinct.</summary>
public sealed class PageChipVm : ObservableObject
{
    private bool _isCurrent;

    public PageChipVm(int number)
    {
        Number = number;
    }

    public int Number { get; }

    public bool IsCurrent
    {
        get => _isCurrent;
        set => SetProperty(ref _isCurrent, value);
    }
}

/// <summary>
/// The student's actual paper: the backend's rendered pages, a page strip, and
/// zoom. It owns no marking state — the Desk composes it with the question and
/// mark panes.
///
/// Availability is a first-class state, not an error: the backend says why the
/// original is not stored (retention off, missing copy, unreadable copy, still
/// processing, no job link), and this type shows that reason verbatim instead
/// of substituting a placeholder that looks like a paper.
/// </summary>
public sealed class PaperPaneViewModel : ObservableObject
{
    public const int MinZoom = 50;
    public const int MaxZoom = 300;
    public const int ZoomStep = 25;

    private static readonly TimeSpan ProcessingPollInterval = TimeSpan.FromSeconds(5);

    private readonly AppSession _session;
    private readonly PageCache _cache;
    private readonly IPageImageDecoder _decoder;

    /// <summary>Measures open → first page on screen, per the desk's budget.</summary>
    private readonly System.Diagnostics.Stopwatch _openClock = new();

    private CancellationTokenSource _lifetime = new();
    private CancellationTokenSource? _pageCts;
    private CancellationTokenSource? _pollCts;

    private string? _resultId;
    private ResultPagesDto? _pages;

    private PaperPaneState _state = PaperPaneState.Idle;
    private string? _reason;
    private string? _failureMessage;
    private string? _pageError;
    private string? _fileName;
    private int _pageIndex = 1;
    private int _pageCount;
    private int _zoomPercent = 100;
    private int _imagePixelWidth;
    private int _requestedDpi = 150;
    private bool _isFitWidth = true;
    private bool _isPageLoading;
    private double _viewportWidth;
    private ImageSource? _currentImage;

    public PaperPaneViewModel(AppSession session, PageCache? cache = null,
        IPageImageDecoder? decoder = null)
    {
        _session = session;
        _cache = cache ?? new PageCache(session.Api);
        _decoder = decoder ?? new WpfPageImageDecoder();

        ZoomInCommand = new RelayCommand(() => ZoomBy(ZoomStep));
        ZoomOutCommand = new RelayCommand(() => ZoomBy(-ZoomStep));
        FitWidthCommand = new RelayCommand(FitWidth);
        MissingActionCommand = new RelayCommand(() => _ = ActOnMissingAsync());
        RetryCommand = new RelayCommand(() => _ = CheckAgainAsync());
        RetryPageCommand = new RelayCommand(() => _ = RetryPageAsync());
        GoToPageCommand = new RelayCommand(parameter =>
        {
            if (TryPageNumber(parameter, out var page))
            {
                _ = LoadPageAsync(page);
            }
        });
        NextPageCommand = new RelayCommand(() => _ = LoadPageAsync(PageIndex + 1));
        PreviousPageCommand = new RelayCommand(() => _ = LoadPageAsync(PageIndex - 1));
    }

    // ------------------------------------------------------------------ state

    public PaperPaneState State => _state;
    public string? ResultId => _resultId;

    public bool IsChecking => State == PaperPaneState.Checking;
    public bool IsAvailable => State == PaperPaneState.Available;
    public bool IsMissing => State == PaperPaneState.Missing;
    public bool IsPageFailed => State == PaperPaneState.PageFailed;
    public bool IsFailed => State == PaperPaneState.Failed;

    /// <summary>Set a page to be fetched every five seconds while the backend
    /// reports the correction as still processing. Off in tests.</summary>
    public bool AutoRefreshProcessing { get; set; } = true;

    public string? Reason
    {
        get => _reason;
        private set
        {
            if (SetProperty(ref _reason, value))
            {
                RaiseMissing();
            }
        }
    }

    public string? MissingTitle => TitleFor(Reason);
    public string? MissingBody => _pages?.Message;
    public string? MissingActionLabel => ActionFor(Reason);
    public bool HasMissingAction => !string.IsNullOrWhiteSpace(MissingActionLabel);

    public string? FailureMessage
    {
        get => _failureMessage;
        private set => SetProperty(ref _failureMessage, value);
    }

    public string? PageError
    {
        get => _pageError;
        private set => SetProperty(ref _pageError, value);
    }

    // ------------------------------------------------------------- page state

    public string? FileName
    {
        get => _fileName;
        private set
        {
            if (SetProperty(ref _fileName, value))
            {
                OnPropertyChanged(nameof(HeaderText));
            }
        }
    }

    public int PageCount
    {
        get => _pageCount;
        private set
        {
            if (SetProperty(ref _pageCount, value))
            {
                OnPropertyChanged(nameof(HeaderText));
                OnPropertyChanged(nameof(PageLabel));
                OnPropertyChanged(nameof(ImageAutomationName));
                OnPropertyChanged(nameof(ShowPager));
            }
        }
    }

    public int PageIndex
    {
        get => _pageIndex;
        private set
        {
            if (SetProperty(ref _pageIndex, value))
            {
                OnPropertyChanged(nameof(PageLabel));
                OnPropertyChanged(nameof(ImageAutomationName));
            }
        }
    }

    public bool IsPageLoading
    {
        get => _isPageLoading;
        private set => SetProperty(ref _isPageLoading, value);
    }

    public ImageSource? CurrentImage
    {
        get => _currentImage;
        private set
        {
            if (SetProperty(ref _currentImage, value))
            {
                OnPropertyChanged(nameof(ImageAutomationName));
            }
        }
    }

    public int ImagePixelWidth
    {
        get => _imagePixelWidth;
        private set
        {
            if (SetProperty(ref _imagePixelWidth, value))
            {
                OnPropertyChanged(nameof(DisplayWidth));
            }
        }
    }

    public int RequestedDpi
    {
        get => _requestedDpi;
        private set
        {
            if (SetProperty(ref _requestedDpi, value))
            {
                OnPropertyChanged(nameof(DisplayWidth));
            }
        }
    }

    /// <summary>
    /// Width in device-independent pixels. Derived from the pixels the backend
    /// rendered and the dpi that was asked for, so switching between 150 and
    /// 300 dpi never changes how large the paper appears at a given zoom.
    /// </summary>
    public double DisplayWidth =>
        ImagePixelWidth <= 0
            ? 0
            : ImagePixelWidth * 96.0 / Math.Max(1, RequestedDpi) * (ZoomPercent / 100.0);

    public string HeaderText => State is PaperPaneState.Missing or PaperPaneState.Failed
        ? "Student's work"
        : PageCount > 0
            ? $"{FileName ?? "Student's work"} · {PageCount} page{(PageCount == 1 ? string.Empty : "s")}"
            : "Student's work";

    public string PageLabel => PageCount > 0 && State is PaperPaneState.Available or PaperPaneState.PageFailed
        ? $"Page {PageIndex} of {PageCount}"
        : string.Empty;

    public string ImageAutomationName =>
        $"Page {PageIndex} of {Math.Max(1, PageCount)} of the student's work";

    /// <summary>Page chips stay usable after a single page render failed, so
    /// the teacher can try a neighbouring page.</summary>
    public bool ShowPager => PageCount > 0 && State is PaperPaneState.Available
        or PaperPaneState.PageFailed;

    public ObservableCollection<PageChipVm> PageChips { get; } = new();

    // ---------------------------------------------------------------- zooming

    public int ZoomPercent
    {
        get => _zoomPercent;
        private set
        {
            if (SetProperty(ref _zoomPercent, value))
            {
                OnPropertyChanged(nameof(ZoomText));
                OnPropertyChanged(nameof(DisplayWidth));
            }
        }
    }

    public string ZoomText => $"{ZoomPercent}%";

    public bool IsFitWidth
    {
        get => _isFitWidth;
        private set => SetProperty(ref _isFitWidth, value);
    }

    public void SetViewportWidth(double width)
    {
        if (width <= 0)
        {
            return;
        }

        _viewportWidth = width;
        if (IsFitWidth && ImagePixelWidth > 0)
        {
            ApplyFit();
            QueueDpiReloadIfNeeded();
        }
    }

    // --------------------------------------------------------------- commands

    public RelayCommand ZoomInCommand { get; }
    public RelayCommand ZoomOutCommand { get; }
    public RelayCommand FitWidthCommand { get; }
    public RelayCommand MissingActionCommand { get; }
    public RelayCommand RetryCommand { get; }
    public RelayCommand RetryPageCommand { get; }
    public RelayCommand GoToPageCommand { get; }
    public RelayCommand NextPageCommand { get; }
    public RelayCommand PreviousPageCommand { get; }

    /// <summary>The page load started most recently. Exposed for tests; the
    /// view never awaits it (loading must not block the rail).</summary>
    internal Task PendingLoad { get; private set; } = Task.CompletedTask;

    internal Task PrefetchTask { get; private set; } = Task.CompletedTask;

    internal Task WaitForPrefetchAsync() => PrefetchTask;

    // ------------------------------------------------------------------ loads

    public async Task OpenAsync(string resultId, CancellationToken cancellationToken = default)
    {
        CancelAll();
        _lifetime.Dispose();
        _lifetime = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        _resultId = resultId;
        _openClock.Restart();
        await LoadMetadataAsync(_lifetime.Token);
    }

    public async Task CheckAgainAsync()
    {
        if (string.IsNullOrEmpty(_resultId))
        {
            return;
        }

        CancelPoll();
        await LoadMetadataAsync(_lifetime.Token);
    }

    public Task LoadPageAsync(int page)
    {
        var task = LoadPageCoreAsync(page);
        PendingLoad = task;
        return task;
    }

    private async Task LoadMetadataAsync(CancellationToken token)
    {
        if (string.IsNullOrEmpty(_resultId))
        {
            return;
        }

        SetState(PaperPaneState.Checking);
        _pageCts?.Cancel();
        _pageCts?.Dispose();
        _pageCts = null;
        IsPageLoading = false;
        FailureMessage = null;
        PageError = null;
        Reason = null;
        FileName = null;
        PageCount = 0;
        PageIndex = 1;
        ImagePixelWidth = 0;
        CurrentImage = null;
        PageChips.Clear();

        try
        {
            var pages = await _session.Api.ResultPagesAsync(_resultId, token);
            token.ThrowIfCancellationRequested();

            if (!pages.Available)
            {
                ApplyMissing(pages);
                return;
            }

            CancelPoll();
            _pages = pages;
            FileName = pages.FileName;
            PageCount = pages.PageCount;

            if (PageCount <= 0)
            {
                ApplyMissing(new ResultPagesDto
                {
                    ResultId = _resultId,
                    Available = false,
                    Reason = "file_unreadable",
                    Message = "The backend reported this submission as available but with no readable pages.",
                    Retryable = true,
                });
                return;
            }

            BuildChips();
            SetState(PaperPaneState.Available);
            await LoadPageAsync(1);
            _cache.TrimToResult(_resultId, PageIndex);
        }
        catch (OperationCanceledException)
        {
            // Navigating away or opening another paper.
        }
        catch (ApiException ex)
        {
            Fail(ex.Display);
        }
        catch (Exception ex)
        {
            _session.Log.Error("paper metadata failed", ex);
            Fail("The paper could not be checked: " + ex.Message);
        }
    }

    private async Task LoadPageCoreAsync(int page)
    {
        if (string.IsNullOrEmpty(_resultId) || _pages is not { Available: true } || PageCount <= 0)
        {
            return;
        }

        var target = Math.Clamp(page, 1, PageCount);
        PageIndex = target;
        UpdateChips();

        _pageCts?.Cancel();
        _pageCts?.Dispose();
        _pageCts = CancellationTokenSource.CreateLinkedTokenSource(_lifetime.Token);
        var token = _pageCts.Token;

        SetState(PaperPaneState.Available);
        IsPageLoading = true;
        PageError = null;

        try
        {
            var dpi = DpiForZoom(ZoomPercent);
            var bytes = await _cache.GetAsync(_resultId, target, dpi, token);
            token.ThrowIfCancellationRequested();

            var decoded = await _decoder.DecodeAsync(bytes, token);
            token.ThrowIfCancellationRequested();

            RequestedDpi = dpi;
            ImagePixelWidth = decoded.PixelWidth;
            CurrentImage = decoded.Source;

            if (target == 1 && _openClock.IsRunning)
            {
                // Evidence for the desk's first-paint budget; the id is enough
                // to correlate with the backend log and carries no student name.
                _openClock.Stop();
                _session.Log.Info($"paper: {_resultId} first page ready in "
                                  + $"{_openClock.ElapsedMilliseconds} ms");
            }

            if (IsFitWidth)
            {
                ApplyFit();
            }

            if (!token.IsCancellationRequested)
            {
                PrefetchTask = PrefetchAsync(target, dpi, token);
            }

            QueueDpiReloadIfNeeded();
        }
        catch (OperationCanceledException)
        {
            // A newer page request superseded this one.
        }
        catch (ApiException ex)
        {
            PageError = ex.Display;
            SetState(PaperPaneState.PageFailed);
        }
        catch (Exception ex)
        {
            _session.Log.Error($"page {target} failed", ex);
            PageError = "The page could not be shown: " + ex.Message;
            SetState(PaperPaneState.PageFailed);
        }
        finally
        {
            if (!token.IsCancellationRequested)
            {
                IsPageLoading = false;
            }
        }
    }

    private async Task RetryPageAsync()
    {
        if (State == PaperPaneState.PageFailed)
        {
            await LoadPageAsync(PageIndex);
            return;
        }

        await CheckAgainAsync();
    }

    private async Task PrefetchAsync(int page, int dpi, CancellationToken token)
    {
        foreach (var neighbour in new[] { page - 1, page + 1 })
        {
            if (neighbour < 1 || neighbour > PageCount)
            {
                continue;
            }

            try
            {
                await _cache.GetAsync(_resultId!, neighbour, dpi, token);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (Exception ex)
            {
                // A warm-up failure must never surface: the real load will
                // report the same backend reason with its own retry.
                _session.Log.Info($"page {neighbour} prefetch skipped: {ex.Message}");
                return;
            }
        }
    }

    // --------------------------------------------------------------- missing

    private void ApplyMissing(ResultPagesDto pages)
    {
        _pages = pages;
        Reason = pages.Reason;
        FileName = null;
        PageCount = 0;
        ImagePixelWidth = 0;
        CurrentImage = null;
        PageChips.Clear();
        SetState(PaperPaneState.Missing);
        RaiseMissing();

        if (string.Equals(pages.Reason, "processing", StringComparison.Ordinal))
        {
            StartProcessingPoll();
        }
    }

    private void Fail(string message)
    {
        FailureMessage = message;
        ImagePixelWidth = 0;
        CurrentImage = null;
        SetState(PaperPaneState.Failed);
    }

    private async Task ActOnMissingAsync()
    {
        switch (Reason)
        {
            case "retention_disabled":
                _session.GoTo("privacy");
                return;
            case "file_unreadable":
                _session.GoTo("correct");
                return;
            default:
                await CheckAgainAsync();
                return;
        }
    }

    private void StartProcessingPoll()
    {
        if (!AutoRefreshProcessing || _pollCts is { IsCancellationRequested: false })
        {
            return;
        }

        CancelPoll();
        _pollCts = new CancellationTokenSource();
        var token = _pollCts.Token;
        _ = Task.Run(async () =>
        {
            try
            {
                while (!token.IsCancellationRequested)
                {
                    await Task.Delay(ProcessingPollInterval, token).ConfigureAwait(false);
                    await OnUiAsync(async () =>
                    {
                        if (State == PaperPaneState.Missing && Reason == "processing")
                        {
                            await LoadMetadataAsync(_lifetime.Token);
                        }
                    }).ConfigureAwait(false);

                    if (State != PaperPaneState.Missing || Reason != "processing")
                    {
                        break;
                    }
                }
            }
            catch (OperationCanceledException)
            {
                // The pane moved on.
            }
            catch (Exception ex)
            {
                _session.Log.Warn("paper availability poll failed: " + ex.Message);
            }
        }, token);
    }

    private void CancelPoll()
    {
        _pollCts?.Cancel();
        _pollCts?.Dispose();
        _pollCts = null;
    }

    private void CancelAll()
    {
        CancelPoll();
        _pageCts?.Cancel();
        _pageCts?.Dispose();
        _pageCts = null;
        _lifetime.Cancel();
    }

    private static Task OnUiAsync(Func<Task> action)
    {
        var dispatcher = Application.Current?.Dispatcher;
        if (dispatcher is null || dispatcher.CheckAccess())
        {
            return action();
        }

        return dispatcher.InvokeAsync(action).Task.Unwrap();
    }

    // ------------------------------------------------------------------ zoom

    private void ZoomBy(int delta)
    {
        if (!IsAvailable)
        {
            return;
        }

        IsFitWidth = false;
        SetZoom(ZoomPercent + delta);
    }

    private void SetZoom(int percent)
    {
        var value = Math.Clamp(percent, MinZoom, MaxZoom);
        if (value == ZoomPercent)
        {
            return;
        }

        ZoomPercent = value;
        QueueDpiReloadIfNeeded();
    }

    private void FitWidth()
    {
        if (!IsAvailable)
        {
            return;
        }

        IsFitWidth = true;
        ApplyFit();
        QueueDpiReloadIfNeeded();
    }

    private void ApplyFit()
    {
        if (ImagePixelWidth <= 0 || _viewportWidth <= 0)
        {
            return;
        }

        var baseWidth = ImagePixelWidth * 96.0 / Math.Max(1, RequestedDpi);
        if (baseWidth <= 0)
        {
            return;
        }

        var percent = (int)Math.Round(_viewportWidth / baseWidth * 100.0);
        ZoomPercent = Math.Clamp(percent, MinZoom, MaxZoom);
    }

    /// <summary>
    /// A sharper render is only worth fetching when the zoom crosses a bucket.
    /// The rendered size stays constant because width is derived from pixels
    /// and the dpi that was requested.
    /// </summary>
    private void QueueDpiReloadIfNeeded()
    {
        if (!IsAvailable || ImagePixelWidth <= 0)
        {
            return;
        }

        if (DpiForZoom(ZoomPercent) != RequestedDpi)
        {
            _ = LoadPageAsync(PageIndex);
        }
    }

    private static int DpiForZoom(int zoomPercent) =>
        zoomPercent > 225 ? 300 : zoomPercent > 150 ? 220 : 150;

    // ------------------------------------------------------------- page chips

    private void BuildChips()
    {
        PageChips.Clear();
        for (var page = 1; page <= PageCount; page++)
        {
            PageChips.Add(new PageChipVm(page) { IsCurrent = page == PageIndex });
        }
    }

    private void UpdateChips()
    {
        if (PageChips.Count != PageCount)
        {
            BuildChips();
            return;
        }

        foreach (var chip in PageChips)
        {
            chip.IsCurrent = chip.Number == PageIndex;
        }
    }

    private static bool TryPageNumber(object? parameter, out int page)
    {
        switch (parameter)
        {
            case int number:
                page = number;
                return true;
            case string text when int.TryParse(text, out var parsed):
                page = parsed;
                return true;
            default:
                page = 0;
                return false;
        }
    }

    // -------------------------------------------------------------- state text

    private void SetState(PaperPaneState state)
    {
        if (_state == state)
        {
            return;
        }

        _state = state;
        OnPropertyChanged(nameof(State));
        OnPropertyChanged(nameof(IsChecking));
        OnPropertyChanged(nameof(IsAvailable));
        OnPropertyChanged(nameof(IsMissing));
        OnPropertyChanged(nameof(IsPageFailed));
        OnPropertyChanged(nameof(IsFailed));
        OnPropertyChanged(nameof(HeaderText));
        OnPropertyChanged(nameof(PageLabel));
        OnPropertyChanged(nameof(ShowPager));
    }

    private void RaiseMissing()
    {
        OnPropertyChanged(nameof(MissingTitle));
        OnPropertyChanged(nameof(MissingBody));
        OnPropertyChanged(nameof(MissingActionLabel));
        OnPropertyChanged(nameof(HasMissingAction));
    }

    private static string? TitleFor(string? reason) => reason switch
    {
        "retention_disabled" => "The student's work is not stored here",
        "file_missing" => "This paper's original file is missing",
        "file_unreadable" => "The stored copy cannot be read",
        "processing" => "The paper is still being processed",
        "no_link" => "This result has no stored submission",
        null => null,
        _ => "The student's work is not stored here",
    };

    private static string? ActionFor(string? reason) => reason switch
    {
        "retention_disabled" => "Retention settings",
        "file_missing" => "Re-check storage",
        "file_unreadable" => "Re-correct paper",
        "processing" => "Check again",
        _ => null,
    };
}
