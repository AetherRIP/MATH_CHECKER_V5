using System.Collections.ObjectModel;
using MathChecker.Desktop.Core;
using MathChecker.Desktop.Core.UI;
using MathChecker.Desktop.Models;

namespace MathChecker.Desktop.ViewModels;

/// <summary>
/// Peer networking: one switch, and one honest account of what the backend did
/// with it.
///
/// Two habits keep this page from lying. Every value it shows comes from a
/// backend answer — there is no local optimism, so a click never paints
/// "running" before the supervisor says so. And the stored preference is shown
/// apart from the observed state, because a preference of On is exactly what a
/// stopped service also has.
/// </summary>
public sealed class PeerViewModel : PageViewModel
{
    private bool _enabled;
    private string _state = "DISABLED";
    private string _label = "Peer networking off";
    private string _detail = string.Empty;
    private bool _supervised;
    private bool _credentialsPresent;
    private bool _corrupt;
    private string? _storeDetail;
    private bool? _encryptedStore;
    private int _restarts;
    private int? _pid;

    public PeerViewModel(AppSession session)
        : base(session, "peer", "Peer network")
    {
    }

    /// <summary>The STORED preference. Not a claim that anything is running.</summary>
    public bool Enabled
    {
        get => _enabled;
        private set
        {
            if (SetProperty(ref _enabled, value))
            {
                OnPropertyChanged(nameof(PreferenceText));
            }
        }
    }

    public string PreferenceText => Enabled ? "On" : "Off";

    /// <summary>The observed state token from the backend ("RUNNING", "STOPPED", …).</summary>
    public string State
    {
        get => _state;
        private set => SetProperty(ref _state, value);
    }

    public string Label
    {
        get => _label;
        private set => SetProperty(ref _label, value);
    }

    public string Detail
    {
        get => _detail;
        private set => SetProperty(ref _detail, value);
    }

    /// <summary>True when the backend process answering this page owns a
    /// supervisor. False with <see cref="Enabled"/> true is "switched on, not
    /// started" and is displayed as such.</summary>
    public bool Supervised
    {
        get => _supervised;
        private set => SetProperty(ref _supervised, value);
    }

    public bool CredentialsPresent
    {
        get => _credentialsPresent;
        private set
        {
            if (SetProperty(ref _credentialsPresent, value))
            {
                OnPropertyChanged(nameof(CredentialsText));
            }
        }
    }

    public string CredentialsText => CredentialsPresent
        ? "This device holds peer credentials."
        : "This device has no peer invitation yet. Enabling will report that it could not start.";

    public bool Corrupt
    {
        get => _corrupt;
        private set => SetProperty(ref _corrupt, value);
    }

    public string StoreText => _encryptedStore switch
    {
        true => "Encrypted with Windows DPAPI.",
        false => _storeDetail ?? "Stored WITHOUT encryption.",
        null => _storeDetail ?? "Not reported.",
    };

    public string ProcessText => Supervised && _pid is { } pid
        ? $"Supervised process · pid {pid} · {_restarts} restart(s)"
        : Supervised ? $"Supervised · {_restarts} restart(s)" : "No process is supervised by this backend.";

    /// <summary>Safe diagnostics shown as labelled rows: state, store and
    /// process facts only — never a key, a token or a credential body.</summary>
    public ObservableCollection<FactRow> Facts { get; } = new();

    // Not CommandWithReload: that writes its success text AFTER the reload, which
    // would replace the explanation of a start that failed with "Peer service
    // started". The observation decides the status line, so the command does.
    public AsyncCommand EnableCommand => Command(() => SetEnabledAsync(true), after: LoadAsync);

    public AsyncCommand DisableCommand => Command(() => SetEnabledAsync(false), after: LoadAsync);

    private async Task SetEnabledAsync(bool enable)
    {
        var result = await Session.Api.SetPeerEnabledAsync(enable);
        Apply(result);

        // The status line follows the observation, never the request.
        Status = result.Confirmed == true
            ? (enable ? "Peer service started." : "Peer service stopped.")
            : (string.IsNullOrWhiteSpace(result.Detail) ? result.Label : result.Detail);
    }

    protected override async Task LoadCoreAsync()
    {
        Apply(await Session.Api.PeerStatusAsync());
    }

    private void Apply(PeerStatusDto status)
    {
        Enabled = status.Enabled;
        State = status.State ?? "UNKNOWN";
        Label = status.Label ?? "Peer status unknown";
        Detail = status.Detail ?? string.Empty;
        Supervised = status.Supervised;
        CredentialsPresent = status.CredentialsPresent;
        Corrupt = status.Corrupt;
        _encryptedStore = status.Service?.EncryptedStore;
        _storeDetail = status.Service?.StoreDetail;
        _restarts = status.Service?.Restarts ?? 0;
        _pid = status.Service?.Pid;

        OnPropertyChanged(nameof(StoreText));
        OnPropertyChanged(nameof(ProcessText));

        Facts.Clear();
        Facts.Add(new FactRow("Stored preference", PreferenceText));
        Facts.Add(new FactRow("Observed state", State));
        Facts.Add(new FactRow("Backend reports", Label));
        Facts.Add(new FactRow("Supervisor", Supervised ? "In this backend process" : "None"));
        Facts.Add(new FactRow("Credentials", CredentialsPresent ? "Present" : "Not invited"));
        Facts.Add(new FactRow("Secure store", StoreText));
        Facts.Add(new FactRow("Process", ProcessText));
    }
}
