using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;

namespace MathChecker.Desktop.Views;

public partial class StudentsView : UserControl
{
    /// <summary>Smallest roster worth showing; below this the page scrolls.</summary>
    private const double RosterFloor = 220;

    public StudentsView()
    {
        InitializeComponent();
    }

    /// <summary>
    /// The roster takes whatever the tiles and the filter bar leave, but never
    /// less than <see cref="RosterFloor"/>: a star row in a short window took
    /// all of it and left the table invisible.
    /// </summary>
    private void OnRosterViewportChanged(object sender, SizeChangedEventArgs e)
    {
        // The auto rows are measured after this pass; fit once layout settles.
        Dispatcher.BeginInvoke(DispatcherPriority.Loaded, new Action(FitRoster));
    }

    private void FitRoster()
    {
        var viewport = RosterScroll.ViewportHeight > 0
            ? RosterScroll.ViewportHeight
            : RosterScroll.ActualHeight;
        if (viewport <= 0)
        {
            return;
        }

        var height = Math.Max(RosterFloor, viewport - TilesRow.ActualHeight - FilterRow.ActualHeight);
        if (Math.Abs(RosterRow.Height.Value - height) > 0.5)
        {
            RosterRow.Height = new GridLength(height);
        }
    }
}
