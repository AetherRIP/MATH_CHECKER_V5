# 06 · The Marking Desk — implementation spec

Status: design complete. Phase D0 (workspace DTOs, integer client methods, page-bytes helper, contract tests) and D1 (paper pane: page strip, byte cache, zoom, prefetch, availability states) are implemented; D2–D4 are not.
Scope: one screen (`MarkingDeskView`) plus the DTO/client work it needs.
Non-goals: no Python changes, no new backend endpoint, no ink annotations on the paper, no mark-scheme page viewer for ad-hoc uploads (the backend only renders scheme pages for a stored assignment).

This document is written to be implemented without further design decisions. Where a
decision depends on product sign-off it is marked **DECISION**.

---

## 1. What the teacher is doing on this screen

Three jobs, in this order, repeated per paper:

1. **Orient** — whose paper is this, how many marks, what did the engine distrust?
2. **Decide** — for each question: accept, or change the mark, or leave it for later.
3. **Close** — record that the paper has been reviewed, so it leaves the queue.

Every layout and interaction choice below serves those three jobs. Anything that does
not (charts, activity feeds, batch statistics) belongs on another screen.

---

## 2. Screen anatomy

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│ ‹  Year 11 Set 2 · Paper 3            Amara O.        12 / 20   [Needs review]  ⋯  │  header 56
├───────┬─────────────────────────────────────────────┬──────────────────────────────┤
│ Q1 ✓  │                                             │  Q4 (b)               2 / 3   │
│ Q2 ⚑  │                                             │  ⚑ scheme check · partial     │
│ Q3 ✓  │            rendered page image              │  ── how the mark was built    │
│ Q4 ⚑  │            (warm paper on dark)             │  ✓ M1  correct substitution   │
│ Q5 ·  │                                             │  ✗ A1  final value            │
│ Q6 ✓  │                                             │  ── why marks were lost       │
│  …    │                                             │  ── what the student wrote    │
│       │                                             │  ── scheme check (if any)     │
│       │                                             │  ── your mark  [0][1][2][3]   │
│       │                                             │  [ Accept ⏎ ]  [ Set 3 ⏎ ]    │
├───────┴─────────────────────────────────────────────┴──────────────────────────────┤
│ 1 2 3 4 5 6 7   ·  fit width  ·  zoom 100%     14 accepted · 2 changed   [Approve A]│  footer 72
└────────────────────────────────────────────────────────────────────────────────────┘
   rail 236 (56)          paper 2.2*                       marks 400 (340–520)
