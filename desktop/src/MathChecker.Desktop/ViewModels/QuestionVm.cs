using MathChecker.Desktop.Core.Mvvm;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>One staged value button: zero through the question's maximum.</summary>
public sealed record MarkChoiceVm(int Value, bool IsCurrent, bool IsStaged)
{
    public string Text => Value.ToString();
}

/// <summary>
/// One question on the rail and in the mark panel. It holds the recorded mark,
/// the session draft and the local accept flag; the desk owns the HTTP calls.
/// Values are integers because the backend casts <c>int(payload["awarded"])</c>.
/// </summary>
public sealed class QuestionVm : ObservableObject
{
    private readonly bool _resultVerified;
    private int _awarded;
    private int? _draft;
    private bool _isAccepted;
    private bool _isChanged;
    private bool _isSaving;
    private string _ledgerLine = string.Empty;
    private string? _error;
    private QuestionProvenance _provenance;

    public QuestionVm(QuestionMarkDto source, bool resultVerified)
    {
        Source = source ?? throw new ArgumentNullException(nameof(source));
        _resultVerified = resultVerified;
        Max = (int)(source.MaxMarks ?? source.Max ?? source.Available ?? 0);
        _awarded = Clamp((int)(source.AwardedMarks ?? source.Awarded ?? source.Earned ?? 0));
        _provenance = DeskProvenance.ForQuestion(source, resultVerified);
        if (source.TeacherMark is not null)
        {
            _ledgerLine = $"Your mark {_awarded}";
            _isAccepted = true;
            _isChanged = true;
        }
    }

    public QuestionMarkDto Source { get; }

    public string Number => Source.NumberText;

    public string Topic => string.IsNullOrWhiteSpace(Source.Topic) ? "Unfiled" : Source.Topic!;

    public int Max { get; }

    public int Awarded => _awarded;

    public int? Draft => _draft;

    public bool HasDraft => _draft is not null;

    public bool IsAccepted => _isAccepted;

    /// <summary>True when this session wrote a mark for the question.</summary>
    public bool IsChanged => _isChanged;

    public bool IsSaving => _isSaving;

    /// <summary>No max means the engine could not bound the question; the pad is disabled.</summary>
    public bool CanMark => Max > 0 && !IsSaving;

    public string MaxZeroText => Max > 0
        ? string.Empty
        : "No maximum mark was stored for this question, so it cannot be marked here.";

    public QuestionProvenance Provenance => _provenance;

    public QuestionState State => _provenance.State;

    public string StateLabel => _provenance.Label;

    public string StateGlyph => _provenance.Glyph;

    public string StateTone => _provenance.Tone;

    /// <summary>
    /// Amber edge only while the engine's warning is unresolved. A teacher mark,
    /// a session change or an accept settles it.
    /// </summary>
    public bool IsAttention => _provenance.IsAttention && !_isChanged && !_isAccepted;

    public string AttentionText => _provenance.AttentionText;

    /// <summary>A local accept is a decision, so the rail must show it as settled.</summary>
    public string RailGlyph => _isAccepted ? "✓" : _provenance.Glyph;

    public string MarkText => $"{_awarded} / {Max}";

    public string LedgerLine => _ledgerLine;

    public string? Error => _error;

    public string StudentAnswerText =>
        string.IsNullOrWhiteSpace(Source.StudentAnswer)
            ? "No transcribed answer was stored for this question."
            : Source.StudentAnswer!;

    public string LossReasonText =>
        string.IsNullOrWhiteSpace(Source.LossReason)
            ? "No loss reason was recorded."
            : Source.LossReason!;

    public bool HasLossReason => !string.IsNullOrWhiteSpace(Source.LossReason);

    public bool HasBreakdown => Source.MarkBreakdown.Count > 0;

    public bool HasSchemeCheck => Source.SchemeCheck is not null;

    public SchemeCheckDto? SchemeCheck => Source.SchemeCheck;

    public string AutomationName
    {
        get
        {
            var name = $"Question {Number}, {MarkText}, {StateLabel.ToLowerInvariant()}";
            if (IsAccepted)
            {
                name += ", accepted";
            }

            if (IsAttention)
            {
                name += ", needs check";
            }

            return name;
        }
    }

