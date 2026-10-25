using System.Windows;
using System.Windows.Controls;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Views;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// Every page is measured and arranged at the three supported window sizes.
/// XAML resource mistakes (a missing StaticResource, a broken template) throw
/// here instead of in front of a teacher.
/// </summary>
[Collection(WpfCollection.Name)]
public sealed class ViewSmokeTests
{
    private static readonly (int Width, int Height)[] Sizes =
    {
        (900, 600),
        (1280, 800),
        (1440, 900),
    };

    [Fact]
    public void EveryPageRendersAtEveryTargetSize()
    {
        RunOnSta(() =>
        {
            foreach (var (width, height) in Sizes)
            {
                foreach (var factory in PageViews())
                {
                    var view = factory();
                    view.Width = width;
                    view.Height = height;
                    view.Measure(new Size(width, height));
                    view.Arrange(new Rect(0, 0, width, height));
                    view.UpdateLayout();
                }
            }
        });
    }

    [Fact]
    public void PageFrameExposesItsHeaderAndStates()
    {
        RunOnSta(() =>
        {
            // A code-created control is not in a tree, so the implicit style is
            // applied explicitly here; inside the window the same style is
            // resolved automatically.
            var frame = new PageFrame
            {
                Style = (Style)Application.Current.FindResource(typeof(PageFrame)),
                Eyebrow = "REVIEW",
                Title = "Queue",
                Description = "2 results need a decision.",
                StatusText = "Loaded",
                ErrorText = "HTTP 500",
                IsBusy = true,
            };
            frame.Measure(new Size(900, 600));
            frame.Arrange(new Rect(0, 0, 900, 600));
            frame.UpdateLayout();

            Assert.True(frame.HasError);
            Assert.Contains("Queue", DescendantText(frame));
            Assert.Contains("REVIEW", DescendantText(frame));
            Assert.Contains("HTTP 500", DescendantText(frame));

            frame.ErrorText = string.Empty;
            Assert.False(frame.HasError);
        });
    }

    [Fact]
    public void JsonSectionTemplatesRenderWithData()
    {
        RunOnSta(() =>
        {
            // Section cards are built by a DataTemplate, so they only load once
            // the page has data; the empty page in the smoke test never sees
            // the Expander and its header style.
            var view = new JsonPageView
            {
                DataContext = new
                {
                    Title = "Storage",
                    Eyebrow = "SYSTEM",
                    Description = "Inventory",
                    Status = (string?)null,
                    Error = (string?)null,
                    IsBusy = false,
                    Actions = Array.Empty<object>(),
                    Sections = new[]
                    {
                        new ViewModels.JsonSectionVm("Inventory", "data/inventory") { Raw = "{ }" },
                    },
                },
            };
            view.Width = 900;
            view.Height = 600;
            view.Measure(new Size(900, 600));
            view.Arrange(new Rect(0, 0, 900, 600));
            view.UpdateLayout();
        });
    }

    [Fact]
    public void A_chosen_course_hides_the_placeholder_instead_of_drawing_over_it()
    {
        RunOnSta(() =>
        {
            // The placeholder is drawn ON TOP of the combo box, so it must
            // collapse the moment a course is selected. It stayed visible for a
            // selected object (the converter only recognised strings), which
            // painted "Choose a course" over the course's own name.
            var course = new MathChecker.Desktop.Models.ClassroomCourseDto
            {
                Id = "c1",
                Name = "WBIS - O Level Mathematics - Mr. Abanout Akram",
                Section = "Set 1",
            };
            var view = new ClassroomView
            {
                DataContext = new ClassroomTargetStub
                {
                    Courses = new[] { course },
                    SelectedCourse = course,
                },
            };
            view.Width = 1280;
            view.Height = 800;
            view.Measure(new Size(1280, 800));
            view.Arrange(new Rect(0, 0, 1280, 800));
            view.UpdateLayout();

            var placeholder = FindText(view, "Choose a course");
            Assert.NotNull(placeholder);
            Assert.Equal(Visibility.Collapsed, placeholder!.Visibility);
        });
    }

