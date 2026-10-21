using System.Windows.Controls;
using System.Windows.Input;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Views;

public partial class SearchView : UserControl
{
    public SearchView()
    {
        InitializeComponent();
    }

    private void OnQueryKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter && DataContext is SearchViewModel viewModel)
        {
            viewModel.SearchCommand.Execute(null);
        }
    }

    private void OnRowDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (DataContext is not SearchViewModel viewModel || viewModel.Hits.Count == 0)
        {
            return;
        }

        var hit = (sender as DataGrid)?.SelectedItem as SearchHit;
        if (hit is null)
        {
            return;
        }

        // The backend's deep links are browser routes; map them to desktop pages.
        var route = hit.Route ?? string.Empty;
        var page = route.Contains("students", StringComparison.OrdinalIgnoreCase) ? "students"
            : route.Contains("assignments", StringComparison.OrdinalIgnoreCase) ? "assignments"
            : route.Contains("results", StringComparison.OrdinalIgnoreCase) ? "results"
            : route.Contains("classroom", StringComparison.OrdinalIgnoreCase) ? "classroom"
            : null;
        if (page is not null)
        {
            viewModel.Session.GoTo(page);
        }
    }
}
