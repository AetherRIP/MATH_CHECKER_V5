namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// One choice in a drop-down: what a person reads, plus the value the backend
/// understands. ComboBoxes bind <c>SelectedValuePath="Value"</c> so the
/// property stays the machine value while the list shows a sentence-case label
/// instead of <c>needs_review</c> or <c>QUEUED</c>.
/// </summary>
public sealed record ChoiceOption(string Label, string Value)
{
    public override string ToString() => Label;
}