```

### 2.1 Columns

| Region | Width | Notes |
|---|---|---|
| Question rail | 236 DIP, collapsible to 56 | `ListBox`, virtualising, grouped by topic |
| Splitter | 8 DIP | `GridSplitter`, `ShowsPreview=False` |
| Paper pane | `2.2*`, min 380 | warm surface, page image centred |
| Splitter | 8 DIP | |
| Mark panel | 400 DIP (min 340, max 520) | `ScrollViewer`; header stays pinned |
| Header | 56 DIP | paper identity + result provenance |
| Footer | 72 DIP | page chips + counts + primary action |

Grid: `Header(Auto) / Body(*) / Footer(Auto)`. Body: `236 | 8 | * | 8 | 400`.

### 2.2 Breakpoints

| Width | Layout |
|---|---|
| ≥ 1440 | full three-pane, rail expanded |
| 1100–1439 | rail collapses to 56; mark panel 360 |
| 900–1099 | two panes (paper + marks); rail becomes an overlay drawer opened by `F6` / a rail button |
| < 900 | stacked: segmented control `Paper · Marks · Questions`; window `MinWidth` stays 900, so this is the practical floor |

All sizes are DIPs. The paper image is the only element whose size depends on DPI
scale; text, padding and rails must not scale with the monitor.

### 2.3 Empty and degraded states

| State | Paper pane | Mark panel |
|---|---|---|
| Loading detail | skeleton block, no shimmer | skeleton lines |
| Loading page | 1px skeleton rectangle at page aspect | interactive (marks are independent) |
| Page available | image + page chips | normal |
| Original not stored | title/body from `ResultPagesDto.Message`, action per `Reason` (§9.2) | normal |
| Page failed | inline "Page 3 could not be shown" + Retry | normal |
| Detail failed | full-width error with backend message + Retry | hidden |
| No per-question data | "This correction stored totals only. The PDF report has the feedback." | totals only, approve allowed |
| Backend unreachable | keeps last content, header shows an amber lamp and "reconnecting" chip | actions disabled with reason |

The Desk **never** blanks the marks because the paper image failed: they are independent
requests.

---

## 3. Interaction model

### 3.1 Pointer

- Click a rail row → activate that question (no HTTP).
- Click a mark button → **stage** the value; the button shows a ring, not a save.
- Click `Accept` → accept and advance (no write).
- Click `Set n` → commit the staged mark, then advance.
- Drag splitters → resize; widths persist in `DesktopSettings.DeskPaneWidths`.
- `Ctrl` + wheel over the paper → zoom (25-point steps); double-click → toggle fit / 100%.
- Wheel scrolls the paper; the mark panel scrolls independently.

### 3.2 Keyboard

Handled by `DeskKeyboard` (a pure mapping from `(Key, Modifiers, FocusKind, HasDraft)` →
`DeskIntent`), attached once at the view root via `PreviewKeyDown`.

| Key | Action | Guard |
|---|---|---|
| `N` / `P` | next / previous question (paper order) | not in a text field |
| `Shift+N` / `Shift+P` | next / previous paper | same |
| `Enter` | draft staged → commit + advance; otherwise accept + advance | |
| `Space` | accept + advance | only when the paper pane has focus (never fires a focused button) |
| `0`–`9` | stage that value (clamped to max) | not in a text field; not with Ctrl/Alt |
| `Backspace` / `Esc` | clear draft; `Esc` with no draft closes overlays only | |
| `A` | approve paper (opens the confirm sheet) | ignored on key repeat |
| `R` | re-mark with AI (confirm dialog) | ignored on key repeat |
| `E` | export PDF | |
| `Ctrl` `+` / `-` / `0` | zoom in / out / fit width | |
| `PgUp` / `PgDn` | previous / next page | paper pane focused |
| `F6` / `Shift+F6` | cycle panes (paper → rail → panel) | |
| `Ctrl+Z` | undo the last mark change in this session | |
| `?` | shortcut sheet | |
| `Ctrl+K`, `Ctrl+J` | reserved for the shell (palette, activity) | shell owns them |

Rules:

- Single-letter and digit keys are ignored when `Keyboard.FocusedElement` is
  `TextBoxBase`, `PasswordBox` or `ComboBox`, and whenever Ctrl or Alt is held except
  for the explicit chords above.
- Destructive intents (`A`, `R`) ignore `e.IsRepeat`.
- No key silently saves: digits stage, `Enter` commits. This is the only way to make
  repeated marking fast without accidental writes.
- Every keyboard action has a pointer equivalent and vice versa.

### 3.3 Focus and announcements

- On open: focus the paper pane's scroll host (`Focusable=True`, `IsTabStop=True`), so
  `N`/`P` work immediately without stealing focus from a search box elsewhere.
- Panel controls are Tab-reachable; the splitter is not in the tab order.
- After a save, `AutomationProperties.LiveSetting="Polite"` on the footer status and
  `AutomationPeer.RaiseNotificationEvent` ("Question 4b saved as 3 of 3").
- When the active question changes, the rail item's `AutomationProperties.Name`
  includes number, mark and state ("Question 4b, 2 of 3, scheme check").

### 3.4 Decision semantics — the rule that matters

**DECISION.** *Accept is local; change writes; approve is the durable decision.*

| Action | HTTP | Why |
|---|---|---|
| Accept (unchanged) | **none** | `POST /results/{id}/questions/{n}/mark` sets `teacher_mark`, `manual_override=1`, `confidence='HIGH'`, clears `review_required` and appends a version — even when the value is identical (`setup_routes.py:507-580`). Accepting 18 questions would fabricate 18 overrides and remove the paper from the queue before it was reviewed. |
| Change mark | one `POST …/mark` with an **integer** | durable immediately; this is the only per-question write |
| Approve paper | one `POST /results/{id}/override` with `{note}` and no `earned` | records `result_reviews(action="approved")`, clears the review flags, keeps every AI mark |
| Approve with a whole-paper mark | `POST /override` with `{earned:int, note}` | required when the correction is partial (backend returns 409 `partial_correction` otherwise) |
| Undo a change | re-POST the previous integer | backend has no undo endpoint; a new version is the honest record |
| Re-mark | `POST /results/{id}/remark` | existing behaviour, confirmation required |

Consequences the UI must show:

- The footer counts three different things: **accepted** (local), **changed**
  (persisted), **to check** (not yet accepted).
- Leaving the Desk with changed-but-unapproved marks prompts once: "2 marks saved, paper
  not approved. Leave anyway?" — never per action.
- The paper stays in the review queue until approved. This is correct: a partially
  reviewed paper is still unreviewed.
- The ledger line under a changed question reads `AI 2 → you 3 · 14:32`. The AI value
  comes from the POST response (`previous`) or the session ledger; after a restart only
  "Your mark 3" is shown, because the original is only recoverable from version
  history — do not invent it.

---

## 4. Provenance model

Two levels: per question, and per paper. Both are derived only from recorded fields.

### 4.1 Per question (first match wins)

| # | Condition | State | Label | Glyph | Tone |
|---|---|---|---|---|---|
| 1 | `teacher_mark` is not null | `Yours` | Your mark | filled check | clay |
| 2 | `status == "REVIEW"` | `Unmarked` | Not marked | dashed circle | amber |
| 3 | `scheme_check` is not null | `SchemeCheck` | Scheme check | warning triangle | amber |
| 4 | `unmarked_subquestions` non-empty, or any `subquestions[].status != "MARKED"` | `Partial` | Part unmarked | half circle | amber |
| 5 | `verification.verified == true` | `Verified` | Verified | double check | mint |
| 6 | otherwise | `Ai` | AI | spark | neutral |

Notes:

- The engine sets `status="REVIEW"` **and** `review_reason` for questions it could not
  trust (`verify.py:180-200`); treat a missing status as legacy-valid, not as review.
- `scheme_check` is present only when the engine had a defensible reason to disagree
  with the supplied scheme (`_clean_scheme_check`, `legacy_marking.py:229`). Absence is
  the normal case and must not look like approval.
- `verification.verified` is result-level. Showing it on every question would overclaim;
  the spec therefore uses it only as the neutral fallback state, never as a per-question
  claim of verification.

### 4.2 Per paper (header chip; first match wins)

| Condition | Label | Tone |
|---|---|---|
| latest `result_reviews.action == "approved"` | Approved by you | clay outline |
| latest `result_reviews.action == "overridden"` or `manual_override == 1` with a whole-paper mark | Overridden | clay filled |
| `verification.partial` or `coverage.requires_review > 0` | Incomplete · 38/60 marked | amber |
| `review_required == 1` or `confidence in LOW/VALIDATION_FAILED/LARGE_SCORE_CHANGE` | Needs review | amber |
| `verification.verified` | Verified | mint |
| otherwise | AI marks | neutral |

The header never says "teacher override" for an approval. That mislabel is the current
bug (`ResultsViewModel.DetailHeader`), and the existing Accept path is why it happened:
`ReviewViewModel.AcceptCommand` calls `OverrideResultAsync(…, null, …)`, so "accepted"
is stored as `manual_override = 1`.

### 4.3 Visual language

- Chips are `icon + word + tone`; colour is never the only signal.
- Unchanged AI marks use a neutral outline chip, **not** a success colour. Clay
  (the `AccentBrush` token) is reserved for "you did this".
- The mark glyph: numerator 44px semibold, denominator 20px muted. A 4px provenance dot
  sits under the numerator.
- A question the engine distrusted gets a 3px amber left edge in the rail, not a
  coloured row background.

---

## 5. Verified backend contract

All facts below were read from the repository; nothing here is assumed.

### 5.1 Question JSON (`result_json.questions[]`)

Produced by `legacy_marking.normalise_result` (`backend/app/engine/legacy_marking.py:257-320`):

```json
{
  "question_number": "4(b)",
  "max_marks": 3,
  "awarded_marks": 2,
  "topic": "Algebra",
  "student_answer": "x = 4.5",
  "working_summary": "…",
  "loss_reason": "…",
  "mark_breakdown": [{"code": "M1", "awarded": true, "reason": "…"}],
  "correct_method": "…",
  "full_mark_solution": "…",
  "scheme_check": {
    "disagrees_with_scheme": true,
    "student_answer_valid": true,
    "scheme_answer": "…",
    "independently_verified_answer": "…",
    "discrepancy_type": "…",
    "evidence": "…",
    "confidence": 0.82,
    "justified_marks": 3
  },
  "status": "REVIEW",
  "review_reason": "…",
  "teacher_mark": 3,
  "subquestions": [
    {"id": "4b-i", "label": "(i)", "max_marks": 1, "awarded_marks": 1,
     "status": "MARKED", "answers": ["…"], "mark_codes": ["M1"]}
  ],
  "unmarked_subquestions": ["(ii)"]
}
```

Key names are snake_case; the client's `Json.Options` already uses
`JsonNamingPolicy.SnakeCaseLower`.

**Existing defect to fix while adding the DTO:** `QuestionMarkDto` maps
`Awarded`/`Max`/`Number`, but the engine emits `awarded_marks` / `max_marks` /
`question_number`. The current question grid therefore renders `— / —`. The test
`Result_detail_returns_per_question_rows_when_present` hides this because its fixture
uses the fictional keys `number` / `awarded` / `max`.

### 5.2 Result detail fields used by the Desk

From `GET /api/results/{id}` (`routes_all.py:374`, decorated by `_identity`):

- `id`, `student_name`, `display_name`, `class_label`, `assignment_title`
- `earned`, `available`, `percentage`, `provisional_grade`, `marking_model`
- `result_json` (`paper_info`, `questions`, `overall_notes`, `totals`)
- `verification_json` (`verified`, `status`, `anomalies`, `warnings`,
  `review_questions`, `partial`, `coverage`, `unresolved_marks`, `paper_total_marks`)
- `confidence`, `confidence_reasons`, `review_required`, `manual_override`
- `processing_seconds`, `created_at`, `key_fingerprint`

### 5.3 Pages

- `GET /api/results/{id}/pages` → availability and metadata.
  `{available, reason, message, retryable, job_status, retention:{keep_submissions,
  keep_days, keeps}, page_count, file_kind, file_name, source, dpi, page_url_template}`
- `GET /api/results/{id}/pages/{page}.png?dpi=150` → PNG, `ETag`, `Cache-Control:
  private, max-age=86400`; `dpi` clamped 72–300, default 150.
- `reason` ∈ `retention_disabled | file_missing | file_unreadable | processing | no_link`.
- Router prefix is `/api/results`; the client base already appends `/api/`.

### 5.4 Review history

`GET /api/results/{id}/reviews` → `{result_id, history:[{action, previous_earned,
new_earned, previous_confidence, note, created_at}]}`; `action` is `approved` or
`overridden` (`setup_routes.py:390-500`).

### 5.5 Endpoints used

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `results/{id}` | — | detail |
| GET | `results/{id}/pages` | — | availability |
| GET | `results/{id}/pages/{page}.png?dpi=` | — | bytes |
| GET | `results/{id}/versions` | — | history table |
| GET | `results/{id}/reviews` | — | paper-level ledger |
| POST | `results/{id}/questions/{q}/mark` | `{awarded:int, note?}` | **integers only**; 400 `bad_question_mark` |
| POST | `results/{id}/override` | `{earned?:int, note?}` | 409 `partial_correction` when incomplete and `earned` missing |
| POST | `results/{id}/remark` | `{}` | existing re-mark flow |
| GET | `results/{id}/export?format=pdf` | — | existing download |

---

## 6. Exact DTOs

New file `desktop/src/MathChecker.Desktop/Models/ResultWorkspaceDtos.cs`.
`QuestionMarkDto` **moves** here from `ApiModels.cs` (delete the old definition).

```csharp
namespace MathChecker.Desktop.Models;

