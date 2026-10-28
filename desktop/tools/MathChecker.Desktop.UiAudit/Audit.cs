using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using System.Windows.Automation;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.UiAudit;

internal sealed record Check(string Name, bool Ok, string Detail);

/// <summary>Collects the audit's assertions so one command prints the whole run.</summary>
internal sealed class Audit
{
    public List<Check> Checks { get; } = new();

    public int Failed => Checks.Count(c => !c.Ok);
    public int Passed => Checks.Count(c => c.Ok);

    public void Pass(string name, string detail = "") => Checks.Add(new Check(name, true, detail));

    public void Fail(string name, string detail = "") => Checks.Add(new Check(name, false, detail));

    public void Check(string name, bool ok, string detail) =>
        Checks.Add(new Check(name, ok, detail));
}

/// <summary>
/// Runs work on a real STA thread with a pumping dispatcher.
///
/// A WPF page cannot be exercised without one: await continuations, <c>Progress</c>
/// callbacks and the view models' own UI posts all need the queue drained. No
/// window is created, so nothing appears on the user's desktop.
/// </summary>
internal static class UiHost
{
    public static async Task<T> RunAsync<T>(Func<Task<T>> body)
    {
        var result = default(T);
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            var dispatcher = Dispatcher.CurrentDispatcher;
            var frame = new DispatcherFrame();
            _ = dispatcher.InvokeAsync(async () =>
            {
                try
                {
                    result = await body();
                }
                catch (Exception ex)
                {
                    failure = ex;
                }
                finally
                {
                    frame.Continue = false;
                }
            });
            Dispatcher.PushFrame(frame);
        })
        {
            IsBackground = true,
            Name = "ui-audit",
        };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();

        while (thread.IsAlive)
        {
            await Task.Delay(25);
        }

        if (failure is not null)
        {
            throw failure;
        }

        return result!;
    }

    public static async Task WaitUntilAsync(Func<bool> condition, TimeSpan timeout,
        int settleMs = 0)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (condition())
            {
                if (settleMs > 0)
                {
                    await Task.Delay(settleMs);
                }

                return;
            }

            await Task.Delay(40);
        }
    }
}

/// <summary>
/// The answer the application's dialogs would give, recorded instead of shown.
///
/// A modal dialog cannot be opened here (it would appear on the user's desktop),
/// so this stands in for the four prompts a page can raise. File pickers answer
/// with paths the audit created; confirmations say yes and are recorded, because
/// "did this action ask before overwriting?" is itself worth checking.
/// </summary>
internal sealed class RecordingDialogs : IDialogService
{
    private readonly Queue<string> _openFiles = new();
    private readonly Queue<string> _texts = new();

    public string? SaveDirectory { get; set; }

    public List<string> Confirms { get; } = new();
    public List<string> Infos { get; } = new();
    public List<string> Errors { get; } = new();
    public List<string> OpenedExternal { get; } = new();
    public List<string> SavedFiles { get; } = new();
    public bool ConfirmAnswer { get; set; } = true;

    public void QueueOpenFile(string path) => _openFiles.Enqueue(path);

    public void QueueText(string value) => _texts.Enqueue(value);

    public bool Confirm(string title, string message)
    {
        Confirms.Add(title + " — " + message);
        return ConfirmAnswer;
    }

    public void Info(string title, string message) => Infos.Add(title + " — " + message);

    public void Error(string title, string message) =>
        Errors.Add(title + " — " + message);

    public string? PickOpenFile(string title, string filter) =>
        _openFiles.Count > 0 ? _openFiles.Dequeue() : null;

    public string? PickSaveFile(string title, string filter, string defaultName)
    {
        var directory = SaveDirectory ?? Path.GetTempPath();
        Directory.CreateDirectory(directory);
        var target = Path.Combine(directory, Path.GetFileName(defaultName));
        SavedFiles.Add(target);
        return target;
    }

    public string? AskText(string title, string message, string initial = "") =>
        _texts.Count > 0 ? _texts.Dequeue() : null;

    public void OpenExternal(string pathOrUrl) => OpenedExternal.Add(pathOrUrl);

    public void OpenFolder(string directory) => OpenedExternal.Add(directory);
}

/// <summary>
/// Every WPF data-binding failure, captured instead of written to a debug console.
///
/// This is the check that finds the defect a screenshot hides: a binding to a
/// property that does not exist renders an empty control, throws nothing, and
/// looks like an empty state. A real user sees a blank box next to a label.
/// </summary>
internal sealed class BindingErrors : TraceListener
{
    private readonly List<string> _messages = new();
    private readonly object _gate = new();

