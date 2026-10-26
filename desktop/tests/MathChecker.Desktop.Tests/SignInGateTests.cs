using System.Text.Json;
using MathChecker.Desktop.Models;
using MathChecker.Desktop.ViewModels;

namespace MathChecker.Desktop.Tests;

/// <summary>
/// The first launch of an installed copy has no account yet, so it must go
/// straight to setup — not to a sign-in dialog that cannot be satisfied.
///
/// The backend already distinguishes the two cases in /auth/status:
/// ``login_required`` is the local setting (true by default), while
/// ``required`` is false when there is nothing to sign in with. The desktop
/// used to gate on the setting, which locked a fresh installation behind an
/// impossible password.
/// </summary>
public class SignInGateTests
{
    private static AuthStatusDto Parse(string json) =>
        JsonSerializer.Deserialize<AuthStatusDto>(json, Json.Options)!;

    [Fact]
    public void FreshInstall_goes_to_setup_instead_of_sign_in()
    {
        var auth = Parse("""
            {"public_mode": false, "required": false, "login_required": true,
             "configured": false, "authenticated": false}
            """);

        Assert.True(auth.LoginRequired, "the local setting is still on, as designed");
        Assert.False(auth.Required);
        Assert.False(MainViewModel.RequiresSignIn(auth));
    }

    [Fact]
    public void Existing_account_still_asks_for_a_password()
    {
        var auth = Parse("""
            {"public_mode": false, "required": true, "login_required": true,
             "configured": true, "authenticated": false, "username": "teacher"}
            """);

        Assert.True(MainViewModel.RequiresSignIn(auth));
        Assert.Equal("teacher", auth.User);
    }

    [Fact]
    public void Signed_in_session_never_prompts()
    {
        var auth = Parse("""
            {"public_mode": false, "required": true, "login_required": true,
             "configured": true, "authenticated": true, "username": "teacher",
             "role": "teacher"}
            """);

        Assert.False(MainViewModel.RequiresSignIn(auth));
    }

    [Fact]
    public void Public_mode_with_an_account_prompts()
    {
        var auth = Parse("""
            {"public_mode": true, "required": true, "login_required": false,
             "configured": true, "authenticated": false}
            """);

        Assert.True(MainViewModel.RequiresSignIn(auth));
    }

    [Fact]
    public void Older_backend_without_required_falls_back_to_the_account_check()
    {
        // No "required" key: only prompt when the local requirement is on AND
        // there is something to authenticate against.
        var withAccount = Parse("""
            {"login_required": true, "configured": true, "authenticated": false}
            """);
        var withoutAccount = Parse("""
            {"login_required": true, "configured": false, "authenticated": false}
            """);

        Assert.Null(withAccount.Required);
        Assert.True(MainViewModel.RequiresSignIn(withAccount));
        Assert.False(MainViewModel.RequiresSignIn(withoutAccount));
    }
}
