using System.Collections.ObjectModel;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class ClassesViewModel : PageViewModel
{
    private ClassDto? _selected;
    private string _summary = string.Empty;

    public ClassesViewModel(AppSession session)
        : base(session, "classes", "Classes")
    {
    }

    public ObservableCollection<ClassDto> Items { get; } = new();

    public ClassDto? Selected
    {
        get => _selected;
        set => SetProperty(ref _selected, value);
    }

    public string Summary
    {
        get => _summary;
        private set => SetProperty(ref _summary, value);
    }

    public AsyncCommand ExportClassReportCommand => Command(async () =>
    {
        if (Selected is null)
        {
            return;
        }

        var file = await Session.Api.ExportClassReportAsync(Selected.Id, "pdf");
        var saved = DownloadSaver.Save(Session.Settings, Session.Dialogs, file,
            "PDF report (*.pdf)|*.pdf", "Save class report", openAfter: true);
        if (!string.IsNullOrEmpty(saved))
        {
            Session.Notify("Class report saved: " + saved);
        }
    });

    public AsyncCommand OpenAssignmentsCommand => new(() =>
    {
        Session.GoTo("assignments");
        return Task.CompletedTask;
    });

    public AsyncCommand OpenStudentsCommand => new(() =>
    {
        Session.GoTo("students");
        return Task.CompletedTask;
    });

    protected override async Task LoadCoreAsync()
    {
        var classes = await Session.Api.ClassesAsync();
        Items.Clear();
        foreach (var item in classes.Classes)
        {
            Items.Add(item);
        }

        Summary = classes.Total == 0
            ? "No classes yet. Connect Google Classroom and sync, or create a local assignment."
            : $"{classes.Total} classes · {classes.Classes.Sum(c => c.Students)} students";
    }
}