public sealed class MarkBreakdownItemDto
{
    public string? Code { get; set; }
    public bool Awarded { get; set; }
    public string? Reason { get; set; }
}

public sealed class SubquestionDto
{
    public string? Id { get; set; }
    public string? Label { get; set; }
    public string? ParentId { get; set; }
    public double? MaxMarks { get; set; }
    public double? AwardedMarks { get; set; }
    /// <summary>"MARKED" or "UNMARKED".</summary>
    public string? Status { get; set; }
    public List<string> Answers { get; set; } = new();
    public List<string> MarkCodes { get; set; } = new();

    public bool IsMarked => string.Equals(Status, "MARKED", StringComparison.OrdinalIgnoreCase);
}

public sealed class SchemeCheckDto
{
    public bool DisagreesWithScheme { get; set; }
    public bool StudentAnswerValid { get; set; }
    public string? SchemeAnswer { get; set; }
    public string? IndependentlyVerifiedAnswer { get; set; }
    public string? DiscrepancyType { get; set; }
    public string? Evidence { get; set; }
    public double? Confidence { get; set; }
    public double? JustifiedMarks { get; set; }
}

public sealed class QuestionMarkDto
{
    // Real engine keys.
    public string? QuestionNumber { get; set; }
    public double? MaxMarks { get; set; }
    public double? AwardedMarks { get; set; }

