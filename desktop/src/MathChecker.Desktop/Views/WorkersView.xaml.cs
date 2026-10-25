using System.Windows.Controls;

namespace MathChecker.Desktop.Views;

/// <summary>
/// The Correction monitor: a first-class page of worker cards. Everything it
/// shows is bound to <see cref="ViewModels.WorkersViewModel"/>; the view itself
/// holds no state.
/// </summary>
public partial class WorkersView : UserControl
{
    public WorkersView()
    {
        InitializeComponent();
    }
}
