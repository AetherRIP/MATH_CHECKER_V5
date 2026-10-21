using System.Windows.Input;

namespace MathChecker.Desktop.Core.Input;

/// <summary>Where keyboard focus is, which changes what a key is allowed to do.</summary>
public enum DeskFocus
{
    /// <summary>The paper pane owns focus (its scroll host or the desk itself).</summary>
    Paper,

    /// <summary>A text box, password box or combo box owns focus.</summary>
    TextField,

    /// <summary>A button, the rail or the mark panel.</summary>
    Other,
}

public enum DeskIntentKind
{
    None,
    NextQuestion,
    PreviousQuestion,
    NextPaper,
    PreviousPaper,
    CommitOrAccept,
    Accept,
    StageMark,
    ClearDraft,
    Approve,
    Remark,
    Export,
    ZoomIn,
    ZoomOut,
    FitWidth,
    PreviousPage,
    NextPage,
    CyclePane,
    Undo,
    Shortcuts,
}

/// <summary>One keyboard decision, separated from the WPF event that produced it.</summary>
public readonly record struct DeskIntent(DeskIntentKind Kind, int Value = 0)
{
    public static readonly DeskIntent None = new(DeskIntentKind.None);
}

/// <summary>
/// The desk's whole keyboard map as a pure function (spec §3.2). Keeping it out
/// of the view is what makes "digits stage, Enter commits" testable without a
/// window: no key here writes anything by itself.
/// </summary>
public static class DeskKeyboard
{
    public static DeskIntent Map(Key key, ModifierKeys modifiers, DeskFocus focus,
        bool hasDraft, bool isRepeat = false)
    {
        var control = modifiers.HasFlag(ModifierKeys.Control);
        var shift = modifiers.HasFlag(ModifierKeys.Shift);
        var alt = modifiers.HasFlag(ModifierKeys.Alt);

        if (control)
        {
            return key switch
            {
                Key.Add or Key.OemPlus => new DeskIntent(DeskIntentKind.ZoomIn),
                Key.Subtract or Key.OemMinus => new DeskIntent(DeskIntentKind.ZoomOut),
                Key.D0 or Key.NumPad0 => new DeskIntent(DeskIntentKind.FitWidth),
                Key.Z => new DeskIntent(DeskIntentKind.Undo),
                _ => DeskIntent.None,
            };
        }

        // Alt is reserved by the shell and by menus.
        if (alt)
        {
            return DeskIntent.None;
        }

        // Typing must never stage or accept a mark.
        if (focus == DeskFocus.TextField)
        {
            return DeskIntent.None;
        }

        if (key == Key.F6)
        {
            return new DeskIntent(DeskIntentKind.CyclePane, shift ? -1 : 1);
        }

        if (key == Key.PageUp)
        {
            return focus == DeskFocus.Paper
                ? new DeskIntent(DeskIntentKind.PreviousPage)
                : DeskIntent.None;
        }

        if (key == Key.PageDown)
        {
            return focus == DeskFocus.Paper
                ? new DeskIntent(DeskIntentKind.NextPage)
                : DeskIntent.None;
        }

        if (key is Key.Back or Key.Escape)
        {
            return new DeskIntent(DeskIntentKind.ClearDraft);
        }

        if (key == Key.Enter)
        {
            return new DeskIntent(DeskIntentKind.CommitOrAccept);
        }

        if (key == Key.Space)
        {
            // Space on a focused button belongs to that button.
            return focus == DeskFocus.Paper && !isRepeat
                ? new DeskIntent(DeskIntentKind.Accept)
                : DeskIntent.None;
        }

        if (key == Key.A && !isRepeat)
        {
            return new DeskIntent(DeskIntentKind.Approve);
        }

        if (key == Key.R && !isRepeat)
        {
            return new DeskIntent(DeskIntentKind.Remark);
        }

        if (key == Key.E)
        {
            return new DeskIntent(DeskIntentKind.Export);
        }

        if (key == Key.OemQuestion && shift)
        {
            return new DeskIntent(DeskIntentKind.Shortcuts);
        }

        if (TryDigit(key, out var digit))
        {
            return new DeskIntent(DeskIntentKind.StageMark, digit);
        }

        if (key == Key.N)
        {
            return shift
                ? new DeskIntent(DeskIntentKind.NextPaper)
                : new DeskIntent(DeskIntentKind.NextQuestion);
        }

        if (key == Key.P)
        {
            return shift
                ? new DeskIntent(DeskIntentKind.PreviousPaper)
                : new DeskIntent(DeskIntentKind.PreviousQuestion);
        }

        // hasDraft is part of the mapping contract even where the intent itself
        // does not change; the desk uses it to decide what ClearDraft means.
        _ = hasDraft;
        return DeskIntent.None;
    }

    private static bool TryDigit(Key key, out int digit)
    {
        if (key is >= Key.D0 and <= Key.D9)
        {
            digit = (int)key - (int)Key.D0;
            return true;
        }

        if (key is >= Key.NumPad0 and <= Key.NumPad9)
        {
            digit = (int)key - (int)Key.NumPad0;
            return true;
        }

        digit = 0;
        return false;
    }
}
