using System.Net;
using System.Text.Json;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.Core.Workspace;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

public class DeskCommandTests
{
    [Fact]
    public async Task Opening_sorts_questions_and_selects_the_first_one_that_needs_attention()
    {
        using var handler = new DeskHandler();
        var desk = await OpenAsync(handler);

        Assert.Equal(new[] { "1", "2", "3(a)", "4", "10" },
            desk.Questions.Select(question => question.Number));
        Assert.Equal("3(a)", desk.ActiveQuestion!.Number);
        Assert.True(desk.ActiveQuestion.IsAttention);
        Assert.Equal("Not marked", desk.ActiveQuestion.StateLabel);
        Assert.Equal("0 accepted · 0 changed · 5 to check", desk.ProgressText);
        Assert.Null(desk.Error);
    }

    [Fact]
    public async Task Accept_writes_nothing_and_advances()
    {
        using var handler = new DeskHandler();
        var desk = await OpenAsync(handler);

        await desk.AcceptAndAdvanceAsync();

        Assert.Empty(handler.MarkBodies);
        Assert.Equal(0, handler.Requests.Count(request =>
            request.Method == "POST" && request.Url.Contains("/questions/")));
        Assert.True(desk.Questions[2].IsAccepted);
        Assert.False(desk.Questions[2].IsAttention);
        Assert.Equal("✓", desk.Questions[2].RailGlyph);
        Assert.Equal("4", desk.ActiveQuestion!.Number);
        Assert.Equal("1 accepted · 0 changed · 4 to check", desk.ProgressText);
    }

    [Fact]
    public async Task Staging_the_recorded_value_and_committing_is_an_accept_with_no_write()
    {
        using var handler = new DeskHandler();
        var desk = await OpenAsync(handler);
        desk.ActiveQuestion = desk.Questions[0];

        desk.StageMark(2);
        await desk.CommitDraftAsync();

        Assert.Empty(handler.MarkBodies);
        Assert.True(desk.Questions[0].IsAccepted);
        Assert.False(desk.Questions[0].IsChanged);
        Assert.Equal("2", desk.ActiveQuestion!.Number);
    }

    [Fact]
    public async Task Committing_a_change_posts_one_integer_and_updates_totals_and_ledger()
    {
        using var handler = new DeskHandler();
        var desk = await OpenAsync(handler);
        desk.ActiveQuestion = desk.Questions[0];

        desk.StageMark(3);
        Assert.True(desk.HasDraft);
        Assert.Equal("Set 3", desk.SetMarkText);

        await desk.CommitDraftAsync();

        var body = Assert.Single(handler.MarkBodies);
        Assert.True(DeskHandler.TryReadInt(body, "awarded", out var awarded));
        Assert.Equal(3, awarded);
        Assert.Contains("\"note\":\"Set in the marking desk\"", body);
        Assert.DoesNotContain("3.0", body);

        var question = desk.Questions[0];
        Assert.Equal(3, question.Awarded);
        Assert.True(question.IsChanged);
        Assert.Null(question.Draft);
        Assert.StartsWith("AI 2 → you 3", question.LedgerLine);
        Assert.Equal("Your mark", question.StateLabel);

        Assert.Equal("11 / 16", desk.ScoreText);
        Assert.Equal("2", desk.ActiveQuestion!.Number);
        Assert.Equal("0 accepted · 1 changed · 4 to check", desk.ProgressText);
    }

    [Fact]
    public async Task A_failed_save_keeps_the_draft_and_shows_the_backend_error_inline()
    {
        using var handler = new DeskHandler();
        var desk = await OpenAsync(handler);
        desk.ActiveQuestion = desk.Questions[0];
        handler.MarkResponse = () => DeskHandler.Error(HttpStatusCode.InternalServerError,
            "backend_error", "The marking backend is unavailable.");

        desk.StageMark(1);
        await desk.CommitDraftAsync();

        var question = desk.Questions[0];
        Assert.Equal(2, question.Awarded);
        Assert.Equal(1, question.Draft);
        Assert.Equal("1", desk.ActiveQuestion!.Number);
        Assert.Contains("backend_error", question.Error);
        Assert.True(desk.HasQuestionError);
        Assert.Single(handler.MarkBodies);
        Assert.Equal("10 / 16", desk.ScoreText);
    }

    [Fact]
    public async Task Undo_posts_the_previous_integer_and_restores_the_rail_position()
    {
        using var handler = new DeskHandler();
        var desk = await OpenAsync(handler);
        desk.ActiveQuestion = desk.Questions[0];

        desk.StageMark(3);
        await desk.CommitDraftAsync();
        Assert.Equal("2", desk.ActiveQuestion!.Number);

        await desk.UndoLastChangeAsync();

        Assert.Equal(2, handler.MarkBodies.Count);
        Assert.True(DeskHandler.TryReadInt(handler.MarkBodies[1], "awarded", out var reverted));
        Assert.Equal(2, reverted);
        Assert.Equal("1", desk.ActiveQuestion!.Number);
        Assert.Equal(2, desk.Questions[0].Awarded);
        Assert.StartsWith("Set back to 2", desk.Questions[0].LedgerLine);

        await desk.UndoLastChangeAsync();
        Assert.Equal(2, handler.MarkBodies.Count);
        Assert.Contains("No mark change to undo", desk.Status);
    }

