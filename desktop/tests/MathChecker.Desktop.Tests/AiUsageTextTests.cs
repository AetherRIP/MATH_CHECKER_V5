using System.Text.Json;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The AI-request ledger as the desk reads it.
///
/// The rule these pin is honesty: a job with no recorded ledger must say
/// nothing (or "not recorded"), never "0 requests", and a measured cost must
/// name the stage that spent it.
/// </summary>
public sealed class AiUsageTextTests
{
    // The desk's own options: the backend speaks snake_case, and the policy
    // has to match or a field silently stays null.
    private static JobDto Job(string json) =>
        JsonSerializer.Deserialize<JobDto>(json, Json.Options)!;

    [Fact]
    public void A_measured_correction_names_its_stages()
    {
        var job = Job("""
            {
              "id": "j1",
              "ai_requests": 3,
              "ai_usage": {
                "requests": 3,
                "total_requests": 5,
                "skipped": 1,
                "by_kind": { "generate": 3, "upload": 1, "file_get": 1 },
                "by_stage": { "mark_scheme": 1, "marking": 2 },
                "budget_exceeded": false
              }
            }
            """);
        Assert.Equal("3 AI requests · mark scheme 1, marking 2 · 1 upload, 1 file lookup "
            + "· 1 not sent (provider cooling)", job.AiRequestsText);
    }

    [Fact]
    public void A_job_without_a_ledger_claims_nothing()
    {
        var job = Job("""{ "id": "j2" }""");
        Assert.Equal(string.Empty, job.AiRequestsText);
    }

    [Fact]
    public void A_budget_stop_is_stated_plainly()
    {
        var job = Job("""
            {
              "id": "j3",
              "ai_usage": {
                "requests": 12,
                "by_stage": { "marking": 12 },
                "budget_exceeded": true
              }
            }
            """);
        Assert.Contains("stopped by the request budget", job.AiRequestsText);
    }

    [Fact]
    public void The_usage_summary_reports_measured_average()
    {
        var summary = JsonSerializer.Deserialize<AiUsageSummaryDto>("""
            { "papers": 15, "requests": 30, "average": 2.0, "max": 6,
              "budget_exceeded": 1, "unavailable": false }
            """, Json.Options)!;
        Assert.Contains("2 per correction", summary.Text);
        Assert.Contains("15 corrections", summary.Text);
        Assert.Contains("max 6", summary.Text);
        Assert.Contains("1 stopped by the request budget", summary.Text);
    }

    [Fact]
    public void An_unmeasured_summary_says_so_instead_of_zero()
    {
        var summary = new AiUsageSummaryDto { Unavailable = true };
        Assert.Contains("No correction has recorded", summary.Text);
    }
}
