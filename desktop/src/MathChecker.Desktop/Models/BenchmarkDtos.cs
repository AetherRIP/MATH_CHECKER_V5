using System.Text.Json;
using System.Text.Json.Serialization;

namespace MathChecker.Desktop.Models;

/// <summary>
/// The benchmark API's wire shapes. These are deliberately read-only DTOs:
/// the desktop page displays what the backend resolved and appends teacher
/// assessments through the one verify endpoint. It never computes eligibility
/// or a certified score locally.
/// </summary>
public sealed class BenchmarkOverviewDto
{
    public BenchmarkDatasetDto Dataset { get; set; } = new();
    public BenchmarkVerificationDto Verification { get; set; } = new();
    public bool CertifiedAvailable { get; set; }
    public BenchmarkCertifiedAccuracyDto? CertifiedAccuracy { get; set; }
    public string? CertificationReason { get; set; }
    public BenchmarkRunDto? LatestRun { get; set; }
    public string? RunsRoot { get; set; }
}

public sealed class BenchmarkDatasetDto
{
    public string? Path { get; set; }
    public int SchemaVersion { get; set; }
    public string Version { get; set; } = string.Empty;

    [JsonPropertyName("sha256")]
    public string Sha256 { get; set; } = string.Empty;

    public List<BenchmarkPaperDto> Papers { get; set; } = new();
    public int PaperCount { get; set; }
    public int ItemCount { get; set; }
    public int QuestionCount { get; set; }
    public int SubquestionCount { get; set; }
    public int MarksAvailable { get; set; }
    public int SchemeMarks { get; set; }
    public Dictionary<string, int> Partitions { get; set; } = new();
    public Dictionary<string, int> Provenance { get; set; } = new();
    public Dictionary<string, int> Categories { get; set; } = new();
    public List<string> Warnings { get; set; } = new();
    public BenchmarkDatasetReviewDto? Review { get; set; }
}

public sealed class BenchmarkPaperDto
{
    public string Id { get; set; } = string.Empty;
    public string Title { get; set; } = string.Empty;
    public string Board { get; set; } = string.Empty;
    public string Qualification { get; set; } = string.Empty;
    public string Topic { get; set; } = string.Empty;
    public string Partition { get; set; } = string.Empty;
    public string Provenance { get; set; } = string.Empty;
    public int MarksAvailable { get; set; }
    public int ExpectedTotal { get; set; }
    public int SchemeStatedTotal { get; set; }
    public bool SchemeHeaderDisagrees { get; set; }
    public string? TeacherGrade { get; set; }
    public int Items { get; set; }
}

public sealed class BenchmarkDatasetReviewDto
{
    public bool Required { get; set; }
    public string? VerifiedBy { get; set; }
    public string? VerifiedOn { get; set; }
    public string? Instructions { get; set; }
}

public sealed class BenchmarkVerificationDto
{
    public int TotalReferences { get; set; }
    public Dictionary<string, int> Counts { get; set; } = new();
    public int Eligible { get; set; }
    public int EligibleQuestions { get; set; }
    public int EligibleSubquestions { get; set; }
    public int EligibleMarks { get; set; }
    public int TeacherMarks { get; set; }
    public List<BenchmarkExcludedReferenceDto> Unresolved { get; set; } = new();
    public List<BenchmarkExcludedReferenceDto> Excluded { get; set; } = new();
    public Dictionary<string, int> Provenance { get; set; } = new();
}

public sealed class BenchmarkExcludedReferenceDto
{
    public string ReferenceId { get; set; } = string.Empty;
    public string Status { get; set; } = string.Empty;
    public string Reason { get; set; } = string.Empty;
}

public sealed class BenchmarkCertifiedAccuracyDto
{
    public BenchmarkAgreementBlockDto? Items { get; set; }
    public BenchmarkAgreementBlockDto? Questions { get; set; }
    public BenchmarkAgreementBlockDto? Subquestions { get; set; }
    public BenchmarkTotalAgreementDto? Total { get; set; }
    public BenchmarkSampleDto? Sample { get; set; }
    public string? RunId { get; set; }
    public string? FinishedAt { get; set; }
}

public class BenchmarkAgreementBlockDto
{
    public int Exact { get; set; }
    public int Compared { get; set; }
    public double? Rate { get; set; }

    [JsonPropertyName("ci95")]
    public List<double>? Ci95 { get; set; }

