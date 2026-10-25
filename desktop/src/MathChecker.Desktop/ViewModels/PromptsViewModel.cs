using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.Api;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

public sealed class PromptsViewModel : PageViewModel
{
    private string _systemPrompt = string.Empty;
    private string _userPrompt = string.Empty;
    private string _info = string.Empty;
    private bool _isAvailable = true;

    /// <summary>False when the backend closes prompt overrides to this role.
    /// That is a state, not a failure: the editors are read-only and the page
    /// says why, instead of raising a red error a teacher cannot act on.</summary>
    public bool IsAvailable
    {
        get => _isAvailable;
        private set
        {
            if (SetProperty(ref _isAvailable, value))
            {
                OnPropertyChanged(nameof(IsUnavailable));
            }
        }
    }

    public bool IsUnavailable => !IsAvailable;

    public string UnavailableText =>
        "Prompt overrides are open to the developer role only. "
        + "Corrections keep using the built-in prompts, and every other page works as usual.";

    public PromptsViewModel(AppSession session)
        : base(session, "prompts", "Prompts")
    {
    }

    public string SystemPrompt
    {
        get => _systemPrompt;
        set => SetProperty(ref _systemPrompt, value);
    }

    public string UserPrompt
    {
        get => _userPrompt;
        set => SetProperty(ref _userPrompt, value);
    }

    public string Info
    {
        get => _info;
        private set => SetProperty(ref _info, value);
    }

    public AsyncCommand SaveCommand => Command(async () =>
    {
        await Session.Api.SavePromptsAsync(SystemPrompt, UserPrompt);
        await LoadAsync();
        Status = "Prompt overrides saved (an empty box restores the built-in prompt).";
    });

    public AsyncCommand ClearCommand => Command(async () =>
    {
        if (!Session.Dialogs.Confirm("Clear prompt overrides",
                "Restore the built-in legacy prompts? This removes both overrides."))
        {
            return;
        }

        await Session.Api.SavePromptsAsync(string.Empty, string.Empty);
        await LoadAsync();
    });

    protected override async Task LoadCoreAsync()
    {
        try
        {
            var payload = await Session.Api.PromptsAsync();
            SystemPrompt = Text(payload, "system_prompt_override") ?? string.Empty;
            UserPrompt = Text(payload, "user_prompt_override") ?? string.Empty;
            IsAvailable = true;
            Info = "Editing these overrides affects every correction, manual and automatic: the worker "
                   + "and the automation scheduler read the same text. Leave a box empty to use the "
                   + "built-in prompt.";
            Status = "Prompt overrides are editable on this account.";
        }
        catch (ApiException ex) when (ex.IsForbidden || ex.IsUnauthorized)
        {
            SystemPrompt = string.Empty;
            UserPrompt = string.Empty;
            IsAvailable = false;
            Info = string.Empty;
            Status = "Prompt overrides are read-only on this account.";
        }
    }

    private static string? Text(System.Text.Json.JsonElement element, string property) =>
        element.TryGetProperty(property, out var value) && value.ValueKind == System.Text.Json.JsonValueKind.String
            ? value.GetString()
            : null;
}
