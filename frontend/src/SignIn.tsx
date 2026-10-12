import { useEffect, useState } from "react";
import {
  api, authStatus, setCsrfToken, type AuthStatus,
} from "./api";
import { Button, Icon } from "./components";

/**
 * The MAIN sign-in page for Math Correct.
 *
 * Rendered by the shell whenever the backend says a session is required —
 * public (tunnel) mode OR the local `login_required` setting. Credentials are
 * verified by the backend against the Multi-School accounts (the shared
 * multi-user system) with the legacy single-teacher account as a fallback;
 * nothing is trusted client-side. The session itself is a signed HttpOnly
 * cookie, so "Remember me" only changes how long that cookie lives — the
 * password is never stored anywhere in the browser.
 */
export default function SignIn({
  status: initial,
  onSignedIn,
}: {
  status: AuthStatus | null;
  onSignedIn: (status: AuthStatus) => void;
}) {
  const [status, setStatus] = useState<AuthStatus | null>(initial);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [current, setCurrent] = useState("");
  const [remember, setRemember] = useState(true);
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!status) {
      authStatus().then(setStatus).catch(() => {});
    }
  }, [status]);

  const needsSetup = Boolean(status && !status.configured);
  const remoteNoSetup = needsSetup && status?.local_request === false;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (needsSetup) {
        await api("/auth/setup", {
          method: "POST",
          body: JSON.stringify({ username: username || "teacher", password }),
        });
        // Sign in immediately after creating the account so the flow is one
        // continuous step for the teacher.
        await api("/auth/login", {
          method: "POST",
          body: JSON.stringify({
            username: username || "teacher", password, remember_me: remember,
          }),
        });
      } else {
        await api("/auth/login", {
          method: "POST",
          body: JSON.stringify({ username, password, remember_me: remember }),
        });
      }
      const fresh = await authStatus();
      setCsrfToken(fresh.csrf);
      setStatus(fresh);
      if (fresh.authenticated) onSignedIn(fresh);
      else setError("Sign-in did not complete. Try again.");
    } catch (err) {
      setError((err as Error).message);
      // Never leave stale credentials sitting behind a failed attempt.
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  function copyAddress() {
    if (!status?.public_url) return;
    void navigator.clipboard?.writeText(status.public_url).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    });
  }

  const intro = needsSetup
    ? "Create the first account for this installation. You will sign in with it every time."
    : status?.public_mode
      ? "Public access is enabled, so this session requires an account."
      : "Sign in with your Math Correct account to open the dashboard.";

  return (
    <div className="signin-wrap">
      <div className="card signin-card">
        <div className="brand" style={{ justifyContent: "center", marginBottom: 8 }}>
          <div className="brand-badge" aria-hidden="true">
            <Icon name="sparkles" size={18} />
          </div>
          <div>
            Math Correct
            <small>Sign in to continue</small>
          </div>
        </div>

        <p className="faint small" style={{ textAlign: "center", marginTop: 0 }}>
          {intro}
        </p>

        {remoteNoSetup ? (
          <div className="notice warn">
            <Icon name="alert" size={16} />
            <span>
              <strong>No sign-in account exists yet.</strong> Open Math Correct
              on the laptop running it ({status?.local_url ?? "http://127.0.0.1:8642"})
              once and create the account there — account setup is deliberately
              blocked through the public address.
            </span>
          </div>
        ) : (
          <form onSubmit={submit} style={{ display: "grid", gap: 12 }}>
            <label className="field">
              <span>Username</span>
              <input className="input" value={username} autoComplete="username"
                onChange={(e) => { setUsername(e.target.value); setError(null); }}
                placeholder={needsSetup ? "teacher" : "your username"} required />
            </label>
            <label className="field">
              <span>{needsSetup ? "Choose a password (10+ characters)" : "Password"}</span>
              <div style={{ position: "relative" }}>
                <input className="input" type={reveal ? "text" : "password"}
                  value={password}
                  autoComplete={needsSetup ? "new-password" : "current-password"}
                  onChange={(e) => { setPassword(e.target.value); setError(null); }}
                  minLength={needsSetup ? 10 : undefined} required
                  style={{ paddingRight: 74 }} />
                <button type="button"
                  className="btn ghost size-sm"
                  onClick={() => setReveal((v) => !v)}
                  aria-label={reveal ? "Hide password" : "Show password"}
                  style={{
                    position: "absolute", right: 2, top: "50%",
                    transform: "translateY(-50%)", fontSize: 12, padding: "4px 8px",
                  }}>
                  <Icon name={reveal ? "shield" : "user"} size={14} />
                  {reveal ? "Hide" : "Show"}
                </button>
              </div>
            </label>
            {needsSetup && (
              <label className="field">
                <span>Confirm password</span>
                <input className="input" type="password" value={current}
                  autoComplete="new-password"
                  onChange={(e) => { setCurrent(e.target.value); setError(null); }}
                  minLength={10} required
                  pattern={password === current ? undefined : ".*"}
                  onInvalid={(e) => {
                    if (current !== password) {
                      (e.currentTarget as HTMLInputElement).setCustomValidity("Passwords do not match.");
                    }
                  }}
                  onInput={(e) => (e.currentTarget as HTMLInputElement).setCustomValidity("")} />
              </label>
            )}
            {!needsSetup && (
              <label className="row" style={{ gap: 8, alignItems: "center", cursor: "pointer" }}>
                <input type="checkbox" checked={remember}
                  onChange={(e) => setRemember(e.target.checked)} />
                <span className="small">Remember me on this device</span>
              </label>
            )}
            {error && (
              <div className="notice err" role="alert"
                onClick={() => setError(null)} style={{ cursor: "pointer" }}>
                {error}
              </div>
            )}
            <Button type="submit" variant="primary" disabled={busy}
              icon={busy ? "refresh" : "shield"}>
              {busy ? "Please wait…" : needsSetup ? "Create account & sign in" : "Sign in"}
            </Button>
            <p className="faint small" style={{ margin: 0, textAlign: "center" }}>
              {needsSetup
                ? "Passwords are stored only as salted hashes on this machine."
                : "Your session is remembered until you sign out" +
                  (remember ? " — even after a restart." : " for this browser session only.")}
            </p>
          </form>
        )}

        {status?.public_url && (
          <div className="signin-address">
            <span className="faint small">Public address</span>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <code style={{ wordBreak: "break-all" }}>{status.public_url}</code>
              <Button variant="ghost" size="sm" onClick={copyAddress}
                icon={copied ? "check" : "copy"}>
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