    public string? Denominator { get; set; }
}

public sealed class BenchmarkTotalAgreementDto : BenchmarkAgreementBlockDto
{
    public int Scripts { get; set; }
}

public sealed class BenchmarkSampleDto
{
    public int PapersExpected { get; set; }
    public int PapersAttempted { get; set; }
    public int PapersProcessed { get; set; }
    public int ReferencesExpected { get; set; }
    public int QuestionsExpected { get; set; }
    public int SubquestionsExpected { get; set; }
    public int MarksAvailable { get; set; }
    public string? CertificationBasis { get; set; }
    public string? DatasetVersion { get; set; }

    [JsonPropertyName("dataset_sha256")]
    public string? DatasetSha256 { get; set; }

    public string? Partition { get; set; }
}

public sealed class BenchmarkRunDto
{
    public string RunId { get; set; } = string.Empty;
    public string Status { get; set; } = string.Empty;
    public bool Certified { get; set; }
    public BenchmarkCertificationDto? Certification { get; set; }
    public string? StartedAt { get; set; }
    public string? FinishedAt { get; set; }
    public string? Mode { get; set; }
    public string? Model { get; set; }
    public string? DatasetVersion { get; set; }
    public bool DatasetMatches { get; set; }
    public bool VerificationMatches { get; set; }
    public BenchmarkSampleDto? Sample { get; set; }
    public BenchmarkAgreementDto? Agreement { get; set; }
    public BenchmarkErrorDto? Error { get; set; }
    public BenchmarkCoverageDto? Coverage { get; set; }
    public BenchmarkReviewMetricsDto? Review { get; set; }
    public BenchmarkGradesDto? Grades { get; set; }
    public List<BenchmarkDisagreementDto> Disagreements { get; set; } = new();
    public JsonElement? DisagreementGroups { get; set; }
    public JsonElement? ExcludedReasons { get; set; }
    public List<string> Limitations { get; set; } = new();
    public BenchmarkReproductionDto? Reproduction { get; set; }
    public JsonElement? Artifacts { get; set; }
}

public sealed class BenchmarkCertificationDto
{
    public string? Basis { get; set; }
    public bool Eligible { get; set; }
    public string? Label { get; set; }
    public string? Reason { get; set; }
    public int TeacherVerifiedReferences { get; set; }
}

public sealed class BenchmarkAgreementDto
{
    public BenchmarkAgreementBlockDto? Items { get; set; }
    public BenchmarkAgreementBlockDto? Questions { get; set; }
    public BenchmarkAgreementBlockDto? Subquestions { get; set; }
    public BenchmarkTotalAgreementDto? Total { get; set; }
}

public sealed class BenchmarkErrorDto
{
    public double? MeanAbsoluteError { get; set; }
    public double? NormalizedMae { get; set; }
    public BenchmarkDirectionDto? Overmarking { get; set; }
    public BenchmarkDirectionDto? Undermarking { get; set; }
}

public sealed class BenchmarkDirectionDto
{
    public int Count { get; set; }
    public double? Rate { get; set; }
    public double? MeanMagnitude { get; set; }
}

public sealed class BenchmarkCoverageDto
{
    public int QuestionsProcessed { get; set; }
    public double? QuestionCoverage { get; set; }
    public int SubquestionsProcessed { get; set; }
    public double? SubquestionCoverage { get; set; }
    public int ItemsProcessed { get; set; }
    public double? ItemCoverage { get; set; }
    public int MissingMarkCount { get; set; }
    public int InvalidResultCount { get; set; }
    public int ScriptsFailed { get; set; }
    public double? SchemaValidationFailureRate { get; set; }
    public int UnresolvedReferenceCount { get; set; }
}

public sealed class BenchmarkReviewMetricsDto
{
    public double? ReviewFlagRate { get; set; }
    public int FlaggedScripts { get; set; }
    public int FlaggedWithDisagreement { get; set; }
    public int FlaggedWithoutDisagreement { get; set; }
    public double? HumanReviewRate { get; set; }
    public int ItemsRequiringInvestigation { get; set; }
}

public sealed class BenchmarkGradesDto
{
    public int Compared { get; set; }
    public int Exact { get; set; }
    public double? Rate { get; set; }
    public bool Available { get; set; }
    public string? Reason { get; set; }
}