    public static BindingErrors Install()
    {
        PresentationTraceSources.Refresh();
        var sink = new BindingErrors();
        var source = PresentationTraceSources.DataBindingSource;
        source.Listeners.Clear();
        source.Listeners.Add(sink);
        source.Switch.Level = SourceLevels.Warning;
        return sink;
    }

    public override void Write(string? message) => Add(message);

    public override void WriteLine(string? message) => Add(message);

    public override void TraceEvent(TraceEventCache? eventCache, string source,
        TraceEventType eventType, int id, string? message) => Add(message);

    public override void TraceEvent(TraceEventCache? eventCache, string source,
        TraceEventType eventType, int id, string format, params object?[]? args)
    {
        try
        {
            Add(args is { Length: > 0 } ? string.Format(format, args) : format);
        }
        catch (FormatException)
        {
            Add(format);
        }
    }

    private void Add(string? message)
    {
        if (string.IsNullOrWhiteSpace(message))
        {
            return;
        }

        lock (_gate)
        {
            _messages.Add(message.Trim());
        }
    }

    /// <summary>The messages since the last call, de-duplicated by text.</summary>
    public IReadOnlyList<string> Take()
    {
        lock (_gate)
        {
            var copy = _messages
                .Select(Condense)
                .Distinct(StringComparer.Ordinal)
                .ToList();
            _messages.Clear();
            return copy;
        }
    }

    /// <summary>A binding error carries the element's whole description; the
    /// path and the offending property are what identify it.</summary>
    private static string Condense(string message)
    {
        var text = message.Replace("\r", " ").Replace("\n", " ");
        while (text.Contains("  ", StringComparison.Ordinal))
        {
            text = text.Replace("  ", " ", StringComparison.Ordinal);
        }

        return text.Length <= 300 ? text : text[..300] + "…";
    }
}

/// <summary>What one page looked like after it was measured, arranged and painted.</summary>
internal sealed class PageRender
{
    public string Key { get; init; } = string.Empty;
    public string Title { get; init; } = string.Empty;
    public double Width { get; init; }
    public double Height { get; init; }
    public bool TemplateResolved { get; init; }
    public string ViewType { get; init; } = string.Empty;
    public double DesiredWidth { get; init; }
    public double DesiredHeight { get; init; }
    public string Text { get; init; } = string.Empty;
    public List<string> ClippedText { get; init; } = new();
    public List<string> PastRightEdge { get; init; } = new();
    public List<string> BindingErrors { get; init; } = new();
    public List<string> CanExecuteFailures { get; init; } = new();
    public List<string> CommandlessButtons { get; init; } = new();
    public List<string> EmptyDropdowns { get; init; } = new();
    public int Buttons { get; init; }
    public int EnabledButtons { get; init; }
    public int Inputs { get; init; }
    public int Dropdowns { get; init; }
    public int Lists { get; init; }
    public int Images { get; init; }
    public bool Focusable { get; init; }
    public List<string> HeaderLayout { get; init; } = new();
}

