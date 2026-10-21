using System.Windows.Input;
using MathChecker.Desktop.Core.Input;

namespace MathChecker.Desktop.Tests;

public class DeskKeyboardTests
{
    [Theory]
    [InlineData(Key.N, ModifierKeys.None, DeskIntentKind.NextQuestion)]
    [InlineData(Key.P, ModifierKeys.None, DeskIntentKind.PreviousQuestion)]
    [InlineData(Key.N, ModifierKeys.Shift, DeskIntentKind.NextPaper)]
    [InlineData(Key.P, ModifierKeys.Shift, DeskIntentKind.PreviousPaper)]
    [InlineData(Key.Enter, ModifierKeys.None, DeskIntentKind.CommitOrAccept)]
    [InlineData(Key.Back, ModifierKeys.None, DeskIntentKind.ClearDraft)]
    [InlineData(Key.Escape, ModifierKeys.None, DeskIntentKind.ClearDraft)]
    [InlineData(Key.A, ModifierKeys.None, DeskIntentKind.Approve)]
    [InlineData(Key.R, ModifierKeys.None, DeskIntentKind.Remark)]
    [InlineData(Key.E, ModifierKeys.None, DeskIntentKind.Export)]
    public void Letters_and_enter_map_to_their_intent(Key key, ModifierKeys modifiers,
        DeskIntentKind expected)
    {
        var intent = DeskKeyboard.Map(key, modifiers, DeskFocus.Other, hasDraft: false);

        Assert.Equal(expected, intent.Kind);
    }

    [Theory]
    [InlineData(Key.D0, 0)]
    [InlineData(Key.D3, 3)]
    [InlineData(Key.D9, 9)]
    [InlineData(Key.NumPad7, 7)]
    public void Digits_stage_a_value_without_saving(Key key, int value)
    {
        var intent = DeskKeyboard.Map(key, ModifierKeys.None, DeskFocus.Paper, hasDraft: false);

        Assert.Equal(DeskIntentKind.StageMark, intent.Kind);
        Assert.Equal(value, intent.Value);
    }

    [Fact]
    public void Space_accepts_only_when_the_paper_has_focus()
    {
        Assert.Equal(DeskIntentKind.Accept,
            DeskKeyboard.Map(Key.Space, ModifierKeys.None, DeskFocus.Paper, false).Kind);
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.Space, ModifierKeys.None, DeskFocus.Other, false).Kind);
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.Space, ModifierKeys.None, DeskFocus.Paper, false, isRepeat: true).Kind);
    }

    [Fact]
    public void Typing_in_a_text_field_never_stages_or_accepts()
    {
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.D3, ModifierKeys.None, DeskFocus.TextField, false).Kind);
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.N, ModifierKeys.None, DeskFocus.TextField, false).Kind);
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.Enter, ModifierKeys.None, DeskFocus.TextField, true).Kind);
    }

    [Fact]
    public void Page_keys_and_space_belong_to_the_paper()
    {
        Assert.Equal(DeskIntentKind.PreviousPage,
            DeskKeyboard.Map(Key.PageUp, ModifierKeys.None, DeskFocus.Paper, false).Kind);
        Assert.Equal(DeskIntentKind.NextPage,
            DeskKeyboard.Map(Key.PageDown, ModifierKeys.None, DeskFocus.Paper, false).Kind);
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.PageDown, ModifierKeys.None, DeskFocus.Other, false).Kind);
    }

    [Theory]
    [InlineData(Key.Add, DeskIntentKind.ZoomIn)]
    [InlineData(Key.OemPlus, DeskIntentKind.ZoomIn)]
    [InlineData(Key.Subtract, DeskIntentKind.ZoomOut)]
    [InlineData(Key.OemMinus, DeskIntentKind.ZoomOut)]
    [InlineData(Key.D0, DeskIntentKind.FitWidth)]
    [InlineData(Key.Z, DeskIntentKind.Undo)]
    public void Control_chords_are_explicit(Key key, DeskIntentKind expected)
    {
        var intent = DeskKeyboard.Map(key, ModifierKeys.Control, DeskFocus.Paper, false);

        Assert.Equal(expected, intent.Kind);
    }

    [Fact]
    public void Repeat_is_ignored_for_destructive_intents()
    {
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.A, ModifierKeys.None, DeskFocus.Other, false, isRepeat: true).Kind);
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.R, ModifierKeys.None, DeskFocus.Other, false, isRepeat: true).Kind);
    }

    [Fact]
    public void Alt_is_left_to_the_shell_and_f6_cycles_panes()
    {
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.N, ModifierKeys.Alt, DeskFocus.Other, false).Kind);
        Assert.Equal(1,
            DeskKeyboard.Map(Key.F6, ModifierKeys.None, DeskFocus.Other, false).Value);
        Assert.Equal(-1,
            DeskKeyboard.Map(Key.F6, ModifierKeys.Shift, DeskFocus.Other, false).Value);
    }

    [Fact]
    public void Question_mark_opens_the_shortcut_sheet()
    {
        Assert.Equal(DeskIntentKind.Shortcuts,
            DeskKeyboard.Map(Key.OemQuestion, ModifierKeys.Shift, DeskFocus.Other, false).Kind);
        Assert.Equal(DeskIntentKind.None,
            DeskKeyboard.Map(Key.OemQuestion, ModifierKeys.None, DeskFocus.Other, false).Kind);
    }
}