public sealed class BenchmarkDisagreementDto
{
    public string ReferenceId { get; set; } = string.Empty;
    public string PaperId { get; set; } = string.Empty;
    public string Question { get; set; } = string.Empty;
    public string Level { get; set; } = string.Empty;
    public int MaxMarks { get; set; }
    public int TeacherMark { get; set; }
    public int AiMark { get; set; }
    public int Difference { get; set; }
    public string Direction { get; set; } = string.Empty;
    public string? Hypothesis { get; set; }
    public bool NeedsHuman { get; set; }
}

public sealed class BenchmarkReproductionDto
{
    public string? RunId { get; set; }
    public bool Reproduced { get; set; }
    public List<string> Differences { get; set; } = new();
    public string? CheckedAt { get; set; }

    [JsonPropertyName("metrics_sha256")]
    public string? MetricsSha256 { get; set; }
}

public sealed class BenchmarkReferencesDto
{
    public List<BenchmarkReferenceDto> Items { get; set; } = new();
    public int Total { get; set; }
    public string? DatasetVersion { get; set; }

    [JsonPropertyName("dataset_sha256")]
    public string? DatasetSha256 { get; set; }

    public string? RunId { get; set; }
    public List<string> Statuses { get; set; } = new();
    public bool CertifiedAvailable { get; set; }
}

public sealed class BenchmarkReferenceDto
{
    public string ReferenceId { get; set; } = string.Empty;
    public string PaperId { get; set; } = string.Empty;
    public string PaperTitle { get; set; } = string.Empty;
    public string Board { get; set; } = string.Empty;
    public string Qualification { get; set; } = string.Empty;
    public string Topic { get; set; } = string.Empty;
    public string Partition { get; set; } = string.Empty;
    public string Question { get; set; } = string.Empty;
    public string? Subquestion { get; set; }
    public string Level { get; set; } = string.Empty;
    public int MaxMarks { get; set; }
    public int SchemeMark { get; set; }
    public string Provenance { get; set; } = string.Empty;
    public string Prompt { get; set; } = string.Empty;
    public List<string> Scheme { get; set; } = new();
    public List<string> StudentAnswer { get; set; } = new();
    public List<string> Categories { get; set; } = new();
    public string Ruling { get; set; } = string.Empty;
    public string Basis { get; set; } = string.Empty;
    public string? StudentPdf { get; set; }
    public string? SchemePdf { get; set; }
    public BenchmarkReferenceStateDto Reference { get; set; } = new();
    public BenchmarkAiMarkDto? Ai { get; set; }
    public List<BenchmarkAssessmentDto> History { get; set; } = new();
    public string? RunId { get; set; }
}

public sealed class BenchmarkReferenceStateDto
{
    public string ReferenceId { get; set; } = string.Empty;
    public string Status { get; set; } = string.Empty;
    public int? TeacherMark { get; set; }
    public string Reason { get; set; } = string.Empty;
    public bool Eligible { get; set; }
    public List<BenchmarkAssessmentDto> Assessments { get; set; } = new();
}

public sealed class BenchmarkAiMarkDto
{
    public string? RunId { get; set; }
    public int? Mark { get; set; }
    public int? Max { get; set; }
    public int? TeacherMarkAtRun { get; set; }
    public int? Difference { get; set; }
    public string? Direction { get; set; }
    public string? MatchedBy { get; set; }
    public bool ReviewRequired { get; set; }
    public string? Message { get; set; }
}

public sealed class BenchmarkAssessmentDto
{
    public int Seq { get; set; }
    public string ReferenceId { get; set; } = string.Empty;
    public int? Awarded { get; set; }
    public string Status { get; set; } = string.Empty;
    public string Assessor { get; set; } = string.Empty;
    public string Role { get; set; } = string.Empty;
    public string Note { get; set; } = string.Empty;
    public List<string> AlternativeMethods { get; set; } = new();
    public string CreatedAt { get; set; } = string.Empty;
    public string DatasetVersion { get; set; } = string.Empty;
}

public sealed class BenchmarkVerifyResultDto
{
    public string ReferenceId { get; set; } = string.Empty;
    public BenchmarkReferenceStateDto Reference { get; set; } = new();
    public BenchmarkVerificationDto Verification { get; set; } = new();
    public bool ProductionDbModified { get; set; }
    public int ClassroomWrites { get; set; }
}
