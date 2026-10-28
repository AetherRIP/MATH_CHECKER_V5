# Skill infrastructure audit — MATH_CHECKER_V5

Date: 2026-10-27. Scope: every skill under `.agents/skills/`, the Freebuff skill
loader, and the graphify graph. This document is the record referenced by the
"Freebuff adaptation" notes in the copied skills and by `AGENTS.md`.

No production code was changed by this audit, and nothing was committed.

## 1. How Freebuff loads a skill (measured, not assumed)

- Discovery is by directory name: `<project>/.agents/skills/<name>/SKILL.md`.
  No plugin, marketplace, manifest or Claude Code config is consulted.
- The skill menu is built once at session start. A skill added mid-session is
  absent from that menu but still loadable by name — verified for all 18.
- Loading returns the file **verbatim**, prefixed with
  `Base directory for this skill: "<abs path>/"`.
- There is **no** `${VAR}` substitution, no `!`-command expansion, no `@file`
  include, no subagent dispatch, and no `Workflow` tool.

## 2. Skills and status

| Skill | Status | Evidence |
|---|---|---|
| brainstorming | PASS | copied from the verified source; loads; drives the spike/bounded/architectural gate |
| writing-plans | PASS WITH LIMITATIONS | loads; produced a plan that passed its own header/step self-review; its hand-off names skills that now all resolve |
| executing-plans | PASS | `scripts/task-start` + `scripts/task-done` ran for real against a plan in a disposable repo: brief written, ledger line `Task 1: complete (commits …→…, tests: … → 5 passed)` |
| subagent-driven-development | PASS WITH LIMITATIONS | installed as a **dependency** of executing-plans (its `scripts/` are what executing-plans calls); its own per-task subagent dispatch is impossible here and its decision diagram redirects to executing-plans when no subagent tool exists |
| receiving-code-review | PASS | applied to a real finding (tracked build artifacts); response pattern used, no performative agreement |
| requesting-code-review | PASS WITH LIMITATIONS | loads; the reviewer-subagent dispatch it mandates has no equivalent here — applied `code-reviewer.md` as a documented self-review instead |
| systematic-debugging | PASS | seeded punctuation bug reproduced → root cause traced to `slugify()` never stripping punctuation → fixed at the cause, regression test green |
| test-driven-development | PASS | failed first (`ImportError: cannot import name 'truncate_slug'`), then implemented, then green — red→green observed |
| verification-before-completion | PASS | applied to this audit: every claim here is backed by a command run in-session |
| using-git-worktrees | PASS | Step 0 detection distinguished a normal checkout from a linked worktree; the `.worktrees/` ignore gate and `git worktree add` verified in a disposable repo (never in this repo) |
| using-superpowers | PASS | loads; `references/freebuff-tools.md` added so its platform-adaptation contract has an entry for this harness |
| finishing-a-development-branch | PASS | loads; installed so executing-plans' terminal step resolves |
| claude-md-improver | PASS | rubric applied to `AGENTS.md` |
| claude-security | FAIL — unsupported runtime | its scan jobs run **only** as a `claude-security:scan` workflow and apply no manual substitute; no `Workflow` tool exists here, so no scan was run. The checklist was applied manually instead (§5) |
| frontend-design | PASS | produced its required design-planning artifact |
| skill-creator | PASS WITH LIMITATIONS | `quick_validate.py` validates all 18 skills after the encoding fix; its eval loop needs `claude -p` |
| skill-development | PASS | guidance applied to the repairs |

Not installed anywhere in this project: `writing-skills` (referenced once,
advisory, from `test-driven-development/writing-good-tests.md`) — deliberately
not added because `skill-development` and `skill-creator` already fill that role.

## 3. Source of the Superpowers skills

Verified source: `.claude/plugins/cache/anthropic-plugin-directory/superpowers/6.4.2-8ca22dba9a94`,
from `github.com/obra/superpowers`, plugin version 6.4.2, commit `8ca22dba9a94`
(recorded in `~/.claude/plugins/installed_plugins.json` for project
`C:\Projects\MATH_CHECKER_V5`).

Copied into `.agents/skills/` (byte-identical, then adapted):
`brainstorming`, `executing-plans`, `receiving-code-review`,
`using-git-worktrees`, `using-superpowers` — plus `subagent-driven-development`
and `finishing-a-development-branch`, which `executing-plans` references and
depends on (without the former's `scripts/sdd-workspace`, `task-brief` and
`review-package`, `executing-plans` cannot run at all).