    // Legacy aliases kept from the previous DTO so older stored rows still show
    // their number and marks (the old fixture used these keys).
    public string? Number { get; set; }
    public string? Question { get; set; }
    public double? Awarded { get; set; }
    public double? Earned { get; set; }
    public double? Available { get; set; }
    public double? Max { get; set; }
    public string? Topic { get; set; }
    public string? StudentAnswer { get; set; }
    public string? WorkingSummary { get; set; }
    public string? LossReason { get; set; }
    public string? CorrectMethod { get; set; }
    public string? FullMarkSolution { get; set; }
    public string? Status { get; set; }          // VALID | REVIEW
    public string? ReviewReason { get; set; }
    public double? TeacherMark { get; set; }
    public List<MarkBreakdownItemDto> MarkBreakdown { get; set; } = new();
    public List<SubquestionDto> Subquestions { get; set; } = new();
    public List<string> UnmarkedSubquestions { get; set; } = new();
    public SchemeCheckDto? SchemeCheck { get; set; }

    // Compatibility with the existing grid.
    public string NumberText => QuestionNumber ?? Number ?? Question ?? "—";
    public int? AwardedValue => (int?)(AwardedMarks ?? Awarded ?? Earned);
    public int? MaxValue => (int?)(MaxMarks ?? Max ?? Available);
    public string MarkText => $"{AwardedValue?.ToString() ?? "—"} / {MaxValue?.ToString() ?? "—"}";
    public bool HasSchemeCheck => SchemeCheck is not null;
}

public sealed class RetentionStateDto
{
    public string? KeepSubmissions { get; set; }
    public int KeepDays { get; set; }
    public bool Keeps { get; set; }
}

public sealed class ResultPagesDto
{
    public string? ResultId { get; set; }
    public bool Available { get; set; }
    public string? Reason { get; set; }
    public string? Message { get; set; }
    public bool Retryable { get; set; }
    public string? JobStatus { get; set; }
    public RetentionStateDto? Retention { get; set; }
    public double? CheckedAt { get; set; }
    public int PageCount { get; set; }
    public string? FileKind { get; set; }
    public string? FileName { get; set; }
    public string? Source { get; set; }
    public int Dpi { get; set; }
    public string? PageUrlTemplate { get; set; }
}

public sealed class CoverageDto
{
    public int Expected { get; set; }
    public int Marked { get; set; }
    public int RequiresReview { get; set; }
    public int Unanswered { get; set; }
    public bool Verified { get; set; }
}

public sealed class VerificationDto
{
    public bool Verified { get; set; }
    public string? Status { get; set; }
    public List<string> Anomalies { get; set; } = new();
    public List<string> Warnings { get; set; } = new();
    public List<string> ReviewQuestions { get; set; } = new();
    public bool Partial { get; set; }
    public double? MarkedPercentage { get; set; }
    public double? PaperTotalMarks { get; set; }
    public double? UnresolvedMarks { get; set; }
    public CoverageDto? Coverage { get; set; }
}

public sealed class ResultReviewEntryDto
{
    public string? Action { get; set; }          // approved | overridden
    public double? PreviousEarned { get; set; }
    public double? NewEarned { get; set; }
    public string? PreviousConfidence { get; set; }
    public string? Note { get; set; }
    public double? CreatedAt { get; set; }
}

public sealed class ResultReviewHistoryDto
{
    public string? ResultId { get; set; }
    public List<ResultReviewEntryDto> History { get; set; } = new();
}
```

Add to `ResultDetailDto` (in `ApiModels.cs`):

```csharp
/// <summary>Typed view of verification_json; null when the row has none.</summary>
public VerificationDto? Verification()
{
    if (VerificationJson is not { ValueKind: JsonValueKind.Object } value)
    {
        return null;
    }

    return JsonSerializer.Deserialize<VerificationDto>(value.GetRawText(), Json.Options);
}
```

`Questions()` keeps its existing tolerant lookup (`questions` / `question_marks` /
`marks`) but deserialises the new DTO.

---

## 7. Exact client methods

Add `partial` to the class and put workspace calls in
`Core/Api/MathCheckerApiClient.Workspace.cs`. Existing methods to **change** (not add):

```csharp
// int, not double: the backend casts int(payload["awarded"]) and rejects 2.5.
public Task<JsonElement> SaveQuestionMarkAsync(string resultId, string questionNumber,
    int awarded, string? note = null, CancellationToken cancellationToken = default) =>
    SendJsonRawAsync(HttpMethod.Post,
        $"results/{Uri.EscapeDataString(resultId)}/questions/{Uri.EscapeDataString(questionNumber)}/mark",
        note is null ? new { awarded } : new { awarded, note }, cancellationToken);

