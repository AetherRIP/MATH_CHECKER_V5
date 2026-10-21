using System.Windows;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.Views;

public partial class SetupWindow : Window
{
    private readonly AppSession _session;

    public SetupWindow(AppSession session, SetupStatusDto status)
    {
        InitializeComponent();
        WindowTheme.ApplyDarkTitleBar(this);
        _session = session;
        TeacherBox.Text = status.Teacher?.TeacherName ?? string.Empty;
        SchoolBox.Text = status.Teacher?.SchoolName ?? string.Empty;

        var checks = status.Checks;
        ChecksText.Text = checks is null
            ? string.Empty
            : $"AI keys: {(checks.AiKeys ? $"{checks.AiKeyCount} configured" : "none — corrections cannot run yet")}\n"
              + $"Classroom OAuth client: {(checks.ClassroomOauthClient ? "present" : "missing")}\n"
              + $"Classroom account: {(checks.ClassroomConnected ? "connected" : "not connected")}\n"
              + $"Data folder writable: {(checks.StorageWritable ? "yes" : "NO")}";
    }

    /// <summary>True when the user asked to go straight to the key page.</summary>
    public bool KeysRequested { get; private set; }

    private async void OnSave(object sender, RoutedEventArgs e)
    {
        ErrorText.Text = string.Empty;
        var teacher = TeacherBox.Text.Trim();
        if (teacher.Length == 0)
        {
            ErrorText.Text = "A teacher name is required before saving.";
            return;
        }

        try
        {
            await _session.Api.CompleteSetupAsync(teacher, SchoolBox.Text.Trim());
            KeysRequested = KeysBox.IsChecked == true;
            DialogResult = true;
            Close();
        }
        catch (ApiException ex)
        {
            ErrorText.Text = ex.Display;
        }
        catch (Exception ex)
        {
            ErrorText.Text = "Could not save setup: " + ex.Message;
        }
    }

    private void OnSkip(object sender, RoutedEventArgs e)
    {
        DialogResult = false;
        Close();
    }
}
