using System.Windows;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Views;

public partial class SignInWindow : Window
{
    private readonly AppSession _session;

    public SignInWindow(AppSession session, AuthStatusDto? status = null)
    {
        InitializeComponent();
        WindowTheme.ApplyTitleBar(this);
        _session = session;
        if (status is not null && !string.IsNullOrWhiteSpace(status.User))
        {
            UsernameBox.Text = status.User;
        }

        Loaded += (_, _) => UsernameBox.Focus();
    }

    /// <summary>Shows the dialog and returns true only when a session exists.</summary>
    public static bool Prompt(AppSession session, AuthStatusDto status)
    {
        var window = new SignInWindow(session, status) { Owner = Application.Current.MainWindow };
        return window.ShowDialog() == true;
    }

    private async void OnSignIn(object sender, RoutedEventArgs e)
    {
        ErrorText.Text = string.Empty;
        if (string.IsNullOrWhiteSpace(UsernameBox.Text) || PasswordBox.Password.Length == 0)
        {
            ErrorText.Text = "Enter the username and password.";
            return;
        }

        try
        {
            await _session.Api.LoginAsync(UsernameBox.Text.Trim(), PasswordBox.Password,
                RememberBox.IsChecked == true);
            DialogResult = true;
            Close();
        }
        catch (Core.Api.ApiException ex)
        {
            ErrorText.Text = ex.Display;
        }
        catch (Exception ex)
        {
            ErrorText.Text = "Sign in failed: " + ex.Message;
        }
    }
}
