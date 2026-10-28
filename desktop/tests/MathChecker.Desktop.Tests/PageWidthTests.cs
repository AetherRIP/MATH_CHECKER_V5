using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using MathChecker.Desktop.Views;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// Pages are laid out against the width they actually get, which is not the
/// window's width: at the 900 DIP minimum the 220 DIP rail and the 52 DIP of
/// page margins leave 628 DIP for the page itself.
///
/// A page wider than that does not scroll — it simply loses whatever hangs
/// past its right edge. The Results filter bar did exactly that: seven fixed
/// columns summed to 761 DIP, so the row count and its hint were off-screen at
/// the minimum window size. Each page is measured and arranged here, and any
/// element that lands past the right edge fails the test.
/// </summary>
[Collection(WpfCollection.Name)]
public sealed class PageWidthTests
{
    /// <summary>900 DIP window − 220 DIP rail − 26 DIP margin on each side.</summary>
    private const double MinimumContentWidth = 628;

    /// <summary>A 1280 DIP window with the rail expanded.</summary>
    private const double TypicalContentWidth = 1008;

    private const double Height = 574;

    [Theory]
    [InlineData(MinimumContentWidth)]
    [InlineData(TypicalContentWidth)]
    public void NoPageWidensPastTheSpaceItHas(double width)
    {
        var offenders = new List<string>();

        RunOnSta(() =>
        {
            foreach (var (name, factory) in Pages())
            {
                var host = new Border { Child = factory(), Width = width, Height = Height };
                host.Measure(new Size(width, Height));
                host.Arrange(new Rect(0, 0, width, Height));
                host.UpdateLayout();

                foreach (var (element, right) in PastTheEdge(host, width))
                {
                    offenders.Add($"{name}: {element.GetType().Name} ends at {right:F0} DIP " +
                                  $"(page is {width:F0} DIP)");
                }
            }
        });

        Assert.True(offenders.Count == 0, string.Join(Environment.NewLine, offenders));
    }

    [Fact]
    public void TheCorrectionMonitorStacksItsDetailCardWhenNarrow()
    {
        RunOnSta(() =>
        {
            var view = new WorkersView();
            var host = new Border { Child = view };
            var detail = (FrameworkElement)view.FindName("WorkerDetail")!;

            Arrange(host, MinimumContentWidth);
            Assert.Equal(1, Grid.GetRow(detail));
            Assert.Equal(0, Grid.GetColumn(detail));

            Arrange(host, TypicalContentWidth);
            Assert.Equal(0, Grid.GetRow(detail));
            Assert.Equal(1, Grid.GetColumn(detail));
        });
    }

    private static void Arrange(Border host, double width)
    {
        host.Width = width;
        host.Height = Height;
        host.Measure(new Size(width, Height));
        host.Arrange(new Rect(0, 0, width, Height));
        host.UpdateLayout();
    }

    /// <summary>
    /// Every page that lays itself out from its own width. The marking desk is
    /// the exception: it switches to its two-pane compact layout from Loaded,
    /// and a view measured outside a window never loads, so it is exercised by
    /// its own desk tests instead of being measured here.
    /// </summary>
    private static IEnumerable<(string Name, Func<UserControl> Create)> Pages()
    {
        yield return ("dashboard", () => new DashboardView());
        yield return ("search", () => new SearchView());
        yield return ("correction", () => new CorrectionView());
        yield return ("workers", () => new WorkersView());
        yield return ("jobs", () => new JobsView());
        yield return ("automation", () => new AutomationView());
        yield return ("review", () => new ReviewView());
        yield return ("scheme review", () => new SchemeReviewView());
        yield return ("mark scheme", () => new MarkSchemeView());
        yield return ("results", () => new ResultsView());
        yield return ("reports", () => new ReportsView());
        yield return ("usage", () => new UsageView());
        yield return ("students", () => new StudentsView());
        yield return ("student profile", () => new StudentProfileView());
        yield return ("assignments", () => new AssignmentsView());
        yield return ("create assignment", () => new CreateAssignmentView());
        yield return ("assignment detail", () => new AssignmentDetailView());
        yield return ("settings", () => new SettingsView());
        yield return ("ai keys", () => new AiKeysView());
        yield return ("prompts", () => new PromptsView());
        yield return ("json page", () => new JsonPageView());
    }

    /// <summary>
    /// Elements whose right edge lands outside the page. Content inside a
    /// horizontally scrolling surface is meant to be wider than its viewport,
    /// and a scrollbar paints a few DIP into the padding, so both are skipped.
    /// </summary>
    private static List<(FrameworkElement Element, double Right)> PastTheEdge(Visual host, double width)
    {
        var found = new List<(FrameworkElement, double)>();
        Walk((DependencyObject)host, false);
        return found;

        void Walk(DependencyObject node, bool scrollsSideways)
        {
            var count = VisualTreeHelper.GetChildrenCount(node);
            for (var i = 0; i < count; i++)
            {
                var child = VisualTreeHelper.GetChild(node, i);
                var scrolls = scrollsSideways
                    || child is ScrollBar
                    || child is DataGrid
                    || (child is ScrollViewer viewer
                        && viewer.HorizontalScrollBarVisibility != ScrollBarVisibility.Disabled
                        && viewer.HorizontalScrollBarVisibility != ScrollBarVisibility.Hidden);

                if (child is FrameworkElement element && element.ActualWidth > 0 && !scrolls)
                {
                    var right = element.TransformToAncestor(host)
                        .Transform(new Point(element.ActualWidth, 0)).X;
                    if (right > width + 0.5)
                    {
                        found.Add((element, right));
                    }
                }

                Walk(child, scrolls);
            }
        }
    }

    private static void RunOnSta(Action action)
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            try
            {
                EnsureApplication();
                action();
            }
            catch (Exception ex)
            {
                failure = ex;
            }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();

        if (failure is not null)
        {
            throw new Xunit.Sdk.XunitException(failure.ToString());
        }
    }

    /// <summary>The real App.xaml resources: the same brushes, templates and
    /// converters the application loads.</summary>
    private static void EnsureApplication()
    {
        if (Application.Current is MathChecker.Desktop.App)
        {
            return;
        }

        var app = new MathChecker.Desktop.App();
        app.InitializeComponent();
    }
}