    [Fact]
    public async Task Approving_posts_a_note_without_a_whole_paper_mark_and_refreshes_provenance()
    {
        using var handler = new DeskHandler();
        var desk = await OpenAsync(handler);
        handler.Reviews = DeskHandler.ApprovedHistory;

        desk.OpenApprovePanel();
        Assert.True(desk.IsApprovePanelOpen);
        await desk.ApproveAsync();

        var body = Assert.Single(handler.OverrideBodies);
        using var document = JsonDocument.Parse(body);
        Assert.False(document.RootElement.TryGetProperty("earned", out _));
        Assert.Equal("Approved in the marking desk",
            document.RootElement.GetProperty("note").GetString());

        Assert.Equal("Approved by you", desk.ProvenanceLabel);
        Assert.True(desk.IsApproved);
        Assert.False(desk.CanApprove);
        Assert.False(desk.IsApprovePanelOpen);
    }

    [Fact]
    public async Task Approving_a_partial_correction_requires_a_whole_paper_mark()
    {
        using var handler = new DeskHandler { Detail = DeskHandler.PartialDetailJson };
        var desk = await OpenAsync(handler);
        handler.Reviews = DeskHandler.ApprovedHistory;

        desk.OpenApprovePanel();
        await desk.ApproveAsync();

        Assert.Empty(handler.OverrideBodies);
        Assert.True(desk.ApproveRequiresMark);
        Assert.Contains("incomplete", desk.ApproveError, StringComparison.OrdinalIgnoreCase);

        desk.WholePaperMarkText = "38";
        await desk.ApproveAsync();

        var body = Assert.Single(handler.OverrideBodies);
        Assert.True(DeskHandler.TryReadInt(body, "earned", out var earned));
        Assert.Equal(38, earned);
        Assert.True(desk.IsApproved);
    }

    [Fact]
    public async Task A_409_partial_correction_keeps_the_backend_message_and_retries_with_a_mark()
    {
        using var handler = new DeskHandler();
        var desk = await OpenAsync(handler);
        const string message = "This correction is incomplete (38 of 60 questions have no mark). "
                               + "Enter the student's mark out of 60 to approve it.";
        handler.OverrideResponse = () => DeskHandler.Error(HttpStatusCode.Conflict,
            "partial_correction", message);

        desk.OpenApprovePanel();
        await desk.ApproveAsync();

        Assert.Single(handler.OverrideBodies);
        Assert.Equal(message, desk.ApproveError);
        Assert.True(desk.ApproveRequiresMark);
        Assert.False(desk.IsApproved);

        handler.OverrideResponse = null;
        desk.WholePaperMarkText = "12";
        await desk.ApproveAsync();

        Assert.Equal(2, handler.OverrideBodies.Count);
        Assert.True(DeskHandler.TryReadInt(handler.OverrideBodies[1], "earned", out var earned));
        Assert.Equal(12, earned);
        Assert.Null(desk.ApproveError);
    }

    private static async Task<MarkingDeskViewModel> OpenAsync(DeskHandler handler)
    {
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        var session = new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs());
        var desk = new MarkingDeskViewModel(session, new FakeDecoder());
        await desk.OpenAsync("r1");
        Assert.Null(desk.Error);
        return desk;
    }

    private sealed class FakeDecoder : IPageImageDecoder
    {
        public Task<DecodedPage> DecodeAsync(byte[] bytes,
            CancellationToken cancellationToken = default) =>
            Task.FromResult(new DecodedPage(1000, 1414, null));
    }

    private sealed class AlwaysOurs : IHealthProbe
    {
        public Task<HealthResult> CheckAsync(int port, CancellationToken cancellationToken) =>
            Task.FromResult(new HealthResult(HealthKind.Ours, null));
    }

    private sealed class UnusedStarter : IProcessStarter
    {
        public IProcessHandle Start(BackendLaunchPlan plan) =>
            throw new InvalidOperationException("the test attaches to a backend, it never starts one");
    }

    private sealed class SilentDialogs : IDialogService
    {
        public bool Confirm(string title, string message) => true;
        public void Info(string title, string message)
        {
        }

        public void Error(string title, string message)
        {
        }

        public string? PickOpenFile(string title, string filter) => null;
        public string? PickSaveFile(string title, string filter, string defaultName) => null;
        public string? AskText(string title, string message, string initial = "") => null;
        public void OpenExternal(string pathOrUrl)
        {
        }

        public void OpenFolder(string directory)
        {
        }
    }
}