Five skills already installed here (`requesting-code-review`,
`systematic-debugging`, `test-driven-development`,
`verification-before-completion`, `writing-plans`) are the **same version** with
extra local portability fixes (e.g. `awk` → `cut`, a `${IDENTITY:+SET}` form
that works on every shell), so they were **not** overwritten. Comparing first is
what prevented that loss.

### Adaptations applied to the copies

1. `superpowers:<name>` → `<name>` wherever the target is installed (Freebuff
   resolves bare names; 7 references rewritten, 0 left unresolved).
2. `skills/brainstorming/visual-companion.md` → `visual-companion.md`
   (the skill's own directory, not the plugin root).
3. A "Freebuff adaptation" note after the frontmatter recording source, version
   and commit so the copy is traceable.
4. `using-superpowers/references/freebuff-tools.md` added, and listed in the
   skill's Platform Adaptation section.

## 4. Other repairs

| File | Change | Why it was broken |
|---|---|---|
| `skill-creator/scripts/quick_validate.py` | `read_text(encoding="utf-8")`; `version` allowed in frontmatter | crashed `UnicodeDecodeError: 'charmap'` on any non-ASCII `SKILL.md` (Windows default codec); rejected a key `skill-development` recommends |
| 6 further `skill-creator` scripts | same encoding fix (14 sites) | identical latent crash |
| `claude-md-improver/SKILL.md` | `tools:` → `allowed-tools:` (YAML list) | key the bundled validator rejects |
| `claude-security/SKILL.md` | `${CLAUDE_SKILL_DIR}` → `.agents/skills/claude-security`; dead `${CLAUDE_PLUGIN_ROOT}` lines dropped with a note; `@…/role.md` → "Read `…/role.md`" | the loader does not substitute variables or expand includes, so every path was dead |
| `systematic-debugging/SKILL.md` | parenthetical that had become circular after the namespace rewrite removed | my own earlier edit |
| `AGENTS.md` | corrected the hooks claim (`/graphify` is not a subcommand; no hooks are installed), quantified the graph, added the `.graphifyignore` and `## skills` sections | those claims did not match the repository |
| `.graphifyignore` (new) | exclude the committed vendored Python runtime and build output | ~3000 of ~3300 tracked `.py` files are vendored `site-packages`; they would have been most of the graph |
| `.gitignore` | ignore `graphify-out/` scratch (cache, manifest, `.graphify_*`, `memory/`, `*.html`) | 26 MB of regenerable cache; keeps only `graph.json` + `GRAPH_REPORT.md` committable, per `AGENTS.md` |
| `AGENTS.md` graphify bullet | documents `uv tool update-shell` and keeps the absolute-path fallback | `graphify` was "command not found" |

## 4b. Referenced resources, checked

Every relative file reference in all 18 skills was resolved mechanically (287
references). Nothing a skill needs in order to run is missing. The flags that
survive are three kinds, none of them broken links:

- **Placeholders and templates** — `docs/superpowers/plans/YYYY-MM-DD-<feature>.md`,
  `<workspace>/progress.md`, `F<n>.md`, `exact/path/to/file.py`.
- **Outputs the skill creates** — `layout.html`, `findings.json`,
  `graphify-out/graph.json`, `CLAUDE.md`. `graphify-out/graph.json` now exists;
  the rest are written during a run.
- **Claude Code plugin scripts that are absent by design** —
  `claude-security`'s `render_report.py`, `save_result.py`,
  `write_scan_meta.py`, `keep-waiting.sh`, `patch_artifacts.py`. The skill now
  says so at the point of use instead of linking to a `${CLAUDE_PLUGIN_ROOT}`
  path that cannot resolve here.

One inaccuracy found in this audit's own edits was fixed: the adaptation note in
each copied skill pointed at `docs/skill-audit-report.md` in a way that read as
skill-relative; it now says "at the *project* root".

Also verified: every skill directory name matches the `name:` in its
frontmatter (what the loader resolves against), and all 18 pass
`skill-creator/scripts/quick_validate.py`.

## 5. claude-security: what was checked by hand

Automated scans were **not** run (no `Workflow` tool). The manual pass, with the
command behind each line:

- **Secrets in version control** — no real credential in any tracked project
  file; the only matches are test mocks (`AIzaMockKey…`, `ya29.fake-access-token`).
  The vendored runtime contains marker *strings* from `cryptography`/`google-auth`,
  not keys.
- **Secrets at rest** — `data/secrets.toml` and `data/secrets/google_oauth_client.json.json`,
  both under the gitignored `data/`.
- **Log redaction** — `logging_setup.redact()` masks Google token patterns and
  `api_key`/`client_secret` assignments across the whole JSON entry, including
  exception text; the diagnostics export skips `secrets.toml`, redacts paths and
  replaces unreadable log lines.
- **Authentication** — PBKDF2-SHA256, 200 000 iterations, per-credential 16-byte
  salt; constant-time compare on both password digest and username; HMAC-SHA256
  signed session cookies with a 12-hour expiry and a persisted signing secret;
  middleware enforces it on every `/api` request in public mode; fails closed
  (503) when public mode has nothing to authenticate against and treats a config
  read failure as "require auth"; login throttled at 8 failures / 15 min; first-run
  setup is loopback-only; `/docs`, `/redoc` and `/openapi.json` are hidden in
  public mode.
- **Authorization** — role and permissions are read live per request, so a
  disabled or deleted account loses access on its next call; `require_role`
  (403) and `require_permission` (with `*`); the most sensitive endpoints
  (`/dev/*`, developer diagnostics) require the app role *and* a
  `DEVELOPER` session — a teacher's session cannot reach them. The client never
  declares its own role.
- **CSRF** — double-submit token compared with `compare_digest` on every
  non-safe method; the bearer-token `/api/distributed` API is exempt by design
  because it carries no ambient cookie.
- **File handling** — no `StaticFiles`/`FileResponse` serves a user-controlled
  path; uploads are capped at 64 MB, the filename is only echoed back, content
  is parsed as untrusted JSON; `Content-Disposition` is built by `_ascii_fallback`
  plus RFC 5987 encoding, so a student name cannot inject a header.
- **Dependencies** — runtime floors in `requirements.txt` plus a 53-entry exact
  `requirements-lock.txt` for the verified environment.
- **Residual risk** — there is no CI workflow, so no automated dependency
  vulnerability scan runs. That is a process gap, not a code defect, and it was
  not fixed here.

## 6. Graphify: verified working

- `graphifyy` 0.9.96, uv tool, `~/.local/bin/graphify.exe`; `uv tool update-shell`
  added that directory to the user PATH (verified by resolving `graphify` under a
  freshly composed PATH: `C:\Users\rudro\.local\bin\graphify.exe`, `graphify 0.9.96`).
- Build: `graphify extract . --code-only` → 15 601 nodes / 38 248 edges /
  445 communities in ~45 s, then `graphify update .` → 17 317 nodes / 40 220 edges
  / 579 communities. Every edge is `_origin: ast` — no API key, no model calls,
  fully reproducible; `built_at_commit` records the revision.
- Quality: `god-nodes` returns project hubs (`execute()`, `query_one()`,
  `get_config()`, `AppError`, `MathCheckerApiClient`, `log_event()`); `path`
  returned a real chain
  `blueprint_for_mark_scheme() <--contains-- blueprint.py --imports_from--> legacy_marking.py --imports_from--> call_audit.py`;
  node coverage is project code (8 315 backend, 5 071 desktop, 1 108 frontend)
  and **zero** vendored-runtime nodes.
- Freebuff can use it: the CLI runs from this shell, and `AGENTS.md`'s query-first
  rule is now executable.
- Docs, papers and images are not extracted (`--code-only`); those need an LLM
  backend and were deliberately skipped to avoid spending the project's Gemini
  quota. `graphify label .` adds community names later if wanted.

## 7. Still open

1. `claude-security` cannot run until a `Workflow`/subagent tool exists.
2. `subagent-driven-development` runs only in its inline form via
   `executing-plans`; `requesting-code-review` likewise ends in a self-review.
3. `writing-skills` is referenced once and not installed (superseded here by
   `skill-development` / `skill-creator`).
4. No CI runs a dependency vulnerability scan.
5. 20 MB of graph updates ride along with each code change; that is `AGENTS.md`'s
   stated policy, and the single-line alternative (ignore all of `graphify-out/`)
   is noted in `.gitignore`.
6. `.skill-audit-tmp/` holds this audit's pre-edit backups of every file it
   touched (and its probe projects). It is gitignored and graph-ignored; delete
   it when the changes are accepted.
7. An untracked directory `47743487-1851-4fbd-a2dc-46f3a3127a6b/` holding
   `src/main.tsx` and `vite.config.ts` sits at the repository root. It is not
   part of any build and is currently indexed by the graph. Its disposition is
   the owner's call.