// int?, not double?: the backend casts int(payload["earned"]).
public Task<JsonElement> OverrideResultAsync(string resultId, int? earned, string? note,
    CancellationToken cancellationToken = default) =>
    SendJsonRawAsync(HttpMethod.Post, $"results/{Uri.EscapeDataString(resultId)}/override",
        earned is null ? new { note } : new { earned, note }, cancellationToken);

// Typed; replaces the JsonElement version (no current callers).
public Task<ResultReviewHistoryDto> ResultReviewHistoryAsync(string resultId,
    CancellationToken cancellationToken = default) =>
    GetAsync<ResultReviewHistoryDto>($"results/{Uri.EscapeDataString(resultId)}/reviews",
        cancellationToken);
```

New methods:

```csharp
public Task<ResultPagesDto> ResultPagesAsync(string resultId,
    CancellationToken cancellationToken = default) =>
    GetAsync<ResultPagesDto>($"results/{Uri.EscapeDataString(resultId)}/pages",
        cancellationToken);

public Task<byte[]> ResultPagePngAsync(string resultId, int page, int dpi = 150,
    CancellationToken cancellationToken = default) =>
    GetBytesAsync(
        $"results/{Uri.EscapeDataString(resultId)}/pages/{page}.png" + Query(("dpi", dpi)),
        "image/png", cancellationToken);

public Task<JsonElement> ApproveResultAsync(string resultId, int? earned, string note,
    CancellationToken cancellationToken = default) =>
    OverrideResultAsync(resultId, earned, note, cancellationToken);
```

Companion helper (private, in `MathCheckerApiClient.cs`):

```csharp
private async Task<byte[]> GetBytesAsync(string path, string expectedMediaType,
    CancellationToken cancellationToken)
{
    using var request = Request(HttpMethod.Get, path);
    using var response = await _http.SendAsync(request,
        HttpCompletionOption.ResponseHeadersRead, cancellationToken).ConfigureAwait(false);
    if (!response.IsSuccessStatusCode)
    {
        var body = await response.Content.ReadAsStringAsync(cancellationToken)
            .ConfigureAwait(false);
        throw ToException(response.StatusCode, body, path);
    }

    var media = response.Content.Headers.ContentType?.MediaType;
    var expectedPrefix = expectedMediaType.Split('/')[0] + "/";
    if (!string.IsNullOrEmpty(media)
        && !media.StartsWith(expectedPrefix, StringComparison.OrdinalIgnoreCase))
    {
        throw new ApiException((int)response.StatusCode, "unexpected_content_type",
            $"Expected {expectedMediaType}, received {media}.", path);
    }

    return await response.Content.ReadAsByteArrayAsync(cancellationToken)
        .ConfigureAwait(false);
}
```

Callers to update:

- `ResultsViewModel.SetQuestionMarkCommand` — `int.TryParse`, clamp to `MaxValue`.
- `ResultsViewModel.OverrideCommand` — `int.TryParse`; keep `AskText` until the Desk exists.
- `ReviewViewModel.OverrideCommand` — `int.TryParse`.
- `ReviewViewModel.AcceptCommand` — keep `OverrideResultAsync(null, …)` for now, but the
  label/ledger must say "Approved", not "Override" (see §4.2).

---

## 8. View-model contract

`ViewModels/MarkingDeskViewModel.cs` (inherits `PageViewModel`, key `"desk"`), plus
`QuestionVm`.

```csharp
public sealed class MarkingDeskViewModel : PageViewModel
{
    public MarkingDeskViewModel(AppSession session) : base(session, "desk", "Marking desk") { }

    // identity
    public string? ResultId { get; private set; }
    public ResultDetailDto? Detail { get; private set; }
    public VerificationDto? Verification { get; private set; }
    public ResultPagesDto? Pages { get; private set; }

    // collections
    public ObservableCollection<QuestionVm> Questions { get; } = new();
    public ObservableCollection<PageChipVm> PageChips { get; } = new();
    public ObservableCollection<LedgerEntryVm> Ledger { get; } = new();
    public ObservableCollection<VersionEntryVm> Versions { get; } = new();

    // state
    public QuestionVm? ActiveQuestion { get; set; }      // selecting does not write
    public PaperPaneState PaperState { get; private set; } // Checking/Available/Missing/Failed
    public string? PaperTitle { get; private set; }
    public string? PaperBody { get; private set; }
    public string? PaperActionLabel { get; private set; }
    public ResultProvenance ResultState { get; private set; }
    public bool HasQuestionData { get; private set; }
    public int DecidedCount { get; private set; }
    public int ChangedCount { get; private set; }
    public int AttentionCount { get; private set; }
    public int TotalCount { get; private set; }
    public string ProgressText { get; private set; }      // "14 accepted · 2 changed · 2 to check"
    public bool CanApprove { get; private set; }
    public bool ApproveRequiresMark { get; private set; }
    public int? WholePaperMark { get; set; }
    public bool IsApproving { get; private set; }
    public string? QuestionError { get; private set; }    // inline, per question

