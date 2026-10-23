import { useEffect, useState } from "react";
import {
  api, authStatus, setCsrfToken, type AuthStatus,
} from "./api";
import { Button, Field, Icon } from "./components";

/**
 * The MAIN sign-in page for Math Checker.
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
      : "Sign in with your Math Checker account to open the desk.";

  return (
    <main className="signin-wrap">
      <div className="signin-card">
        <div className="brand">
          <div className="brand-badge" aria-hidden="true">
            <Icon name="sparkles" size={18} />
          </div>
          <div>
            <span className="brand-kicker">Maths · Assessment</span>
            Math Checker
            <small>Marking desk</small>
          </div>
        </div>

        <div>
          <p className="page-kicker">
            {needsSetup ? "First run" : "Marking desk"}
          </p>
          <h1 className="page-title" style={{ fontSize: "var(--fs-2xl)" }}>
            {needsSetup ? "Create the first account" : "Sign in"}
          </h1>
          <p className="muted small" style={{ margin: "4px 0 0" }}>{intro}</p>
        </div>

        {remoteNoSetup ? (
          <div className="notice warn">
            <Icon name="alert" size={16} />
            <span>
              <strong>No sign-in account exists yet.</strong> Open Math Checker
              on the laptop running it ({status?.local_url ?? "http://127.0.0.1:8642"})
              once and create the account there — account setup is deliberately
              blocked through the public address.
            </span>
          </div>
        ) : (
          <form onSubmit={submit} className="stack" style={{ gap: 12 }}>
            <Field label="Username" id="signin-username">
              <input
                id="signin-username"
                className="input"
                value={username}
                autoComplete="username"
                autoFocus
                onChange={(e) => { setUsername(e.target.value); setError(null); }}
                placeholder={needsSetup ? "teacher" : "your username"}
                required
              />
            </Field>

            <Field
              label={needsSetup ? "Choose a password (10+ characters)" : "Password"}
              id="signin-password"
            >
              <div className="row" style={{ gap: 6 }}>
                <input
                  id="signin-password"
                  className="input"
                  type={reveal ? "text" : "password"}
                  value={password}
                  autoComplete={needsSetup ? "new-password" : "current-password"}
                  onChange={(e) => { setPassword(e.target.value); setError(null); }}
                  minLength={needsSetup ? 10 : undefined}
                  aria-describedby="signin-password-hint"
                  required
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-pressed={reveal}
                  onClick={() => setReveal((v) => !v)}
                >
                  <Icon name={reveal ? "lock" : "eye"} size={14} />
                  {reveal ? "Hide" : "Show"}
                </Button>
              </div>
            </Field>

            {needsSetup && (
              <Field label="Confirm password" id="signin-confirm">
                <input
                  id="signin-confirm"
                  className="input"
                  type="password"
                  value={current}
                  autoComplete="new-password"
                  onChange={(e) => { setCurrent(e.target.value); setError(null); }}
                  minLength={10}
                  required
                  pattern={password === current ? undefined : ".*"}
                  onInvalid={(e) => {
                    if (current !== password) {
                      (e.currentTarget as HTMLInputElement).setCustomValidity("Passwords do not match.");
                    }
                  }}
                  onInput={(e) => (e.currentTarget as HTMLInputElement).setCustomValidity("")}
                />
              </Field>
            )}

            {!needsSetup && (
              <label className="row small" style={{ gap: 8, cursor: "pointer" }}>
                <input type="checkbox" checked={remember}
                  onChange={(e) => setRemember(e.target.checked)} />
                <span>Remember me on this device</span>
              </label>
            )}

            {error && (
              <div className="notice err" role="alert">
                <Icon name="alert" size={15} />
                <span>{error}</span>
              </div>
            )}

            <Button type="submit" variant="primary" block loading={busy} icon="shield">
              {needsSetup ? "Create account & sign in" : "Sign in"}
            </Button>

            <p id="signin-password-hint" className="faint small" style={{ margin: 0, textAlign: "center" }}>
              {needsSetup
                ? "Passwords are stored only as salted hashes on this machine."
                : "Your session is remembered until you sign out"
                  + (remember ? " — even after a restart." : " for this browser session only.")}
            </p>
          </form>
        )}

        {status?.public_url && (
          <div className="signin-address">
            <span className="faint small">Public address</span>
            <div className="row" style={{ gap: 8 }}>
              <code style={{ wordBreak: "break-all", flex: 1 }}>{status.public_url}</code>
              <Button variant="ghost" size="sm" onClick={copyAddress}
                icon={copied ? "check" : "copy"}>
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
          </div>
        )}
      </div>

      <aside className="signin-aside" aria-label="About Math Checker">
        <p className="page-kicker">Chalk &amp; ink, on paper</p>
        <h2>A marking desk that keeps the evidence.</h2>
        <ul>
          <li>
            <Icon name="shield" size={16} />
            <span><strong>Private by default</strong>
              Papers, marks and keys stay on the machine running the app.</span>
          </li>
          <li>
            <Icon name="review" size={16} />
            <span><strong>Every mark checked</strong>
              Independent verification runs before a mark is ever saved.</span>
          </li>
          <li>
            <Icon name="check-circle" size={16} />
            <span><strong>You decide</strong>
              Anything uncertain waits here until a teacher signs it off.</span>
          </li>
        </ul>
        <p className="faint small" style={{ margin: 0 }}>
          Offline-first. No account data leaves the installation.
        </p>
      </aside>
    </main>
  );
}