/// <summary>
/// Paints a page view model into a bitmap through the application's real
/// DataTemplates, exactly as the window's content presenter does.
/// </summary>
internal static class PagePainter
{
    /// <summary>
    /// Builds the page, lays it out at the given size and returns what the user
    /// would see. Nothing is shown: <see cref="RenderTargetBitmap"/> paints the
    /// visual tree directly, so no window, no focus and no taskbar entry.
    /// </summary>
    public static PageRender Paint(PageViewModel page, BindingErrors bindings, int width,
        int height, string? shotPath)
    {
        bindings.Take();

        var host = new ContentControl
        {
            Content = page,
            Width = width,
            Height = height,
            Focusable = false,
        };

        host.Measure(new Size(width, height));
        host.Arrange(new Rect(0, 0, width, height));
        host.UpdateLayout();

        // A page without a DataTemplate renders its ToString() and no view at
        // all, which is why this is checked rather than assumed.
        var view = Visuals.Descendants(host)
            .OfType<FrameworkElement>()
            .FirstOrDefault(element => element.GetType().Namespace == "MathChecker.Desktop.Views");

        var clipped = Visuals.ClippedText(host);
        var overflow = Visuals.PastRightEdge(host, width);
        var buttons = Visuals.Descendants(host).OfType<ButtonBase>().ToList();
        var commandless = new List<string>();
        var canExecute = new List<string>();
        foreach (var button in buttons)
        {
            if (button.Command is null || !button.IsEnabled)
            {
                continue;
            }

            try
            {
                _ = button.Command.CanExecute(button.CommandParameter);
            }
            catch (Exception ex)
            {
                canExecute.Add($"{button.Name}: {ex.GetType().Name} {ex.Message}");
            }

            if (button.Command is null && !string.IsNullOrWhiteSpace(Visuals.Label(button)))
            {
                commandless.Add(Visuals.Label(button));
            }
        }

        var inputs = Visuals.Descendants(host)
            .Count(element => element is TextBox or PasswordBox or RichTextBox);
        var dropdowns = Visuals.Descendants(host).OfType<ComboBox>().ToList();
        var emptyDropdowns = dropdowns
            .Where(combo => combo.IsEnabled && combo.Items.Count == 0)
            .Select(combo => combo.Name.Length == 0 ? "(unnamed)" : combo.Name)
            .ToList();

        if (shotPath is not null)
        {
            Save(host, width, height, shotPath);
        }

        return new PageRender
        {
            Key = page.Key,
            Title = page.Title,
            Width = width,
            Height = height,
            TemplateResolved = view is not null,
            ViewType = view?.GetType().Name ?? "(none)",
            DesiredWidth = host.DesiredSize.Width,
            DesiredHeight = host.DesiredSize.Height,
            Text = Visuals.Text(host),
            ClippedText = clipped,
            PastRightEdge = overflow,
            BindingErrors = bindings.Take().ToList(),
            CanExecuteFailures = canExecute,
            CommandlessButtons = commandless,
            EmptyDropdowns = emptyDropdowns.Distinct().ToList(),
            Buttons = buttons.Count,
            EnabledButtons = buttons.Count(b => b.IsEnabled),
            Inputs = inputs,
            Dropdowns = dropdowns.Count,
            Lists = Visuals.Descendants(host)
                .Count(element => element is DataGrid or ListBox or ItemsControl),
            HeaderLayout = Visuals.HeaderLayout(host),
            Images = Visuals.Descendants(host).OfType<Image>().Count(image =>
                image.Source is not null),
            Focusable = KeyboardNavigation.GetIsTabStop(host)
                       || Visuals.Descendants(host)
                           .OfType<UIElement>()
                           .Any(element => KeyboardNavigation.GetIsTabStop(element)),
        };
    }

    private static void Save(Visual visual, int width, int height, string path)
    {
        var bitmap = new RenderTargetBitmap(width, height, 96, 96, PixelFormats.Pbgra32);
        bitmap.Render(visual);
        var encoder = new PngBitmapEncoder();
        encoder.Frames.Add(BitmapFrame.Create(bitmap));

        var full = Path.GetFullPath(path);
        Directory.CreateDirectory(Path.GetDirectoryName(full)!);
        using var stream = File.Create(full);
        encoder.Save(stream);
    }
}

/// <summary>Reads a rendered visual tree the way a user's eyes would.</summary>
internal static class Visuals
{
    public static IEnumerable<DependencyObject> Descendants(DependencyObject root)
    {
        var count = VisualTreeHelper.GetChildrenCount(root);
        for (var index = 0; index < count; index++)
        {
            var child = VisualTreeHelper.GetChild(root, index);
            yield return child;
            foreach (var nested in Descendants(child))
            {
                yield return nested;
            }
        }
    }

    /// <summary>Every piece of text the page actually paints.</summary>
    public static string Text(DependencyObject root)
    {
        var parts = new List<string>();
        foreach (var element in Descendants(root))
        {
            switch (element)
            {
                case TextBlock { Visibility: Visibility.Visible } block
                    when !string.IsNullOrWhiteSpace(block.Text):
                    parts.Add(block.Text);
                    break;
                case AccessText { Visibility: Visibility.Visible } access
                    when !string.IsNullOrWhiteSpace(access.Text):
                    parts.Add(access.Text);
                    break;
                case TextBox { Visibility: Visibility.Visible } box
                    when !string.IsNullOrWhiteSpace(box.Text):
                    parts.Add(box.Text);
                    break;
                case DataGridColumnHeader { Visibility: Visibility.Visible } header
                    when header.Content is string text && !string.IsNullOrWhiteSpace(text):
                    parts.Add(text);
                    break;
            }
        }

        return string.Join(" ", parts);
    }

    public static string Label(ContentControl control) => control.Content switch
    {
        string text => text,
        TextBlock block => block.Text,
        _ => control.Name,
    };