    // lifecycle
    public Task OpenAsync(string resultId, string? questionNumber = null, CancellationToken ct = default);
    public Task LoadPageAsync(int page);
    public void ZoomIn(); public void ZoomOut(); public void FitWidth();
    public Task AcceptAndAdvanceAsync();
    public Task StageMarkAsync(int value);
    public Task CommitDraftAsync();
    public Task ApproveAsync();
    public Task UndoLastChangeAsync();
    public Task RemarkAsync();
    public Task ExportAsync(string format);
}
```

`QuestionVm`:

```csharp
public sealed class QuestionVm : ObservableObject
{
    public QuestionMarkDto Source { get; }
    public string Number => Source.QuestionNumber ?? "—";
    public int? Max => (int?)Source.MaxMarks;
    public int Awarded { get; private set; }          // recorded value
    public int? Draft { get; private set; }           // staged, null = nothing staged
    public bool IsAccepted { get; private set; }      // session-local
    public bool IsSaving { get; private set; }
    public QuestionState State { get; }
    public IReadOnlyList<string> AttentionReasons { get; }
    public string? LedgerLine { get; private set; }   // "AI 2 → you 3 · 14:32"
    public string AutomationName { get; }
    public bool IsActive { get; set; }
}
```

Load sequence in `OpenAsync`:

1. Cancel the previous token; clear collections; `PaperState = Checking`.
2. `Task.WhenAll(ResultAsync(id), ResultPagesAsync(id))`. Detail failure → page error;
   pages failure → `PaperState.Failed` only.
3. Build `QuestionVm`s, sort by `QuestionNumber` (the backend already sorts, but legacy
   rows may not).
4. `ActiveQuestion` = the first question whose state is not `Ai`/`Verified`, else the
   first question. If `questionNumber` was supplied, select that.
5. Set `PaperState` from `ResultPagesDto`; load page 1; prefetch page 2 after idle.
6. Lazily load versions and review history in the background (`Task.Run` not needed —
   async I/O only): they only affect the ledger/attempts section.

Saving, per question:

1. Validate integer in `0..Max`; if no max, allow any non-negative int and show a hint.
2. Set `Draft`, then commit: optimistic `Awarded = value`, `IsSaving = true`.
3. `POST …/mark` with `note = "Set in the marking desk"`.
4. On success: update totals from the response (`earned`, `available`, `percentage`),
   append ledger entry, set `Source.TeacherMark`, `State = Yours`, `IsAccepted = true`.
5. On failure: revert `Awarded`, keep `Draft`, show `QuestionError` inline (no global
   banner), leave the rail usable.

Never use `PageViewModel.RunAsync` for a mark save: it toggles the page-wide busy flag
and would freeze the rail and paper for every keystroke.

---

## 9. Paper pane technical design

### 9.1 Image pipeline

- Request `dpi=150` by default; `220` when `zoom > 1.5`; `300` when `zoom > 2.25`
  (backend clamps to 300).
- `ResultPagePngAsync` → `byte[]` → `BitmapImage`:
  `BeginInit(); StreamSource = new MemoryStream(bytes); DecodePixelWidth = …;
  CacheOption = BitmapCacheOption.OnLoad; EndInit(); Freeze();`
  Decode on a worker thread (`Task.Run`), assign on the dispatcher.
- `DecodePixelWidth = clamp(round(viewportWidth * dpiScale * 1.15), 480, 3000)`.
- `RenderOptions.BitmapScalingMode="HighQuality"`; no `Effect` on the image.
- Re-decode only when `neededWidth > decodedWidth * 1.25`; never upscale a cached bitmap.

### 9.2 `PageCache`

`Core/Workspace/PageCache.cs`

- Key: `(resultId, page, dpi)`; value: `byte[]` + decoded `BitmapSource`.
- Limits: 120 MB or 60 entries, LRU eviction; on result switch keep only the current and
  ±1 page.
- In-flight de-duplication: a second request for the same key awaits the first
  (`Dictionary<key, Task<byte[]>>`).
- Cancellation: the caller's token cancels an in-flight download; the cache entry is not
  stored on cancel.
- Prefetch page ±1 after the current page renders, at the same dpi.

### 9.3 Availability states and actions

| `reason` | Pane title | Action |
|---|---|---|
| `retention_disabled` | "The student's work is not stored here" | "Retention settings" → Settings/Privacy |
| `file_missing` | "This paper's original file is missing" | "Re-check storage" |
| `file_unreadable` | "The stored copy cannot be read" | "Re-correct paper" (→ Correct) |
| `processing` | "The paper is still being processed" | "Check again" + poll every 5 s |
| `no_link` | "This result has no stored submission" | none |

The body is `ResultPagesDto.Message` verbatim. Never paraphrase it into "retention
deleted your paper" unless `reason == retention_disabled`.

### 9.4 Page controls

- Chips `1..n` under the paper; active chip clay outline; `PgUp`/`PgDn`; `Ctrl`+wheel
  zoom; zoom label 50–300%.
- Fit-width is the default and the reset state (`Ctrl+0`).
- No thumbnail rail (decode cost, no workflow benefit at this size).

### 9.5 Totals and counts

- Header score is `earned / available` from the detail; after a question save, use the
  response's recomputed values — the backend recalculates totals from stored questions.
- Footer: accepted = `IsAccepted` count; changed = ledger `Change` entries in this
  session; to check = questions whose state is `Unmarked`/`SchemeCheck`/`Partial` and
  not accepted.
- Do not display a percentage as the primary number when `verification.partial`; show
  the coverage fraction instead.

---

## 10. Partial corrections

- Header chip: `Incomplete · {coverage.marked}/{coverage.expected} marked`.
- `CanApprove` stays true, but the approve sheet requires a whole-paper mark.
- Sheet copy: "This correction is incomplete (38 of 60 questions marked). Enter the
  student's mark out of 60 to approve it."
- On `409 partial_correction`, set `ApproveRequiresMark = true` and focus the numeric
  field; do not show an error banner.
- On success, the backend clears `verification.partial` and records the teacher's mark;
  reload the detail.

---

## 11. Motion spec

Port the existing rules from `frontend/src/motion.ts`: transform and opacity only,
short, never delaying an action, and no animated path at all under reduced motion.

| Event | Duration | Property | Easing |
|---|---|---|---|
| Rail selection indicator | 160 ms | `TranslateTransform.Y` | `KeySpline 0.2,0,0,1` |
| Active question panel content | 120 ms | opacity 0.4 → 1 | linear |
| Mark committed | 200 ms | glyph `ScaleTransform` 1 → 1.04 → 1; ring opacity 0 → 1 → 0 | decelerate |
| Draft staged | 90 ms | ring opacity | linear |
| Paper switch | 140 ms | page host opacity; `TranslateTransform.Y` 4 → 0 | decelerate |
| Approve accepted | 260 ms | header chip opacity + 6px rise | decelerate |
| Drawer (later, shell) | 200 ms | `TranslateTransform.X` | decelerate |
| Toast | 160 ms in / 120 ms out | `TranslateTransform.Y` 8 + opacity | decelerate |
| Skeleton | none | static block | — |

Never animate: page image bounds, `Grid` column widths, `Margin`/`Padding`, `DropShadow`
blur, `DataGrid` rows, or anything that triggers layout.

Reduced motion:

- `SystemParameters.ClientAreaAnimation` (subscribe to `StaticPropertyChanged`) **or**
  `DesktopSettings.ReduceMotion = true` → all durations 0, all transforms identity.
- State changes still happen instantly; no "fade" substitute.

---

## 12. Accessibility checklist

- Every custom control has `AutomationProperties.Name`; verified by the existing UIA
  probe.
- Mark buttons: "Question 4b, 2 of 3 marks, current 1"; `ToggleButton`-like state
  exposed via `AutomationProperties.ItemStatus` ("staged").
- Paper image: "Page 2 of 7 of the student's work".
- Zoom buttons: "Zoom in", "Zoom out", "Fit width".
- Focus visuals: 2px clay ring, 2px offset, visible on dark and on paper.
- Focus order follows the visual order; `F6` cycles panes.
- Contrast: body text ≥ 4.5:1 on `#120C09`; amber text uses the lighter tint
  (`#FFB27A`) for small type.