    [Theory]
    [InlineData(1200, 3)]
    [InlineData(900, 3)]
    [InlineData(760, 2)]
    [InlineData(600, 1)]
    [InlineData(460, 0)]
    public void ResponsiveTableDropsColumnsByWidth(double width, int expectedPriority)
    {
        Assert.Equal(expectedPriority, TableLayout.MaxPriorityFor(width));
    }

    [Fact]
    public void FlexibleColumnAbsorbsTheSpareWidth()
    {
        // 527 DIP of table, 278 DIP of fixed columns, a 150 DIP floor: the
        // assignment column takes the rest and the row has no dead gap.
        Assert.Equal(228, TableLayout.FillWidth(527, 278, 150));
    }

    [Fact]
    public void FlexibleColumnKeepsItsFloorWhenTheTableIsTooNarrow()
    {
        Assert.Equal(150, TableLayout.FillWidth(300, 278, 150));
    }

    [Fact]
    public void FlexibleColumnIsReadableWithoutAnExplicitMinimum()
    {
        // WPF's own default MinWidth is 20 DIP, which cannot show a word.
        Assert.Equal(96, TableLayout.FillWidth(200, 180, 0));
    }

    private static IEnumerable<Func<UserControl>> PageViews()
    {
        yield return () => new DashboardView();
        yield return () => new SearchView();
        yield return () => new CorrectionView();
        yield return () => new WorkersView();
        yield return () => new JobsView();
        yield return () => new AutomationView();
        yield return () => new ReviewView();
        yield return () => new SchemeReviewView();
        yield return () => new MarkingDeskView();
        yield return () => new ResultsView();
        yield return () => new ReportsView();
        yield return () => new UsageView();
        yield return () => new ClassroomView();
        yield return () => new ClassesView();
        yield return () => new StudentsView();
        yield return () => new StudentProfileView();
        yield return () => new AssignmentsView();
        yield return () => new SettingsView();
        yield return () => new AiKeysView();
        yield return () => new PromptsView();
        yield return () => new JsonPageView();
    }

    /// <summary>A mutable target for the combo's two-way selection bindings;
    /// an anonymous type is read-only and WPF refuses the binding.</summary>
    private sealed class ClassroomTargetStub
    {
        public MathChecker.Desktop.Models.ClassroomCourseDto[] Courses { get; set; } =
            Array.Empty<MathChecker.Desktop.Models.ClassroomCourseDto>();

        public MathChecker.Desktop.Models.ClassroomCourseDto? SelectedCourse { get; set; }

        public object[] Assignments { get; set; } = Array.Empty<object>();

        public object? SelectedAssignment { get; set; }

        public bool IsConnected { get; set; } = true;
    }

    private static TextBlock? FindText(DependencyObject root, string text)
    {
        if (root is TextBlock block && block.Text == text)
        {
            return block;
        }

        var count = System.Windows.Media.VisualTreeHelper.GetChildrenCount(root);
        for (var i = 0; i < count; i++)
        {
            var found = FindText(System.Windows.Media.VisualTreeHelper.GetChild(root, i), text);
            if (found is not null)
            {
                return found;
            }
        }

        return null;
    }

    private static string DescendantText(DependencyObject root)
    {
        var texts = new List<string>();
        Walk(root, texts);
        return string.Join("\n", texts);

        static void Walk(DependencyObject node, List<string> output)
        {
            if (node is TextBlock text)
            {
                output.Add(text.Text);
            }

            var count = System.Windows.Media.VisualTreeHelper.GetChildrenCount(node);
            for (var i = 0; i < count; i++)
            {
                Walk(System.Windows.Media.VisualTreeHelper.GetChild(node, i), output);
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

    /// <summary>Reuses the real App.xaml resources so the test exercises the
    /// same brushes, templates and converters the application loads.</summary>
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