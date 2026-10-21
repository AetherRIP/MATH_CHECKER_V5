using System.Net;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Core.Backend;
using MathChecker.Desktop.Core.Configuration;
using MathChecker.Desktop.Core.Diagnostics;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

public class StudentsPageTests
{
    private const string StudentsBody = """
        {"students":[
          {"student_id":"s1","official_name":"Student A","class_label":"10A","email":"student.a@demo.invalid"},
          {"student_id":"s2","official_name":"Student B","class_label":"10A","email":"student.b@demo.invalid"},
          {"student_id":"s3","official_name":"Student C","class_label":"10B","email":"student.c@demo.invalid"},
          {"student_id":"s4","official_name":"Student D","class_label":"10B","email":"student.d@demo.invalid"},
          {"student_id":"s5","official_name":"Student E","class_label":null,"email":"student.e@demo.invalid"}
        ]}
        """;

    [Fact]
    public async Task Loading_students_keeps_every_row_after_the_class_list_is_rebuilt()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new StudentsViewModel(session);

            await page.LoadAsync();

            Assert.Null(page.Error);
            Assert.Equal(5, page.Items.Count);
            Assert.Contains("All classes", page.ClassFilters);
            Assert.Contains("10A", page.ClassFilters);
        }
    }

    [Fact]
    public async Task A_null_class_selection_means_no_filter_rather_than_no_rows()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new StudentsViewModel(session);
            await page.LoadAsync();

            // WPF writes null through the two-way SelectedItem binding when the
            // filter list is cleared; that must not hide every student.
            page.ClassFilter = null!;

            Assert.Equal(5, page.Items.Count);
            Assert.Equal("All classes", page.ClassFilter);
        }
    }

    [Fact]
    public async Task Class_and_text_filters_narrow_the_list_and_survive_a_reload()
    {
        var (session, handler) = await SessionAsync();
        using (handler)
        {
            var page = new StudentsViewModel(session);
            await page.LoadAsync();

            page.ClassFilter = "10B";
            Assert.Equal(2, page.Items.Count);

            page.Filter = "Student D";
            Assert.Single(page.Items);

            page.Filter = string.Empty;
            await page.LoadAsync();
            Assert.Equal(2, page.Items.Count);
            Assert.Equal("10B", page.ClassFilter);
        }
    }

    private static async Task<(AppSession Session, StubHttpHandler Handler)> SessionAsync()
    {
        var handler = new StubHttpHandler().Respond(HttpStatusCode.OK, StudentsBody);
        var settings = new DesktopSettings { Port = 9, StartBackendAutomatically = false };
        var host = new BackendHost(settings, new MemoryLog(),
            starter: new UnusedStarter(), health: new AlwaysOurs());
        var status = await host.StartAsync();
        Assert.True(status.IsUsable);

        var api = new MathCheckerApiClient(handler);
        api.SetBaseUrl(host.BaseUrl);
        return (new AppSession(settings, new MemoryLog(), host, api, new SilentDialogs()), handler);
    }

    private sealed class AlwaysOurs : IHealthProbe
    {
        public Task<HealthResult> CheckAsync(int port, CancellationToken cancellationToken) =>
            Task.FromResult(new HealthResult(HealthKind.Ours, null));
    }

    private sealed class UnusedStarter : IProcessStarter
    {
        public IProcessHandle Start(BackendLaunchPlan plan) =>
            throw new InvalidOperationException("the test attaches to a backend, it never starts one");
    }

    private sealed class SilentDialogs : IDialogService
    {
        public bool Confirm(string title, string message) => false;
        public void Info(string title, string message)
        {
        }

        public void Error(string title, string message)
        {
        }

        public string? PickOpenFile(string title, string filter) => null;
        public string? PickSaveFile(string title, string filter, string defaultName) => null;
        public string? AskText(string title, string message, string initial = "") => null;
        public void OpenExternal(string pathOrUrl)
        {
        }

        public void OpenFolder(string directory)
        {
        }
    }
}