- Status never colour-only; every chip has a glyph and a word.
- Keyboard-only path for: open paper, navigate questions, accept, change, approve,
  undo, export, zoom, page.
- `AutomationProperties.LiveSetting="Polite"` on footer status; `RaiseNotificationEvent`
  on save/approve.

---

## 13. Performance budgets

| Metric | Target |
|---|---|
| Detail + pages metadata | < 250 ms p95 on localhost |
| First page first paint | < 400 ms cold, < 150 ms warm (150 dpi A4) |
| Page switch | < 120 ms warm, no frame > 50 ms |
| Mark save round-trip | < 250 ms p95; UI optimistic immediately |
| Memory (page cache) | ≤ 120 MB; released to current ±1 on result switch |
| Rail scrolling (60 questions) | no dropped frames; virtualised |
| Desk open (detail + page 1) | < 700 ms |

Measurement: `Stopwatch` around load paths, written through the existing `IFileLog`
(redacted), plus `CompositionTarget.Rendering` frame-gap sampling in debug builds only.
No telemetry leaves the machine.

---

## 14. Tests

### 14.1 Fix the misleading fixture first

`ApiClientTests.Result_detail_returns_per_question_rows_when_present` uses
`number/awarded/max`. Replace it with the engine's real keys and add a legacy-shape
case. This is the regression test for the empty marks column.

### 14.2 New tests

`ResultWorkspaceDtoTests.cs`
- Full engine-shaped question JSON maps: `AwardedMarks`, `MaxMarks`, `MarkBreakdown`,
  `SchemeCheck`, `Subquestions`, `UnmarkedSubquestions`, `Status`, `TeacherMark`.
- Legacy `number/awarded/max` still maps (tolerance kept).
- `Verification()` parses `verified`, `review_questions`, `partial`, `coverage`.
- `ResultPagesDto` maps `available=false` + each reason and `retention.keeps`.

`PageClientTests.cs` (StubHttpHandler)
- `/pages` URL has no query; `ResultPagesAsync` returns metadata.
- `/pages/2.png?dpi=150` URL exact; bytes returned.
- 404 `{code:"original_not_stored"}` → `ApiException.Code` preserved.
- `Content-Type: application/json` on a 200 → `unexpected_content_type`.
- `SaveQuestionMarkAsync` body contains `"awarded":3` and no decimal point.
- `OverrideResultAsync(null, note)` body has `note` and no `earned`.

`ProvenanceTests.cs` — table-driven:
- teacher_mark → `Yours`
- status REVIEW → `Unmarked`
- scheme_check → `SchemeCheck`
- unmarked subquestion → `Partial`
- verified fallback → `Verified`
- plain → `Ai`
- paper: approved / overridden / partial / needs review / verified / AI.

