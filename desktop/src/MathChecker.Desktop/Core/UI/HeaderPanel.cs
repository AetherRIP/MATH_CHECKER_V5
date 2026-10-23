using System.Windows;
using System.Windows.Controls;

namespace MathChecker.Desktop.Core.UI;

/// <summary>
/// Lays out a page header from two children: the title block and the actions.
/// Side by side while both fit, stacked when the window is too narrow — the
/// title is never squeezed to an ellipsis to make room for buttons.
/// </summary>
public sealed class HeaderPanel : Panel
{
    public static readonly DependencyProperty GapProperty = DependencyProperty.Register(
        nameof(Gap), typeof(double), typeof(HeaderPanel), new FrameworkPropertyMetadata(24.0,
            FrameworkPropertyMetadataOptions.AffectsMeasure));

    public static readonly DependencyProperty MinTitleWidthProperty = DependencyProperty.Register(
        nameof(MinTitleWidth), typeof(double), typeof(HeaderPanel), new FrameworkPropertyMetadata(300.0,
            FrameworkPropertyMetadataOptions.AffectsMeasure));

    private bool _stacked;
    private Size _titleSize;
    private Size _actionsSize;

    public double Gap
    {
        get => (double)GetValue(GapProperty);
        set => SetValue(GapProperty, value);
    }

    /// <summary>Below this width the actions move under the title.</summary>
    public double MinTitleWidth
    {
        get => (double)GetValue(MinTitleWidthProperty);
        set => SetValue(MinTitleWidthProperty, value);
    }

    protected override Size MeasureOverride(Size availableSize)
    {
        var title = Children.Count > 0 ? Children[0] : null;
        var actions = Children.Count > 1 ? Children[1] : null;
        var width = double.IsInfinity(availableSize.Width) ? MinTitleWidth * 3 : availableSize.Width;

        actions?.Measure(new Size(width, double.PositiveInfinity));
        _actionsSize = actions?.DesiredSize ?? default;
        _stacked = actions is not null && width - _actionsSize.Width - Gap < MinTitleWidth;

        if (_stacked)
        {
            title?.Measure(new Size(width, double.PositiveInfinity));
            _titleSize = title?.DesiredSize ?? default;
            return new Size(width, _titleSize.Height + Gap + _actionsSize.Height);
        }

        title?.Measure(new Size(Math.Max(0, width - _actionsSize.Width - (actions is null ? 0 : Gap)),
            double.PositiveInfinity));
        _titleSize = title?.DesiredSize ?? default;
        return new Size(width, Math.Max(_titleSize.Height, _actionsSize.Height));
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        var title = Children.Count > 0 ? Children[0] : null;
        var actions = Children.Count > 1 ? Children[1] : null;

        if (actions is null)
        {
            title?.Arrange(new Rect(0, 0, finalSize.Width, finalSize.Height));
            return finalSize;
        }

        if (_stacked)
        {
            title?.Arrange(new Rect(0, 0, finalSize.Width, _titleSize.Height));
            var top = _titleSize.Height + Gap;
            actions.Arrange(new Rect(Math.Max(0, finalSize.Width - _actionsSize.Width), top,
                _actionsSize.Width, _actionsSize.Height));
            return finalSize;
        }

        title?.Arrange(new Rect(0, 0, Math.Max(0, finalSize.Width - _actionsSize.Width - Gap),
            Math.Max(_titleSize.Height, finalSize.Height)));
        actions.Arrange(new Rect(Math.Max(0, finalSize.Width - _actionsSize.Width), 0,
            _actionsSize.Width, _actionsSize.Height));
        return finalSize;
    }
}