    public void Stage(int value)
    {
        if (!CanMark)
        {
            return;
        }

        var clamped = Clamp(value);
        if (_draft == clamped)
        {
            return;
        }

        _draft = clamped;
        OnPropertyChanged(nameof(Draft));
        OnPropertyChanged(nameof(HasDraft));
        OnPropertyChanged(nameof(RailGlyph));
    }

    public void ClearDraft()
    {
        if (_draft is null)
        {
            return;
        }

        _draft = null;
        OnPropertyChanged(nameof(Draft));
        OnPropertyChanged(nameof(HasDraft));
    }

    /// <summary>
    /// Accept is local and deliberately writes nothing: a POST would set
    /// <c>manual_override</c> and remove the paper from the queue before the
    /// teacher had reviewed it.
    /// </summary>
    public void Accept()
    {
        if (_isAccepted || _isSaving)
        {
            return;
        }

        _isAccepted = true;
        OnPropertyChanged(nameof(IsAccepted));
        OnPropertyChanged(nameof(IsAttention));
        OnPropertyChanged(nameof(RailGlyph));
        OnPropertyChanged(nameof(AutomationName));
    }

    public void BeginSave()
    {
        _isSaving = true;
        _error = null;
        OnPropertyChanged(nameof(IsSaving));
        OnPropertyChanged(nameof(CanMark));
        OnPropertyChanged(nameof(Error));
    }

    public void FailSave(string message)
    {
        _isSaving = false;
        _error = message;
        OnPropertyChanged(nameof(IsSaving));
        OnPropertyChanged(nameof(CanMark));
        OnPropertyChanged(nameof(Error));
    }

    /// <summary>Applies the backend's response after a question mark was written.</summary>
    public void ApplyChange(int awarded, int? aiPrevious)
    {
        _awarded = Clamp(awarded);
        Source.AwardedMarks = _awarded;
        Source.TeacherMark = _awarded;
        Source.Status = "VALID";
        Source.ReviewReason = null;
        _draft = null;
        _isAccepted = true;
        _isChanged = true;
        _isSaving = false;
        _error = null;
        _ledgerLine = aiPrevious is { } previous
            ? $"AI {previous} → you {_awarded} · {DateTime.Now:HH:mm}"
            : $"Your mark {_awarded} · {DateTime.Now:HH:mm}";
        _provenance = DeskProvenance.ForQuestion(Source, _resultVerified);
        NotifyAll();
    }

    /// <summary>
    /// Undo re-posts the previous integer. The backend has no undo endpoint, so
    /// this is a new teacher decision and the ledger says what happened.
    /// </summary>
    public void ApplyUndo(int value)
    {
        _awarded = Clamp(value);
        Source.AwardedMarks = _awarded;
        Source.TeacherMark = _awarded;
        Source.Status = "VALID";
        Source.ReviewReason = null;
        _draft = null;
        _isAccepted = true;
        _isSaving = false;
        _error = null;
        _ledgerLine = $"Set back to {_awarded} · {DateTime.Now:HH:mm}";
        _provenance = DeskProvenance.ForQuestion(Source, _resultVerified);
        NotifyAll();
    }

    private int Clamp(int value) => Math.Clamp(value, 0, Math.Max(0, Max));

    private void NotifyAll()
    {
        OnPropertyChanged(nameof(Draft));
        OnPropertyChanged(nameof(HasDraft));
        OnPropertyChanged(nameof(IsAccepted));
        OnPropertyChanged(nameof(IsChanged));
        OnPropertyChanged(nameof(IsSaving));
        OnPropertyChanged(nameof(CanMark));
        OnPropertyChanged(nameof(Provenance));
        OnPropertyChanged(nameof(State));
        OnPropertyChanged(nameof(StateLabel));
        OnPropertyChanged(nameof(StateGlyph));
        OnPropertyChanged(nameof(StateTone));
        OnPropertyChanged(nameof(IsAttention));
        OnPropertyChanged(nameof(AttentionText));
        OnPropertyChanged(nameof(RailGlyph));
        OnPropertyChanged(nameof(MarkText));
        OnPropertyChanged(nameof(LedgerLine));
        OnPropertyChanged(nameof(Error));
        OnPropertyChanged(nameof(AutomationName));
    }
}
