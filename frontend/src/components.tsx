import {
  createContext, Fragment, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import type { JobStatus } from "./api";
import { EmptyArt, type EmptyArtVariant } from "./EmptyArt";
import { Icon, type IconName } from "./Icons";
import { fitPaneWidths } from "./paneWidths";

/* ================================================================ badges */

export type Tone = "ok" | "err" | "warn" | "info" | "review" | "mute";

export function Badge({ tone, children, icon }: {
  tone: Tone; children: ReactNode; icon?: IconName;
}) {
  return (
    <span className={`badge ${tone}`}>
      {icon ? <Icon name={icon} size={12} /> : <span className="dot" />}
      <span className="badge-text">{children}</span>
    </span>
  );
}

const STATUS_TONES: Record<JobStatus, Tone> = {
  QUEUED: "mute", PREPARING: "info", DOWNLOADING: "info", UPLOADING: "info",
  MARKING: "info", VERIFYING: "info", SAVING: "info",
  WAITING_FOR_API: "warn", RETRYING: "warn",
  COMPLETED: "ok", FAILED: "err", CANCELLED: "mute",
};

export function StatusBadge({ status }: { status: JobStatus }) {
  return <Badge tone={STATUS_TONES[status] ?? "mute"}>{status.replace(/_/g, " ")}</Badge>;
}

/* =============================================================== buttons */

export type ButtonVariant =
  | "default" | "primary" | "secondary" | "success" | "warning"
  | "danger" | "ghost" | "nav";

export function Button({
  variant = "secondary", size, icon, iconRight, loading, block, tooltip, className,
  children, ...rest
}: {
  variant?: ButtonVariant;
  size?: "sm" | "lg";
  icon?: IconName;
  iconRight?: IconName;
  loading?: boolean;
  block?: boolean;
  tooltip?: string;
  className?: string;
  children?: ReactNode;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const classes = [
    "btn", variant !== "default" ? variant : "",
    size ?? "", loading ? "loading" : "", block ? "block" : "", className ?? "",
  ].filter(Boolean).join(" ");
  return (
    <button
      className={classes}
      data-tooltip={tooltip}
      aria-busy={loading || undefined}
      disabled={rest.disabled || loading}
      {...rest}
    >
      {icon && <Icon name={icon} size={size === "sm" ? 14 : 16} />}
      {children}
      {iconRight && <Icon name={iconRight} size={size === "sm" ? 14 : 16} />}
    </button>
  );
}

export function IconButton({
  icon, label, variant = "ghost", size, active, ...rest
}: {
  icon: IconName; label: string; variant?: ButtonVariant; size?: "sm";
  active?: boolean;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={`btn ${variant} icon-only ${size ?? ""} ${active ? "active" : ""}`}
      aria-label={label}
      data-tooltip={label}
      {...rest}
    >
      <Icon name={icon} size={size === "sm" ? 15 : 17} />
    </button>
  );
}

/* ============================================================ stat tiles */

export function Stat({
  label, value, tone, compact,
}: { label: ReactNode; value: ReactNode; tone?: "ok" | "err" | "warn"; compact?: boolean }) {
  return (
    <div className={`stat ${compact ? "compact" : ""}`}>
      <div className="label">{label}</div>
      <div className={`value ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

/** Polished, clickable status card: icon + big value + supporting text. */
export function StatusCard({
  icon, value, label, sub, tone = "info", onClick, spark,
}: {
  icon: IconName; value: ReactNode; label: ReactNode; sub?: ReactNode;
  tone?: "ok" | "err" | "warn" | "info" | "review" | "mute"; onClick?: () => void;
  /** Optional trailing visual (sparkline/mini chart). Display only. */
  spark?: ReactNode;
}) {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag
      className={`status-card tone-${tone} ${onClick ? "clickable" : ""}`}
      onClick={onClick}
      type={onClick ? "button" : undefined}
    >
      <div className="sc-icon"><Icon name={icon} size={20} /></div>
      <div style={{ minWidth: 0 }}>
        <div className="sc-value">{value}</div>
        <div className="sc-label">{label}</div>
        {sub != null && <div className="sc-sub">{sub}</div>}
      </div>
      {spark != null && <div style={{ marginLeft: "auto", alignSelf: "center" }}>{spark}</div>}
    </Tag>
  );
}

/** A single "needs attention" row. */
export function AttentionItem({
  icon, tone = "info", title, detail, action, onAction,
}: {
  icon: IconName; tone?: "ok" | "err" | "warn" | "info";
  title: ReactNode; detail?: ReactNode; action?: ReactNode; onAction?: () => void;
}) {
  return (
    <div className={`attention-item tone-${tone} ${onAction ? "clickable" : ""}`}
      onClick={onAction} role={onAction ? "button" : undefined} tabIndex={onAction ? 0 : undefined}
      onKeyDown={onAction ? (e) => { if (e.key === "Enter") onAction(); } : undefined}>
      <span className="ai-icon"><Icon name={icon} size={17} /></span>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="ai-title">{title}</div>
        {detail != null && <div className="ai-detail">{detail}</div>}
      </div>
      {action}
    </div>
  );
}

export function Progress({ value, thin, tone, label }: {
  value: number; thin?: boolean; tone?: "ok";
  /** Accessible name, e.g. "Correcting Yahia Mohamed". Without it a progress
   *  bar is announced as an unlabelled "0%" — useless to a screen reader. */
  label?: string;
}) {
  const pct = Math.round(Math.min(100, Math.max(0, value * 100)));
  return (
    <div className={`progress ${thin ? "thin" : ""} ${tone ? `tone-${tone}` : ""}`}
      role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
      aria-valuetext={`${pct}%`} aria-label={label}>
      <div className="bar" style={{ width: `${pct}%` }} />
    </div>
  );
}

/**
 * Keyboard activation props for click-only rows and cards.
 *
 * A `<tr onClick>` is invisible to keyboard users: it is not focusable and
 * Enter/Space do nothing. These props give any element the same contract a
 * button has — focusable, Enter and Space activate it — without nesting a
 * <button> inside a <tr> (invalid table markup).
 */
export function rowActivate(onActivate: () => void) {
  return {
    tabIndex: 0,
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
      // Space must not scroll the page, and Enter must not follow a nested link.
      e.preventDefault();
      e.stopPropagation();
      onActivate();
    },
  };
}

/* ============================================================== states */

export function Empty({ icon, art, title, hint, action }: {
  icon: string | IconName; title: ReactNode; hint?: ReactNode; action?: ReactNode;
  /** Illustrated line-art instead of the plain icon glyph. Decorative. */
  art?: EmptyArtVariant;
}) {
  const isName = typeof icon === "string" && /^[a-z-]+$/.test(icon);
  return (
    <div className="empty">
      <div className="big">
        {art ? <EmptyArt variant={art} />
          : isName ? <Icon name={icon as IconName} size={30} /> : <span>{icon}</span>}
      </div>
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint faint">{hint}</div>}
      {action && <div style={{ marginTop: 14, display: "flex", justifyContent: "center" }}>{action}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="stack" role="status" aria-live="polite">
      <div className="skeleton" />
      <div className="skeleton-lines">
        <div className="skeleton" />
        <div className="skeleton" />
      </div>
      <span className="faint small">{label}</span>
    </div>
  );
}

export function ErrorState({ title, detail, onRetry }: {
  title: string; detail?: string; onRetry?: () => void;
}) {
  return (
    <div className="card" style={{ borderColor: "var(--err-line)" }}>
      <div className="empty" style={{ padding: "var(--s6) var(--s4)" }}>
        <div className="big" style={{ color: "var(--err)" }}><Icon name="alert" size={30} /></div>
        <div className="empty-title" style={{ color: "var(--err)" }}>{title}</div>
        {detail && <div className="empty-hint faint">{detail}</div>}
        {onRetry && (
          <Button variant="secondary" size="sm" icon="refresh" style={{ marginTop: 12 }} onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    </div>
  );
}

/* ============================================================ page frame */

export function PageHeader({ title, sub, children, icon }: {
  title: string; sub?: string; children?: ReactNode; icon?: IconName;
}) {
  return (
    <div style={{ marginBottom: 20 }}>
      <div className="row between wrap" style={{ gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <h1 className="page-title" style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {icon && <Icon name={icon} size={26} style={{ color: "var(--accent)" }} />}
            {title}
          </h1>
          {sub && <p className="page-sub" style={{ marginBottom: 0 }}>{sub}</p>}
        </div>
        {children && <div className="row wrap">{children}</div>}
      </div>
    </div>
  );
}

/** Section heading with optional right-aligned actions. */
export function Section({ title, hint, actions, children, icon }: {
  title: ReactNode; hint?: ReactNode; actions?: ReactNode; children: ReactNode; icon?: IconName;
}) {
  return (
    <div className="card">
      <div className="row between wrap" style={{ marginBottom: 12, gap: 8 }}>
        <h3 style={{ margin: 0 }}>
          {icon && <Icon name={icon} size={16} style={{ color: "var(--accent)" }} />}
          {title}
          {hint != null && <span className="hint">{hint}</span>}
        </h3>
        {actions && <div className="row wrap">{actions}</div>}
      </div>
      {children}
    </div>
  );
}

export function KV({ k, v }: { k: ReactNode; v: ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}

/* =============================================================== tables */

export function DataTable({ head, children, minWidth }: {
  head: ReactNode; children: ReactNode; minWidth?: number;
}) {
  return (
    <div className="table-wrap">
      <table className="table table-stack" style={minWidth ? { minWidth } : undefined}>
        <thead>{head}</thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/* =========================================================== segmented */

export function Segmented<T extends string>({ value, options, onChange, ariaLabel }: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={value === o.value ? "active" : ""}
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ============================================================== tabs */

export function Tabs<T extends string>({ value, options, onChange, ariaLabel }: {
  value: T;
  options: { value: T; label: string; icon?: IconName }[];
  onChange: (v: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div className="tabs" role="tablist" aria-label={ariaLabel}>
      {options.map((o) => (
        <button key={o.value} type="button" role="tab"
          className={`tab ${value === o.value ? "active" : ""}`}
          aria-selected={value === o.value} onClick={() => onChange(o.value)}>
          {o.icon && <Icon name={o.icon} size={15} />}
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ============================================================ dropdown */

export function Dropdown({ label, children, align, icon }: {
  label: ReactNode; children: (close: () => void) => ReactNode;
  align?: "left" | "right"; icon?: IconName;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") setOpen(false); }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="dropdown" ref={ref}>
      <button type="button" className="btn secondary sm" aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen((v) => !v)}>
        {icon && <Icon name={icon} size={14} />}
        {label}
        <Icon name="chevron-down" size={13} />
      </button>
      {open && (
        <div className="dropdown-menu" role="menu"
          style={align === "left" ? { left: 0, right: "auto" } : undefined}>
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}

/* ================================================================ modal */

/* Shared overlay behaviour: Escape closes, Tab cycles inside the panel, the
   background stops scrolling while it is open, and focus returns where it
   came from. Without the scroll lock a teacher can open a modal and then
   scroll the page behind it into a state they never clicked. */
function useOverlayBehaviour(ref: React.RefObject<HTMLElement>,
                             onClose: () => void) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const node = ref.current;
    const previous = document.activeElement as HTMLElement | null;

    const scroller = document.querySelector<HTMLElement>(".main-scroll");
    const prevOverflow = scroller ? scroller.style.overflow : "";
    if (scroller) scroller.style.overflow = "hidden";

    const focusables = () => Array.from(
      node?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), '
        + 'input:not([disabled]), select:not([disabled]), '
        + '[tabindex]:not([tabindex="-1"])') ?? []
    ).filter((el) => el.offsetParent !== null || el === document.activeElement);

    const target = node?.querySelector<HTMLElement>("[data-autofocus]")
      ?? focusables()[0] ?? node;
    target?.focus?.();

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") { closeRef.current(); return; }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || active === node)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (scroller) scroller.style.overflow = prevOverflow;
      previous?.focus?.();
    };
  }, [ref]);
}

export function Modal({ title, onClose, children, footer, wide, icon }: {
  title: ReactNode; onClose: () => void; children: ReactNode;
  footer?: ReactNode; wide?: boolean; icon?: IconName;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  useOverlayBehaviour(dialog, onClose);

  return (
    <div className="modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true"
        ref={dialog} tabIndex={-1}
        aria-label={typeof title === "string" ? title : undefined}>
        <div className="modal-head">
          <h3 style={{ display: "flex", alignItems: "center", gap: 9 }}>
            {icon && <Icon name={icon} size={18} style={{ color: "var(--accent)" }} />}
            {title}
          </h3>
          <IconButton icon="x" label="Close" onClick={onClose} />
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

/* =============================================================== drawer */

export function Drawer({ title, onClose, children }: {
  title: ReactNode; onClose: () => void; children: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);
  useOverlayBehaviour(panel, onClose);

  return (
    <div className="drawer-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <aside className="drawer" role="dialog" aria-modal="true" ref={panel}
        tabIndex={-1}
        aria-label={typeof title === "string" ? title : undefined}>
        <div className="drawer-head">
          <h3 style={{ margin: 0 }}>{title}</h3>
          <IconButton icon="x" label="Close" onClick={onClose} />
        </div>
        <div className="drawer-body">{children}</div>
      </aside>
      <div />
    </div>
  );
}

/* ================================================================ toasts */

type ToastTone = "ok" | "err" | "warn" | "info";
interface ToastItem { id: number; tone: ToastTone; message: string }

const ToastCtx = createContext<(tone: ToastTone, message: string) => void>(() => {});

export function useToast() {
  return useContext(ToastCtx);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const push = useCallback((tone: ToastTone, message: string) => {
    const id = nextId.current++;
    setItems((cur) => [...cur.slice(-4), { id, tone, message }]);
    window.setTimeout(() => {
      setItems((cur) => cur.filter((t) => t.id !== id));
    }, 5200);
  }, []);

  const value = useMemo(() => push, [push]);
  const iconFor = (tone: ToastTone): IconName =>
    tone === "ok" ? "check-circle" : tone === "err" ? "alert" : tone === "warn" ? "alert" : "info";

  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="toast-stack" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast tone-${t.tone}`}>
            <Icon name={iconFor(t.tone)} size={16} style={{ marginTop: 1 }} />
            <span style={{ flex: 1, minWidth: 0 }}>{t.message}</span>
            <button className="toast-close" aria-label="Dismiss"
              onClick={() => setItems((cur) => cur.filter((x) => x.id !== t.id))}>✕</button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* =========================================================== export menu */

const EXPORT_LABELS: Record<string, string> = {
  csv: "CSV", xlsx: "Excel", pdf: "PDF", json: "JSON", md: "Markdown",
};

export function ExportMenu({ base, params, formats = ["csv", "xlsx", "pdf", "json"], onExported }: {
  base: string; params?: Record<string, string | undefined>;
  formats?: string[]; onExported?: (fmt: string) => void;
}) {
  const qs = useMemo(() => {
    const pairs = Object.entries(params ?? {})
      .filter(([, v]) => v != null && v !== "")
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
    return pairs.length ? `&${pairs.join("&")}` : "";
  }, [params]);

  return (
    <Dropdown label="Export" icon="download" align="right">
      {(close) => (
        <>
          <div className="menu-label">Download as</div>
          {formats.map((f) => (
            <button key={f} role="menuitem" onClick={() => { close(); onExported?.(f); }}>
              <a href={`/api${base}?format=${f}${qs}`}
                style={{ color: "inherit", textDecoration: "none", display: "flex",
                  alignItems: "center", gap: 9, width: "100%" }}>
                <Icon name="file" size={15} />
                {EXPORT_LABELS[f] ?? f.toUpperCase()}
              </a>
            </button>
          ))}
        </>
      )}
    </Dropdown>
  );
}

/* ====================================================== confirm dialog */

/**
 * Confirmation for consequential actions (factory reset, disconnect,
 * publish). Never used for routine actions — a confirm dialog everywhere is
 * a confirm dialog nowhere. ``danger`` marks destructive wording.
 */
export function ConfirmDialog({ title, message, confirmLabel = "Confirm",
  danger, busy, onConfirm, onClose, icon }: {
  title: string; message: ReactNode; confirmLabel?: string;
  danger?: boolean; busy?: boolean;
  onConfirm: () => void; onClose: () => void; icon?: IconName;
}) {
  return (
    <Modal title={title} onClose={onClose} icon={icon ?? (danger ? "alert" : "info")}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm}
            loading={busy} data-autofocus>{confirmLabel}</Button>
        </>
      }>
      <div className="stack">{message}</div>
    </Modal>
  );
}

/* ========================================================= provenance */

/**
 * Where a marking number came from — always labelled, never colour alone.
 *   ai         the model's output, not yet confirmed by anything else
 *   verified   an independent check agreed with it
 *   teacher    a teacher approved it explicitly
 *   overridden the teacher set the mark themselves
 */
export function ProvenanceBadge({ kind }: {
  kind: "ai" | "verified" | "teacher" | "overridden";
}) {
  const map = {
    ai: { tone: "mute" as Tone, label: "AI output" },
    verified: { tone: "ok" as Tone, label: "Verified" },
    teacher: { tone: "info" as Tone, label: "Teacher approved" },
    overridden: { tone: "info" as Tone, label: "Teacher edited" },
  };
  const { tone, label } = map[kind];
  return (
    <Badge tone={tone} icon={kind === "verified" ? "check-circle"
      : kind === "ai" ? "sparkles" : "review"}>{label}</Badge>
  );
}

/* ============================================================ filter bar */

/** One row of list controls: search first, facet chips/controls, then the
 *  trailing actions. The search box shrinks before anything else. */
export function FilterBar({ search, onSearch, placeholder = "Search…",
  children, right, ariaLabel = "Filters" }: {
  search: string; onSearch: (v: string) => void; placeholder?: string;
  children?: ReactNode; right?: ReactNode; ariaLabel?: string;
}) {
  return (
    <div className="filter-bar" role="search" aria-label={ariaLabel}>
      <input className="input filter-search" value={search}
        placeholder={placeholder} aria-label={placeholder}
        onChange={(e) => onSearch(e.target.value)} />
      {children && <div className="row wrap filter-facets">{children}</div>}
      {right && <div className="row wrap filter-right">{right}</div>}
    </div>
  );
}

/* ========================================================= workspace panes */

/**
 * Resizable N-pane workspace (marking workspace §11, review queue).
 *
 * Every pane except the last keeps an explicit pixel width the teacher can
 * drag; the last pane absorbs the remainder. The divider is a real
 * ``role="separator"`` with keyboard support (arrow keys). Below 900px a
 * split layout is impractical, so the panes become tabs (responsive rule
 * §11). Widths persist per ``storageKey``.
 *
 * The stored widths are fitted to the space actually available: a teacher who
 * dragged the first panes wide in a big window and then narrowed the window
 * used to leave the last pane a sliver (or, after inline-size containment, a
 * few px of padding), which made the whole marking result overflow. The last
 * pane now always keeps at least ``min`` and the others absorb the shortfall
 * down to their own ``min``.
 */


export function WorkspacePanes({ panes, storageKey, min = 220 }: {
  panes: { id: string; label: string; el: ReactNode }[];
  storageKey: string; min?: number;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [containerW, setContainerW] = useState(0);
  const [widths, setWidths] = useState<number[]>(() => {
    try {
      const raw = window.localStorage.getItem(storageKey);
      const parsed = raw ? JSON.parse(raw) : null;
      if (Array.isArray(parsed) && parsed.every((n) => typeof n === "number")) {
        return parsed;
      }
    } catch { /* private mode */ }
    return panes.map((_, i) => (i === 0 ? 260 : 380));
  });
  const [tab, setTab] = useState(0);
  const [stacked, setStacked] = useState(
    () => typeof window !== "undefined" && window.innerWidth < 900);

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 899px)");
    const onChange = () => setStacked(mq.matches);
    onChange();
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);

  useEffect(() => {
    try { window.localStorage.setItem(storageKey, JSON.stringify(widths)); } catch { /* ignore */ }
  }, [widths, storageKey]);

  // Track the real workspace width so stored pane widths can be fitted to it.
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0;
      setContainerW((prev) => (Math.abs(prev - w) > 1 ? w : prev));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const fitted = useMemo(
    () => fitPaneWidths(widths, panes.length, containerW, min),
    [widths, panes.length, containerW, min],
  );

  if (stacked) {
    return (
      <div className="workspace-stacked">
        <Tabs value={String(tab)} ariaLabel="Workspace panes"
          options={panes.map((p, i) => ({ value: String(i), label: p.label }))}
          onChange={(v) => setTab(Number(v))} />
        <div className="workspace-pane">{panes[tab]?.el}</div>
      </div>
    );
  }

  const setWidth = (i: number, w: number) =>
    setWidths((cur) => fitPaneWidths(
      cur.map((v, j) => (j === i ? Math.max(min, w) : v)),
      panes.length, containerW, min));

  return (
    <div className="workspace" ref={rootRef}>
      {panes.map((pane, i) => (
        <Fragment key={pane.id}>
          {i > 0 && (
            <div className="wp-divider" role="separator" aria-orientation="vertical"
              aria-label={`Resize ${pane.label}`} tabIndex={0}
              aria-valuenow={Math.round(fitted[i - 1] ?? 0)}
              onKeyDown={(e) => {
                if (e.key === "ArrowLeft") { setWidth(i - 1, (fitted[i - 1] ?? min) - 24); e.preventDefault(); }
                if (e.key === "ArrowRight") { setWidth(i - 1, (fitted[i - 1] ?? min) + 24); e.preventDefault(); }
              }}
              onPointerDown={(e) => {
                const startX = e.clientX;
                const startW = fitted[i - 1] ?? min;
                (e.target as HTMLElement).setPointerCapture(e.pointerId);
                const move = (ev: PointerEvent) =>
                  setWidth(i - 1, startW + (ev.clientX - startX));
                const up = () => {
                  window.removeEventListener("pointermove", move);
                  window.removeEventListener("pointerup", up);
                };
                window.addEventListener("pointermove", move);
                window.addEventListener("pointerup", up);
              }} />
          )}
          <div className="workspace-pane"
            style={i < panes.length - 1 ? { width: fitted[i] ?? min, flex: "0 0 auto" } : undefined}>
            {pane.el}
          </div>
        </Fragment>
      ))}
    </div>
  );
}

export { Icon };
export type { IconName };
