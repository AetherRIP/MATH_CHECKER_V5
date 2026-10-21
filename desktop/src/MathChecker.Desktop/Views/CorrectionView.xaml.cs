using System.Windows;
using System.Windows.Controls;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Views;

public partial class CorrectionView : UserControl
{
    public CorrectionView()
    {
        InitializeComponent();
    }

    private void OnDragOver(object sender, DragEventArgs e)
    {
        e.Effects = e.Data.GetDataPresent(DataFormats.FileDrop) ? DragDropEffects.Copy : DragDropEffects.None;
        e.Handled = true;
    }

    private void OnDrop(object sender, DragEventArgs e)
    {
        if (DataContext is not CorrectionViewModel viewModel
            || !e.Data.GetDataPresent(DataFormats.FileDrop)
            || e.Data.GetData(DataFormats.FileDrop) is not string[] files
            || files.Length == 0)
        {
            return;
        }

        // One file at a time: the student's work and the mark scheme are
        // separate fields, so a two-file drop fills both and anything else is
        // reported instead of guessed.
        if (files.Length >= 2)
        {
            viewModel.SetSubmission(files[0]);
            viewModel.SetMarkScheme(files[1]);
            viewModel.Status = "Both files attached from the drop.";
        }
        else if (viewModel.SubmissionPath is null)
        {
            viewModel.SetSubmission(files[0]);
            viewModel.Status = "Attached as the student's work. Drop or choose the mark scheme next.";
        }
        else
        {
            viewModel.SetMarkScheme(files[0]);
            viewModel.Status = "Attached as the mark scheme.";
        }

        e.Handled = true;
    }
}