`DeskCommandTests.cs` (reuse the `StudentsPageTests` session harness)
- `AcceptAndAdvanceAsync` issues **zero** POSTs and advances.
- `CommitDraftAsync` issues one POST with an integer; totals update from the response.
- A failed POST reverts the mark and keeps the draft.
- `ApproveAsync` issues one `override` POST with the note and no `earned`.
- 409 `partial_correction` sets `ApproveRequiresMark`; retry with a mark succeeds.
- `UndoLastChangeAsync` re-posts the previous value.
- Saving question A leaves question B's `IsSaving` false (no page-wide busy flag).

`DeskKeyboardTests.cs` — pure mapping: text-field guard, modifier guard, repeat guard,
draft/no-draft Enter, digit clamping.

`PageCacheTests.cs` — LRU eviction at the byte cap, in-flight de-dup, cancel does not
cache, result switch trims to ±1.

### 14.3 UI automation

Extend `dev_local/ui_probe.ps1` with a Desk pass over demo data:
open the first result → assert either a page image or a named missing-state element →
`N`, `1`, `Enter` → assert the footer count and ledger line → `Esc` → close. No writes
outside the demo data folder.

---

## 15. File plan and integration

New:

```
Models/ResultWorkspaceDtos.cs
Core/Api/MathCheckerApiClient.Workspace.cs
Core/Workspace/PageCache.cs
Core/Workspace/QuestionTriage.cs      // ports frontend/src/topicGroups.ts
Core/Workspace/Provenance.cs          // pure derivation + copy
Core/Workspace/DeskKeyboard.cs        // pure key → intent
Controls/PageStrip.xaml(.cs)
Controls/MarkPad.xaml(.cs)
Controls/MarkGlyph.xaml(.cs)
Controls/ProvenanceChip.xaml(.cs)
ViewModels/MarkingDeskViewModel.cs
ViewModels/QuestionVm.cs
Views/MarkingDeskView.xaml(.cs)
Themes/Desk.xaml                      // visual tokens used only by the Desk for now
tests/...                             // per §14
```

Changed:

- `App.xaml`: merge `Themes/Desk.xaml`; add
  `<DataTemplate DataType="{x:Type vm:MarkingDeskViewModel}"><views:MarkingDeskView/></DataTemplate>`.
- `MainViewModel.BuildNavigation`: add `new MarkingDeskViewModel(_session)` under
  "Review" (temporary placement until the shell consolidates).
- `AppSession`: add `public void OpenResult(string resultId, string? questionNumber = null)`
  which stores the request and calls `GoTo("desk")`; replace the four context-free
  `GoTo("results")` jumps (`ReviewViewModel`, `JobsViewModel`, `DashboardViewModel`,
  `StudentsViewModel`).
- `ResultsViewModel`: keep the archive grid, replace its detail pane with
  "Open in desk" + export; remove the `AskText` mark editor; stop labelling every
  `manual_override == 1` result "teacher override" — use the latest review action
  (approved / overridden) from `ResultReviewHistoryAsync`, with "teacher decision" as the
  neutral fallback while that request is in flight.
- `Models/ApiModels.cs`: delete the old `QuestionMarkDto`; add `Verification()`.

Explicitly **not** changed: any Python file, `ResultSummaryDto`, `JobsViewModel`,
`BackendHost`, tests other than the fixture named above.

---

## 16. Delivery phases

| Phase | Work | Done when |
|---|---|---|
| D0 · contract | DTOs, client methods, `GetBytesAsync`, fixture fix, DTO/client tests | tests green; a real demo result deserialises with marks, breakdowns and flags |
| D1 · paper | `PageStrip`, `PageCache`, states, zoom, page chips | a stored submission shows page 1 in < 400 ms; a retention-deleted one shows the exact reason |
| D2 · decision | rail, mark panel, provenance, keyboard, accept/change/approve, undo, inline errors | an 18-question paper can be reviewed end-to-end without a mouse; approve removes it from the queue |
| D3 · polish | motion, reduced motion, 900/1100/1440 breakpoints, DPI 100–200 %, UIA names | no hitch > 50 ms; probe passes; shortcuts sheet |
| D4 · integration | `OpenResult`, archive split, navigation from queue/jobs/dashboard/students | "Open in Results" opens the right paper |

---

## 17. Risks and open decisions

1. **DECISION — accept-without-write.** It is the only way to avoid fabricating
   overrides, but it means an unapproved paper keeps its review flag. Needs product
   sign-off; the alternative (write on accept) is measurably worse with the current
   backend.
2. **Per-question AI-vs-teacher history is session-only.** After a restart the Desk can
   show "Your mark" but not the original AI value; only version history has it, and it
   is not per-question addressable. Do not imply otherwise.
3. **Engine marks are integers.** The mark pad is integer-only because
   `POST …/mark` casts `int`. Half marks are a backend limitation, not a UI choice.
4. **Legacy results without `result_json.questions`.** The Desk must show the
   totals-only state; the PDF report remains the only per-question view for them.
5. **`max_marks` can be 0** in warning cases. Disable the pad, keep the question
   visible, explain why.
6. **DPI changes at runtime** (`DpiChanged`) invalidate the decode width; flush decoded
   bitmaps (keep bytes) and re-decode.
7. **Paper image trust.** The viewer reads only inside the backend data dir and returns
   authoritative reasons; the Desk must never substitute a placeholder that looks like a
   paper.
8. **Queue ranking is not part of this spec.** The Desk can be opened from anywhere; the
   queue's ordering is defined by the review phase.
