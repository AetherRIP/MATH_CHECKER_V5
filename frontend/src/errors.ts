/**
 * Plain-English errors (blueprint §12).
 *
 * The backend records exactly what went wrong, which is right for diagnosis
 * and wrong for a teacher's attention feed: "PERMISSION_DENIED {'error':
 * {'code': 403, 'message': 'You do not have permission to access the File…"
 * is not a sentence anyone can act on. Everything here maps a known failure
 * to what it means and what to do, keeps the original text one click away,
 * and never invents a success.
 */

export interface FriendlyError {
  /** What happened, in the teacher's terms. */
  summary: string;
  /** The next action, when there is one. */
  action?: string;
  /** The untouched original, for diagnostics and the developer surface. */
  raw: string;
}

const RULES: { test: RegExp; summary: string; action?: string }[] = [
  {
    test: /PERMISSION_DENIED|does not have permission to access the File|\b403\b/i,
    summary: "The AI service was not allowed to open this file.",
    action: "Check that the file is shared with the service account, then retry.",
  },
  {
    test: /DEADLINE_EXCEEDED|Deadline expired|\b504\b/i,
    summary: "The AI service ran out of time on this paper.",
    action: "Retry the correction; a long paper sometimes needs a second attempt.",
  },
  {
    test: /RESOURCE_EXHAUSTED|quota|rate limit|\b429\b/i,
    summary: "The AI service's rate limit was reached.",
    action: "Wait a few minutes, or add another key in AI & keys.",
  },
  {
    test: /UNAUTHENTICATED|API key not valid|\b401\b|invalid.*key/i,
    summary: "An AI key was rejected.",
    action: "Replace the failing key in AI & keys.",
  },
  {
    test: /no Google Drive attachment|no attachment/i,
    summary: "This submission has no file attached in Google Classroom.",
    action: "Ask the student to attach their work, or mark it another way.",
  },
  {
    test: /Failed to fetch|NetworkError|Load failed|ERR_NETWORK/i,
    summary: "The app lost its connection to the local server.",
    action: "It reconnects by itself; if this stays, restart Math Checker.",
  },
  {
    test: /PDF.*(corrupt|damaged|unreadable)|cannot open.*pdf/i,
    summary: "This PDF could not be read.",
    action: "Open the file to confirm it is intact, then upload it again.",
  },
  {
    test: /mark scheme/i,
    summary: "The mark scheme could not be matched to this paper.",
    action: "Open Review to attach the right scheme.",
  },
];

/** Lower-case, single-line, no payload dumps. */
function firstLine(text: string): string {
  const line = text.split(/\r?\n/)[0].trim();
  return line.length > 220 ? `${line.slice(0, 217)}…` : line;
}

export function friendlyError(error: unknown): FriendlyError {
  const raw = error instanceof Error ? error.message
    : typeof error === "string" ? error
      : error && typeof error === "object" && "message" in error
        ? String((error as { message: unknown }).message)
        : String(error ?? "Unknown error");

  const rule = RULES.find((r) => r.test.test(raw));
  if (!rule) {
    // Unknown failures still get a readable sentence rather than a code
    // dump: the payload is kept in `raw`, and the first line is de-noised.
    const line = firstLine(raw.replace(/\s*\{.*$/s, "").replace(/\s+/g, " "));
    return { summary: line || "Something went wrong.", raw };
  }
  return { summary: rule.summary, action: rule.action, raw };
}

/** "Summary. Action." for one-line surfaces (toasts, table cells). */
export function friendlyText(error: unknown): string {
  const { summary, action } = friendlyError(error);
  return action ? `${summary} ${action}` : summary;
}
