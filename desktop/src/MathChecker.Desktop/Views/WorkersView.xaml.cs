using System.Windows;
using System.Windows.Controls;

namespace MathChecker.Desktop.Views;

/// <summary>
/// The Correction monitor: a first-class page of worker cards. Everything it
/// shows is bound to <see cref="ViewModels.WorkersViewModel"/>; the only state
/// the view holds is the layout choice described below.
/// </summary>
public partial class WorkersView : UserControl
{
    /// <summary>320 DIP of cards, a 14 DIP gap and a 360 DIP detail card:
    /// below this the two panes cannot share a row.</summary>
    private const double SideBySideWidth = 760;

    public WorkersView()
    {
        InitializeComponent();
        SizeChanged += (_, _) => ApplyResponsiveLayout();
    }

    /// <summary>
    /// Beside the grid while both fit, under it when the window is too narrow.
    /// Both panes used to keep their place in one row, so at the minimum window
    /// width the detail card sat outside the page and its Close button was
    /// unreachable.
    /// </summary>
    private void ApplyResponsiveLayout()
    {
        if (WorkerDetail is null || WorkerCardsColumn is null)
        {
            return;
        }

        var width = ActualWidth > 0 ? ActualWidth : Width;
        if (double.IsNaN(width) || width <= 0)
        {
            return;
        }

        var stacked = width < SideBySideWidth;
        Grid.SetColumn(WorkerDetail, stacked ? 0 : 1);
        Grid.SetRow(WorkerDetail, stacked ? 1 : 0);
        WorkerDetail.Width = stacked ? double.NaN : 360;
        WorkerDetail.HorizontalAlignment = stacked ? HorizontalAlignment.Stretch : HorizontalAlignment.Right;
        WorkerDetail.Margin = stacked ? new Thickness(0, 12, 0, 0) : new Thickness(14, 0, 0, 0);
        WorkerDetail.MaxHeight = stacked ? 320 : 720;
        WorkerCardsColumn.MinWidth = stacked ? 240 : 320;
    }
}