    /// <summary>
    /// Text that is painted into less width than it needs. A <see cref="TextBlock"/>
    /// that does not wrap and does not trim simply loses the tail — the user sees
    /// a half sentence with no ellipsis and no way to read it.
    /// </summary>
    public static List<string> ClippedText(DependencyObject root)
    {
        var clipped = new List<string>();
        foreach (var block in Descendants(root).OfType<TextBlock>())
        {
            if (block.Visibility != Visibility.Visible
                || string.IsNullOrWhiteSpace(block.Text)
                || block.TextWrapping != TextWrapping.NoWrap
                || block.TextTrimming != TextTrimming.None
                || block.ActualWidth <= 0
                || block.DesiredSize.Width <= 0)
            {
                continue;
            }

            // DesiredSize includes the margin; ActualWidth is the content box.
            // Comparing them directly reports every margin as a clipped tail.
            var needed = block.DesiredSize.Width - block.Margin.Left - block.Margin.Right;
            if (needed > block.ActualWidth + 2)
            {
                clipped.Add($"'{Shorten(block.Text)}' needs {needed:F0} DIP, has {block.ActualWidth:F0}");
            }
        }

        return clipped.Distinct().Take(12).ToList();
    }

    /// <summary>
    /// Elements whose right edge lands outside the page. A page does not scroll
    /// sideways, so anything past the edge is simply lost.
    /// </summary>
    public static List<string> PastRightEdge(DependencyObject root, double width)
    {
        var found = new List<string>();
        Walk(root, false, found);
        return found.Distinct().Take(12).ToList();

        void Walk(DependencyObject node, bool scrolls, List<string> output)
        {
            var count = VisualTreeHelper.GetChildrenCount(node);
            for (var index = 0; index < count; index++)
            {
                var child = VisualTreeHelper.GetChild(node, index);
                var scrollsHere = scrolls
                    || child is ScrollBar
                    || child is DataGrid
                    || (child is ScrollViewer viewer
                        && viewer.HorizontalScrollBarVisibility != ScrollBarVisibility.Disabled
                        && viewer.HorizontalScrollBarVisibility != ScrollBarVisibility.Hidden);

                if (child is FrameworkElement { ActualWidth: > 0 } element && !scrollsHere
                    && IsPainted(element))
                {
                    // The visual offset is the layout slot, which sits inside the
                    // margin, so the right margin is part of what hangs over.
                    var right = element.TransformToAncestor((Visual)root)
                        .Transform(new Point(element.ActualWidth + element.Margin.Right, 0)).X;
                    if (right > width + 0.5)
                    {
                        var parent = VisualTreeHelper.GetParent(element) as FrameworkElement;
                        output.Add(string.Format(
                            "{0} '{1}' ends {2:F0} (w={3:F0}) inside {4} (w={5:F0})",
                            element.GetType().Name, element.Name, right, element.ActualWidth,
                            parent?.GetType().Name ?? "-", parent?.ActualWidth ?? 0));
                    }
                }

                Walk(child, scrollsHere, output);
            }
        }
    }

    /// <summary>
    /// The page header's own arithmetic, printed when a page overflows: which
    /// child got which width is the difference between a squeezed title and a
    /// button strip laid out past the right edge.
    /// </summary>
    public static List<string> HeaderLayout(DependencyObject root)
    {
        var lines = new List<string>();
        var panel = Descendants(root)
            .OfType<MathChecker.Desktop.Core.UI.HeaderPanel>()
            .FirstOrDefault();
        if (panel is null)
        {
            return lines;
        }

        lines.Add($"HeaderPanel actual={panel.ActualWidth:F0} desired={panel.DesiredSize.Width:F0} "
                  + $"children={panel.Children.Count}");
        Walk(panel, 1);
        return lines;

        void Walk(DependencyObject node, int depth)
        {
            if (depth > 6)
            {
                return;
            }

            foreach (var child in Descendants(node).OfType<FrameworkElement>())
            {
                if (VisualTreeHelper.GetParent(child) != node)
                {
                    continue;
                }

                var offset = child.TransformToAncestor(panel).Transform(new Point(0, 0)).X;
                lines.Add($"{new string(' ', depth * 2)}{child.GetType().Name} '{child.Name}' "
                          + $"x={offset:F0} desired={child.DesiredSize.Width:F0} "
                          + $"actual={child.ActualWidth:F0} {child.Visibility}");
                Walk(child, depth + 1);
            }
        }
    }

    /// <summary>
    /// Is this element part of what the user sees? A collapsed branch keeps the
    /// sizes its children got from the last layout pass, so position alone would
    /// report every hidden alternative as hanging off the page.
    /// <see cref="UIElement.IsVisible"/> is no use here: it is false for any
    /// tree that was never attached to a window, which is every page painted
    /// offscreen.
    /// </summary>
    private static bool IsPainted(DependencyObject node)
    {
        for (var current = node; current is not null; current = VisualTreeHelper.GetParent(current))
        {
            if (current is UIElement { Visibility: not Visibility.Visible })
            {
                return false;
            }
        }

        return true;
    }

    private static string Shorten(string text) =>
        text.Length <= 40 ? text : text[..40] + "…";
}
