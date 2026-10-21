using System.Text;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Workspace;

namespace MathChecker.Desktop.Tests;

public class PageCacheTests
{
    [Fact]
    public async Task A_page_is_fetched_once_and_reused()
    {
        using var handler = new PageHandler();
        using var api = Client(handler);
        var cache = new PageCache(api);

        var first = await cache.GetAsync("r1", 1, 150);
        var second = await cache.GetAsync("r1", 1, 150);

        Assert.Equal("PAGE1", Encoding.UTF8.GetString(first));
        Assert.Same(first, second);
        Assert.Equal(1, handler.ImageRequests);
        Assert.Equal(1, cache.Count);
        Assert.Equal(5, cache.Bytes);
    }

    [Fact]
    public async Task A_different_dpi_is_a_different_render()
    {
        using var handler = new PageHandler();
        using var api = Client(handler);
        var cache = new PageCache(api);

        await cache.GetAsync("r1", 1, 150);
        await cache.GetAsync("r1", 1, 220);

        Assert.Equal(2, handler.ImageRequests);
        Assert.Equal(2, cache.Count);
    }

    [Fact]
    public async Task The_byte_budget_evicts_the_least_recently_used_page()
    {
        using var handler = new PageHandler();
        using var api = Client(handler);
        var cache = new PageCache(api, byteBudget: 10, entryBudget: 10);

        await cache.GetAsync("r1", 1, 150);
        await cache.GetAsync("r1", 2, 150);
        await cache.GetAsync("r1", 3, 150);

        Assert.Equal(2, cache.Count);
        Assert.Equal(10, cache.Bytes);

        // Page 1 was evicted, so this must go back to the backend.
        await cache.GetAsync("r1", 1, 150);
        Assert.Equal(4, handler.ImageRequests);
        Assert.Contains(1, handler.ImagePages);
    }

    [Fact]
    public async Task Trim_keeps_only_the_current_paper_neighbourhood()
    {
        using var handler = new PageHandler();
        using var api = Client(handler);
        var cache = new PageCache(api);

        await cache.GetAsync("r1", 1, 150);
        await cache.GetAsync("r1", 2, 150);
        await cache.GetAsync("r1", 3, 150);
        await cache.GetAsync("r2", 1, 150);
        Assert.Equal(4, cache.Count);

        cache.TrimToResult("r1", currentPage: 1, radius: 0);

        Assert.Equal(1, cache.Count);
        Assert.Equal(5, cache.Bytes);

        // The surviving entry is the cached one, not a refetch.
        await cache.GetAsync("r1", 1, 150);
        Assert.Equal(4, handler.ImageRequests);
    }

    [Fact]
    public async Task Concurrent_requests_for_one_page_share_a_single_fetch()
    {
        using var handler = new PageHandler
        {
            Gate = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously),
        };
        using var api = Client(handler);
        var cache = new PageCache(api);

        var first = cache.GetAsync("r1", 1, 150);
        var second = cache.GetAsync("r1", 1, 150);

        for (var attempt = 0; attempt < 200 && handler.ImageRequests < 1; attempt++)
        {
            await Task.Delay(5);
        }

        Assert.Equal(1, handler.ImageRequests);
        handler.Gate!.SetResult();

        Assert.Equal("PAGE1", Encoding.UTF8.GetString(await first));
        Assert.Equal("PAGE1", Encoding.UTF8.GetString(await second));
        Assert.Equal(1, handler.ImageRequests);
        Assert.Equal(1, cache.Count);
    }

    private static MathCheckerApiClient Client(PageHandler handler)
    {
        var client = new MathCheckerApiClient(handler);
        client.SetBaseUrl("http://127.0.0.1:8642");
        return client;
    }
}
