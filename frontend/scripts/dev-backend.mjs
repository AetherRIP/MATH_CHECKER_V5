// Explicit backend selection for the browser development server.
//
// Why this exists: the dev server used to proxy /api to a hardcoded
// http://127.0.0.1:8642. Whatever happened to be listening there - the real
// local backend or a `python main.py --demo` instance - silently became "the
// app", and the browser had no way to tell a demo database from the teacher's
// real one. Backend selection is now configuration, not a guess:
//
//   frontend/.env.local       MC5_DEV_API=http://127.0.0.1:8642
//
// There is NO implicit fallback: a missing or malformed value stops `vite` with
// an explanation instead of connecting to a backend nobody chose.

export const DEV_BACKEND_ENV = "MC5_DEV_API";

/** The value the old hardcoded proxy used. Kept for documentation and tests -
 *  it is never applied implicitly. */
export const DEFAULT_DEV_BACKEND = "http://127.0.0.1:8642";

export class DevBackendConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = "DevBackendConfigError";
  }
}

const HINT = [
  "",
  "Create frontend/.env.local with the backend you mean to use, for example:",
  `  ${DEV_BACKEND_ENV}=${DEFAULT_DEV_BACKEND}    (python main.py 8642)`,
  "  " + `${DEV_BACKEND_ENV}=http://127.0.0.1:8643    (a second, separate install)`,
  "Copy .env.development.example to .env.local to start from a working template.",
  "This server will not guess: connecting the browser to an unchosen backend is",
  "how demo data ends up looking like a real class.",
].join("\n");

/**
 * Validates a configured backend origin.
 *
 * Accepts only an absolute http(s) origin: no path, query, fragment or
 * embedded credentials (a URL with a password would end up in logs and in the
 * client bundle's environment label).
 *
 * @param {string | undefined | null} raw
 * @returns {string} normalised origin, e.g. "http://127.0.0.1:8642"
 */
export function resolveDevBackend(raw) {
  const value = (raw ?? "").trim();
  if (!value) {
    throw new DevBackendConfigError(
      `No development backend is configured (${DEV_BACKEND_ENV} is empty).` + HINT,
    );
  }

  // `new URL("127.0.0.1:8642")` happily parses "127.0.0.1:" as a scheme, so
  // check the prefix before parsing: a bare host:port is a typo, not an origin.
  if (!/^https?:\/\//i.test(value)) {
    throw new DevBackendConfigError(
      `${DEV_BACKEND_ENV}="${value}" must start with http:// or https:// ` +
        `(for example ${DEFAULT_DEV_BACKEND}).` + HINT,
    );
  }

  let url;
  try {
    url = new URL(value);
  } catch {
    throw new DevBackendConfigError(
      `${DEV_BACKEND_ENV}="${value}" is not a URL. It must look like ` +
        `${DEFAULT_DEV_BACKEND}.` + HINT,
    );
  }

  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new DevBackendConfigError(
      `${DEV_BACKEND_ENV}="${value}" must use http:// or https://.` + HINT,
    );
  }
  if (url.username || url.password) {
    throw new DevBackendConfigError(
      `${DEV_BACKEND_ENV} must not contain credentials. Put keys in the ` +
        "backend's own data folder (Settings → AI keys), never in the browser " +
        "configuration.",
    );
  }
  if ((url.pathname && url.pathname !== "/") || url.search || url.hash) {
    throw new DevBackendConfigError(
      `${DEV_BACKEND_ENV}="${value}" must be an origin only, with no path, ` +
        `query or fragment (for example ${DEFAULT_DEV_BACKEND}).`,
    );
  }
  if (!url.hostname) {
    throw new DevBackendConfigError(
      `${DEV_BACKEND_ENV}="${value}" has no host name.` + HINT,
    );
  }

  return url.origin;
}

/** True for a backend on this machine. Anything else is a remote backend and
 *  deserves an extra warning: the browser would be reading real class data
 *  over the network. */
export function isLoopbackOrigin(origin) {
  try {
    const host = new URL(origin).hostname.toLowerCase();
    return (
      host === "localhost" ||
      host === "127.0.0.1" ||
      host === "[::1]" ||
      host === "::1" ||
      host.endsWith(".localhost")
    );
  } catch {
    return false;
  }
}
