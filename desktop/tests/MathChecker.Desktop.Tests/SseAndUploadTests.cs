using System.Text;
using MathChecker.Desktop.Core.Api;

namespace MathChecker.Desktop.Tests;

public class SseAndUploadTests
{
    [Fact]
    public async Task Job_stream_skips_hello_keepalive_and_malformed_events()
    {
        const string stream =
            "data: {\"type\":\"hello\"}\n\n"
            + ": keepalive\n\n"
            + "data: {\"id\":\"j1\",\"status\":\"MARKING\",\"progress\":0.5,\"student_name\":\"Student A\"}\n\n"
            + "data: not-json\n\n"
            + "data: {\"id\":\"j2\",\"status\":\"COMPLETED\",\"progress\":1.0}\n\n";

        var jobs = new List<string>();
        using var memory = new MemoryStream(Encoding.UTF8.GetBytes(stream));
        await SseParser.ReadJobsAsync(memory, job =>
        {
            jobs.Add(job.Id + ":" + job.Status);
            return Task.CompletedTask;
        }, CancellationToken.None);

        Assert.Equal(new[] { "j1:MARKING", "j2:COMPLETED" }, jobs);
    }

    [Fact]
    public async Task Event_stream_can_be_cancelled()
    {
        var stream = new MemoryStream(Encoding.UTF8.GetBytes(
            "data: {\"a\":1}\n\ndata: {\"a\":2}\n\n"));
        using var cts = new CancellationTokenSource();
        cts.Cancel();

        var seen = 0;
        await SseParser.ReadAsync(stream, _ =>
        {
            seen++;
            return Task.CompletedTask;
        }, cts.Token);

        Assert.Equal(0, seen);
    }

    [Fact]
    public async Task Upload_progress_reaches_one_hundred_percent()
    {
        var bytes = new byte[50_000];
        new Random(42).NextBytes(bytes);
        var reports = new List<double>();
        var progress = new Progress<double>(value => reports.Add(value));
        var content = new ProgressStreamContent(new MemoryStream(bytes), bytes.Length, progress);

        using var sink = new MemoryStream();
        await content.CopyToAsync(sink);

        Assert.Equal(bytes.Length, sink.Length);
        Assert.Single(reports);
        Assert.Equal(1d, reports[0], 3);
    }

    [Fact]
    public void Content_type_is_guessed_from_the_extension()
    {
        Assert.Equal("application/pdf", Multipart.GuessContentType("paper.PDF"));
        Assert.Equal("image/png", Multipart.GuessContentType("page.png"));
        Assert.Equal("application/octet-stream", Multipart.GuessContentType("notes.docx"));
    }
}
