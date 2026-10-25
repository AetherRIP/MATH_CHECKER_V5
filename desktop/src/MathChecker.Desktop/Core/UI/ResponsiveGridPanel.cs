using System.Windows;
using System.Windows.Controls;

namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// A grid that fits as many equal-width cells per row as the window allows.
///
/// The Correction monitor needs this: a card has a readable minimum width, but
/// its width should grow with the page instead of leaving a dead gap, and the
/// column count must follow the real window width rather than a fixed number.
/// <see cref="WrapPanel"/> cannot do that — it gives every child its own
/// requested width and leaves the row ragged.
/// </summary>
public sealed class ResponsiveGridPanel : Panel
{
    public static readonly DependencyProperty MinItemWidthProperty = DependencyProperty.Register(
        nameof(MinItemWidth), typeof(double), typeof(ResponsiveGridPanel),
        new FrameworkPropertyMetadata(320d, FrameworkPropertyMetadataOptions.AffectsMeasure));

    public static readonly DependencyProperty GapProperty = DependencyProperty.Register(
        nameof(Gap), typeof(double), typeof(ResponsiveGridPanel),
        new FrameworkPropertyMetadata(14d, FrameworkPropertyMetadataOptions.AffectsMeasure));

    /// <summary>Narrowest a cell may be. Below this the row drops a column.</summary>
    public double MinItemWidth
    {
        get => (double)GetValue(MinItemWidthProperty);
        set => SetValue(MinItemWidthProperty, value);
    }

    /// <summary>Space between cells, horizontally and vertically.</summary>
    public double Gap
    {
        get => (double)GetValue(GapProperty);
        set => SetValue(GapProperty, value);
    }

    /// <summary>How many columns fit in a width, and how wide each one is.</summary>
    public static (int Columns, double ItemWidth) Fit(double width, int count,
        double minItemWidth, double gap)
    {
        if (count <= 0)
        {
            return (0, 0);
        }

        var minimum = Math.Max(1, minItemWidth);
        var spacing = Math.Max(0, gap);
        var columns = (int)Math.Floor((width + spacing) / (minimum + spacing));
        columns = Math.Clamp(columns, 1, count);
        var itemWidth = (width - spacing * (columns - 1)) / columns;
        return (columns, Math.Max(minimum, itemWidth));
    }

    protected override Size MeasureOverride(Size availableSize)
    {
        var count = InternalChildren.Count;
        if (count == 0)
        {
            return new Size(0, 0);
        }

        var width = double.IsInfinity(availableSize.Width)
            ? MinItemWidth * count + Gap * (count - 1)
            : availableSize.Width;

        var (columns, itemWidth) = Fit(width, count, MinItemWidth, Gap);
        var rows = (int)Math.Ceiling(count / (double)columns);
        var height = 0d;
        for (var row = 0; row < rows; row++)
        {
            var rowHeight = 0d;
            for (var column = 0; column < columns; column++)
            {
                var index = row * columns + column;
                if (index >= count)
                {
                    break;
                }

                var child = InternalChildren[index];
                child.Measure(new Size(itemWidth, double.PositiveInfinity));
                rowHeight = Math.Max(rowHeight, child.DesiredSize.Height);
            }

            height += rowHeight;
            if (row < rows - 1)
            {
                height += Gap;
            }
        }

        return new Size(width, height);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        var count = InternalChildren.Count;
        if (count == 0)
        {
            return finalSize;
        }

        var width = double.IsInfinity(finalSize.Width)
            ? MinItemWidth * count + Gap * (count - 1)
            : finalSize.Width;
        var (columns, itemWidth) = Fit(width, count, MinItemWidth, Gap);

        var y = 0d;
        for (var start = 0; start < count; start += columns)
        {
            var end = Math.Min(start + columns, count);

            // Every cell in a row is as tall as its tallest neighbour, so the
            // rows read as rows instead of a staircase.
            var rowHeight = 0d;
            for (var index = start; index < end; index++)
            {
                rowHeight = Math.Max(rowHeight, InternalChildren[index].DesiredSize.Height);
            }

            for (var index = start; index < end; index++)
            {
                var x = (index - start) * (itemWidth + Gap);
                InternalChildren[index].Arrange(new Rect(x, y, itemWidth, rowHeight));
            }

            y += rowHeight + Gap;
        }

        return finalSize;
    }
}
