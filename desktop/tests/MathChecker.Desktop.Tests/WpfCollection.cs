using Xunit;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// Tests that build and measure real WPF controls share one process-wide
/// <see cref="System.Windows.Application"/> (App.xaml resources). xUnit runs
/// test classes in parallel by default, and a second Application instance on
/// another thread makes the whole run fail with "Cannot create more than one
/// System.Windows.Application instance". Every class that renders a view joins
/// this collection, which runs alone.
/// </summary>
[CollectionDefinition(Name, DisableParallelization = true)]
public sealed class WpfCollection
{
    public const string Name = "wpf";
}
