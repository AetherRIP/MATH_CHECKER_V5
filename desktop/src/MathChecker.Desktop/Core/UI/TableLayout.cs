using System.Windows;
using System.Windows.Controls;

namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// Responsive tables: instead of squeezing every column until the text clips,
/// a table drops its least important columns as the window narrows and keeps a
/// deliberate horizontal scroll for the rest. Mark a column with a priority
/// (0 = always visible, 3 = first to go) and set IsResponsive on the grid.
/// </summary>
public static class TableLayout
{
    /// <summary>Column priority. Higher numbers disappear earlier.</summary>
    public static readonly DependencyProperty PriorityProperty = DependencyProperty.RegisterAttached(
        "Priority", typeof(int), typeof(TableLayout), new PropertyMetadata(0));

    /// <summary>Enables the width breakpoints on a DataGrid.</summary>
    public static readonly DependencyProperty IsResponsiveProperty = DependencyProperty.RegisterAttached(
        "IsResponsive", typeof(bool), typeof(TableLayout),
        new PropertyMetadata(false, OnIsResponsiveChanged));

    /// <summary>
    /// Index of the column that absorbs the table's spare width. A star column
    /// looks right until the grid's visibility rules start collapsing columns;
    /// WPF then keeps the stale star width and leaves a dead gap at the end of
    /// every row. This column is measured to the viewport instead.
    /// </summary>
    public static readonly DependencyProperty FillColumnProperty = DependencyProperty.RegisterAttached(
        "FillColumn", typeof(int), typeof(TableLayout),
        new PropertyMetadata(-1, OnFillColumnChanged));

    private static readonly DependencyProperty AdjustingProperty = DependencyProperty.RegisterAttached(
        "Adjusting", typeof(bool), typeof(TableLayout), new PropertyMetadata(false));

    public static void SetPriority(DependencyObject element, int value) => element.SetValue(PriorityProperty, value);

    public static int GetPriority(DependencyObject element) => (int)element.GetValue(PriorityProperty);

    public static void SetIsResponsive(DependencyObject element, bool value) =>
        element.SetValue(IsResponsiveProperty, value);

    public static bool GetIsResponsive(DependencyObject element) => (bool)element.GetValue(IsResponsiveProperty);

    public static void SetFillColumn(DependencyObject element, int value) =>
        element.SetValue(FillColumnProperty, value);

    public static int GetFillColumn(DependencyObject element) => (int)element.GetValue(FillColumnProperty);

    private static void OnIsResponsiveChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is not DataGrid grid)
        {
            return;
        }

        grid.SizeChanged -= OnGridSizeChanged;
        if (e.NewValue is not true)
        {
            return;
        }

        grid.SizeChanged += OnGridSizeChanged;
        grid.Loaded += OnGridLoaded;
        if (grid.IsLoaded)
        {
            Apply(grid);
        }
    }

    private static void OnFillColumnChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is DataGrid grid && grid.IsLoaded)
        {
            Apply(grid);
        }
    }

    private static void OnGridLoaded(object sender, RoutedEventArgs e)
    {
        if (sender is DataGrid grid)
        {
            Apply(grid);
        }
    }

    private static void OnGridSizeChanged(object sender, SizeChangedEventArgs e)
    {
        if (sender is DataGrid grid)
        {
            Apply(grid);
        }
    }

    /// <summary>
    /// Breakpoints are deliberately generous: a column only disappears once the
    /// remaining table still has room to read. Below 480 DIP only priority 0
    /// columns remain and the grid scrolls horizontally instead of shrinking.
    /// </summary>
    public static int MaxPriorityFor(double width) => width switch
    {
        >= 820 => 3,
        >= 690 => 2,
        >= 480 => 1,
        _ => 0,
    };

    private static void Apply(DataGrid grid)
    {
        if ((bool)grid.GetValue(AdjustingProperty))
        {
            return;
        }

        var width = grid.ActualWidth;
        if (double.IsNaN(width) || width <= 0)
        {
            return;
        }

        grid.SetValue(AdjustingProperty, true);
        try
        {
            var max = MaxPriorityFor(width);
            var changed = false;
            foreach (var column in grid.Columns)
            {
                var visible = GetPriority(column) <= max;
                var target = visible ? Visibility.Visible : Visibility.Collapsed;
                if (column.Visibility == target)
                {
                    continue;
                }

                column.Visibility = target;
                changed = true;
            }

            // The breakpoints are approximate: a table can sit just above one
            // and still be a few pixels too narrow for the flexible column's
            // floor. Rather than let that spill into a stray scrollbar, give up
            // the next column of the highest visible priority.
            var index = GetFillColumn(grid);
            if (index >= 0 && index < grid.Columns.Count && grid.Columns[index].Visibility == Visibility.Visible)
            {
                var floor = FloorFor(grid.Columns[index].MinWidth);
                var level = max;
                while (level > 0 && width - UsedWidth(grid, index) - Chrome < floor)
                {
                    var victim = grid.Columns.LastOrDefault(c =>
                        c.Visibility == Visibility.Visible && GetPriority(c) == level &&
                        !ReferenceEquals(c, grid.Columns[index]));
                    if (victim is null)
                    {
                        level--;
                        continue;
                    }

                    victim.Visibility = Visibility.Collapsed;
                    changed = true;
                }
            }

            if (changed)
            {
                // Visibility changes alter the space left for the fill column.
                grid.InvalidateMeasure();
            }

            ApplyFill(grid);
        }
        finally
        {
            grid.SetValue(AdjustingProperty, false);
        }
    }

    private static double Chrome => SystemParameters.VerticalScrollBarWidth + 4;

    /// <summary>Readable floor for a flexible column.</summary>
    public static double FloorFor(double columnMinWidth) =>
        double.IsNaN(columnMinWidth) || columnMinWidth <= 20 ? 96 : columnMinWidth;

    private static double UsedWidth(DataGrid grid, int skipIndex)
    {
        var used = 0d;
        for (var i = 0; i < grid.Columns.Count; i++)
        {
            var column = grid.Columns[i];
            if (i == skipIndex || column.Visibility != Visibility.Visible)
            {
                continue;
            }

            used += column.Width.IsAbsolute ? column.Width.Value : column.ActualWidth;
        }

        return used;
    }

    /// <summary>
    /// Width for the flexible column: whatever the fixed columns leave, minus
    /// the grid's border and the vertical scrollbar (otherwise the last column
    /// slides under the bar and clips its own text). The few spare pixels keep
    /// per-column device-pixel rounding from conjuring a horizontal scrollbar.
    /// </summary>
    public static double FillWidth(double gridWidth, double used, double columnMinWidth) =>
        Math.Floor(Math.Max(FloorFor(columnMinWidth), gridWidth - used - Chrome));

    private static bool ApplyFill(DataGrid grid)
    {
        var index = GetFillColumn(grid);
        if (index < 0 || index >= grid.Columns.Count)
        {
            return false;
        }

        var fill = grid.Columns[index];
        if (fill.Visibility != Visibility.Visible || double.IsNaN(grid.ActualWidth) || grid.ActualWidth <= 0)
        {
            return false;
        }

        var target = FillWidth(grid.ActualWidth, UsedWidth(grid, index), fill.MinWidth);
        if (fill.Width.IsAbsolute && Math.Abs(fill.Width.Value - target) < 0.5)
        {
            return false;
        }

        fill.Width = new DataGridLength(target);
        return true;
    }
}
