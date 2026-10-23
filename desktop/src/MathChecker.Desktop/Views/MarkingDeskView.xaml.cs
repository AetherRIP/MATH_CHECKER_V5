using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Threading;
using MathChecker.Desktop.Core.Input;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Views;

public partial class MarkingDeskView : UserControl
{
    private MarkingDeskViewModel? _viewModel;
    private PaperPaneViewModel? _paper;
    private bool _compactLayout;
    private bool _showQuestionsInCompact;

    public MarkingDeskView()
    {
        InitializeComponent();
        Loaded += (_, _) =>
        {
            ApplyResponsiveLayout();
            FocusDesk();
        };
        DataContextChanged += OnDataContextChanged;
        PreviewKeyDown += OnPreviewKeyDown;
        SizeChanged += (_, _) => ApplyResponsiveLayout();
    }

    /// <summary>
    /// Three panes need roughly 900 DIP. Below that the desk keeps two: the
    /// paper and the marking panel by default, or the question list and the
    /// marking panel when the teacher flips the toggle. The mark panel is never
    /// pushed off-screen, and the keyboard flow is unchanged.
    /// </summary>
    private void ApplyResponsiveLayout()
    {
        if (!IsLoaded || QuestionPane is null)
        {
            return;
        }

        _compactLayout = ActualWidth < 900;
        QuestionsToggle.Visibility = _compactLayout ? Visibility.Visible : Visibility.Collapsed;

        if (!_compactLayout)
        {
            _showQuestionsInCompact = false;
        }

        var showQuestions = !_compactLayout || _showQuestionsInCompact;
        var showPaper = !_compactLayout || !_showQuestionsInCompact;

        QuestionPane.Visibility = showQuestions ? Visibility.Visible : Visibility.Collapsed;
        QuestionSplitter.Visibility = showQuestions ? Visibility.Visible : Visibility.Collapsed;
        QuestionColumn.Width = showQuestions ? new GridLength(220) : new GridLength(0);
        QuestionColumn.MinWidth = showQuestions ? 176 : 0;
        QuestionSplitColumn.Width = showQuestions ? new GridLength(12) : new GridLength(0);

        PaperPane.Visibility = showPaper ? Visibility.Visible : Visibility.Collapsed;
        PaperSplitter.Visibility = showPaper ? Visibility.Visible : Visibility.Collapsed;
        PaperColumn.Width = showPaper ? new GridLength(_compactLayout ? 1.2 : 1, GridUnitType.Star) : new GridLength(0);
        PaperColumn.MinWidth = showPaper ? (_compactLayout ? 240 : 330) : 0;
        PaperSplitColumn.Width = showPaper ? new GridLength(12) : new GridLength(0);

        // The marking panel shares the row in the compact layout instead of
        // claiming a fixed 360 DIP it cannot have.
        MarkingColumn.Width = _compactLayout ? new GridLength(1, GridUnitType.Star) : new GridLength(360);
        MarkingColumn.MinWidth = _compactLayout ? 240 : 320;
        MarkingColumn.MaxWidth = _compactLayout ? 420 : 480;

        QuestionsToggle.Content = _compactLayout && _showQuestionsInCompact ? "Paper" : "Questions";
    }

    private void OnToggleQuestions(object sender, RoutedEventArgs e)
    {
        _showQuestionsInCompact = !_showQuestionsInCompact;
        ApplyResponsiveLayout();
    }

    private void OnDataContextChanged(object sender, DependencyPropertyChangedEventArgs e)
    {
        if (_paper is not null)
        {
            _paper.PropertyChanged -= OnPaperPropertyChanged;
        }

        if (_viewModel is not null)
        {
            _viewModel.PropertyChanged -= OnDeskPropertyChanged;
        }

        _viewModel = DataContext as MarkingDeskViewModel;
        _paper = _viewModel?.Paper;
        if (_paper is not null)
        {
            _paper.PropertyChanged += OnPaperPropertyChanged;
        }

        if (_viewModel is not null)
        {
            _viewModel.PropertyChanged += OnDeskPropertyChanged;
        }

        FocusDesk();
    }

    private void OnDeskPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (_viewModel is not { } viewModel)
        {
            return;
        }

        if (e.PropertyName == nameof(MarkingDeskViewModel.ActiveQuestion))
        {
            MarkPanelScroll.ScrollToTop();
        }

