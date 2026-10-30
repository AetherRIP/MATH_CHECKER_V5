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

    [Fact]
    public void Measured_tokens_and_latency_read_the_backend_shape()
    {
        var summary = JsonSerializer.Deserialize<AiUsageSummaryDto>("""
            {
              "unavailable": false, "papers": 3, "requests": 6, "average": 2.0,
              "ledger": {
                "available": true, "requests": 7, "ok": 6, "failed": 1,
                "input_tokens": 41000, "output_tokens": 900, "cached_tokens": 12000,
                "total_tokens": 41900, "estimated_rows": 0, "cache_hit_rate": 0.2264,
                "latency": { "p50_ms": 8100, "p95_ms": 24000, "max_ms": 31000 }
              }
            }
            """, Json.Options)!;

        Assert.NotNull(summary.Ledger);
        Assert.Contains("41,000 in", summary.Ledger!.TokensText);
        Assert.Contains("1 failed", summary.Ledger.TokensText);
        Assert.Contains("22.6%", summary.Ledger.CacheText);
        Assert.Contains("p95 24,000 ms", summary.Ledger.LatencyText);
        Assert.Contains("reported by the provider", summary.Ledger.EstimatedText);
    }

    [Fact]
    public void An_unknown_token_cost_is_stated_not_zeroed()
    {
        var summary = JsonSerializer.Deserialize<AiUsageSummaryDto>(
            """{ "ledger": { "available": false } }""", Json.Options)!;
        Assert.Contains("Not measured yet", summary.Ledger!.TokensText);
        Assert.Contains("unavailable", summary.Ledger.CacheText);
        Assert.Contains("unavailable", summary.Ledger.LatencyText);
    }

    [Fact]
    public void Estimated_counts_are_labelled_as_estimates()
    {
        var summary = JsonSerializer.Deserialize<AiUsageSummaryDto>("""
            { "ledger": { "available": true, "requests": 4, "estimated_rows": 2 } }
            """, Json.Options)!;
        Assert.Contains("2 of 4 requests carry an ESTIMATE", summary.Ledger!.EstimatedText);
    }

    [Fact]
    public void A_listed_model_is_not_presented_as_a_usable_one()
    {
        var summary = JsonSerializer.Deserialize<AiUsageSummaryDto>("""
            {
              "models": {
                "configured_models": ["gemini-3.6-flash"],
                "production_model": "gemini-3.6-flash",
                "verified_count": 1, "discovered_count": 2,
                "models": [
                  { "model_id": "gemini-3.6-flash", "availability": "verified",
                    "verified": true, "approved_for_production": true },
                  { "model_id": "gemini-3.8-flash", "availability": "listed_not_verified",
                    "discovered": true }
                ]
              }
            }
            """, Json.Options)!;

        var registry = summary.Models!;
        Assert.Contains("gemini-3.6-flash", registry.ProductionText);
        Assert.Contains("1 verified", registry.CoverageText);
        Assert.Equal("verified — this installation has called it successfully",
            registry.Models[0].AvailabilityText);
        Assert.Contains("never verified from here", registry.Models[1].AvailabilityText);
        Assert.Contains("not approved", registry.Models[1].ApprovalText);
        Assert.Contains("teacher-certified", registry.ApprovalNote);
    }

    [Fact]
    public void An_empty_registry_claims_nothing()
    {
        var summary = new AiUsageSummaryDto();
        Assert.Contains("Not measured yet", new UsageViewModelTexts().Tokens);
        Assert.Contains("No model has been discovered", new UsageViewModelTexts().Coverage);
    }

    /// <summary>The fallbacks the page shows when the backend sends nothing.</summary>
    private sealed class UsageViewModelTexts
    {
        public string Tokens => new AiTokenLedgerDto { Available = false }.TokensText;
        public string Coverage => new AiModelRegistryDto().CoverageText;
    }
}