        // The sheet lives below the question content; opening it must show it,
        // not leave the confirm button off-screen.
        if (e.PropertyName == nameof(MarkingDeskViewModel.IsApprovePanelOpen)
            && viewModel.IsApprovePanelOpen)
        {
            Dispatcher.BeginInvoke(new Action(() => ApprovePanel.BringIntoView()),
                DispatcherPriority.Background);
        }
    }

    private void OnPaperPropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(PaperPaneViewModel.IsAvailable)
            && !IsKeyboardFocusWithin)
        {
            // The page finished arriving while focus was still on the shell.
            Dispatcher.BeginInvoke(new Action(FocusDesk), DispatcherPriority.Background);
        }
    }

    /// <summary>
    /// On open, focus belongs on the paper so N/P and Space work immediately.
    /// If there is no page to show, the desk root holds focus instead.
    /// </summary>
    private void FocusDesk()
    {
        if (!IsVisible)
        {
            return;
        }

        if (PaperView.FocusPaper())
        {
            return;
        }

        if (!IsKeyboardFocusWithin)
        {
            Focus();
        }
    }

    private DeskFocus FocusKind()
    {
        var focused = Keyboard.FocusedElement;
        if (focused is TextBoxBase or PasswordBox or ComboBox)
        {
            return DeskFocus.TextField;
        }

        if (PaperView.IsKeyboardFocusWithin
            || ReferenceEquals(focused, this))
        {
            return DeskFocus.Paper;
        }

        return DeskFocus.Other;
    }

    private void OnPreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (_viewModel is not { } viewModel)
        {
            return;
        }

        var intent = DeskKeyboard.Map(e.Key, Keyboard.Modifiers, FocusKind(),
            viewModel.HasDraft, e.IsRepeat);
        if (intent.Kind == DeskIntentKind.None)
        {
            return;
        }

        Execute(viewModel, intent);
        e.Handled = true;
    }

    private void Execute(MarkingDeskViewModel viewModel, DeskIntent intent)
    {
        switch (intent.Kind)
        {
            case DeskIntentKind.NextQuestion:
                viewModel.NextQuestionCommand.Execute(null);
                break;
            case DeskIntentKind.PreviousQuestion:
                viewModel.PreviousQuestionCommand.Execute(null);
                break;
            case DeskIntentKind.NextPaper:
                viewModel.NextPaperCommand.Execute(null);
                break;
            case DeskIntentKind.PreviousPaper:
                viewModel.PreviousPaperCommand.Execute(null);
                break;
            case DeskIntentKind.CommitOrAccept:
                _ = viewModel.AcceptOrCommitAsync();
                break;
            case DeskIntentKind.Accept:
                _ = viewModel.AcceptAndAdvanceAsync();
                break;
            case DeskIntentKind.StageMark:
                viewModel.StageMark(intent.Value);
                break;
            case DeskIntentKind.ClearDraft:
                ClearOrClose(viewModel);
                break;
            case DeskIntentKind.Approve:
                viewModel.OpenApprovePanel();
                break;
            case DeskIntentKind.Remark:
                viewModel.RemarkCommand.Execute(null);
                break;
            case DeskIntentKind.Export:
                viewModel.ExportPdfCommand.Execute(null);
                break;
            case DeskIntentKind.ZoomIn:
                viewModel.ZoomIn();
                break;
            case DeskIntentKind.ZoomOut:
                viewModel.ZoomOut();
                break;
            case DeskIntentKind.FitWidth:
                viewModel.FitWidth();
                break;
            case DeskIntentKind.PreviousPage:
                viewModel.Paper.PreviousPageCommand.Execute(null);
                break;
            case DeskIntentKind.NextPage:
                viewModel.Paper.NextPageCommand.Execute(null);
                break;
            case DeskIntentKind.CyclePane:
                CyclePane(intent.Value);
                break;
            case DeskIntentKind.Undo:
                _ = viewModel.UndoLastChangeAsync();
                break;
            case DeskIntentKind.Shortcuts:
                viewModel.ToggleShortcutsCommand.Execute(null);
                break;
        }
    }

    private static void ClearOrClose(MarkingDeskViewModel viewModel)
    {
        if (viewModel.HasDraft)
        {
            viewModel.ClearDraft();
            return;
        }

        if (viewModel.IsApprovePanelOpen)
        {
            viewModel.CloseApprovePanel();
            return;
        }

        if (viewModel.IsShortcutsOpen)
        {
            viewModel.ToggleShortcutsCommand.Execute(null);
        }
    }

    /// <summary>F6 cycles paper → rail → mark panel; Shift+F6 goes back.</summary>
    private void CyclePane(int direction)
    {
        var targets = new FrameworkElement[] { PaperView, QuestionRail, MarkPanelFocus };
        var current = Array.FindIndex(targets, target => target.IsKeyboardFocusWithin);
        var next = current < 0
            ? (direction >= 0 ? 0 : targets.Length - 1)
            : (current + direction + targets.Length) % targets.Length;

        var target = targets[next];
        if (ReferenceEquals(target, PaperView))
        {
            if (PaperView.FocusPaper())
            {
                return;
            }

            Focus();
            return;
        }

        target.Focus();
    }
}
